"""텔레그램 감시 모드 테스트(가짜 클라이언트, 네트워크 불필요).

`python tests/test_listen.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.tl.types import (  # noqa: E402
    MessageMediaDocument,
    MessageMediaWebPage,
    User,
    WebPageEmpty,
)

from tgdl.links import parse_link  # noqa: E402
from tgdl.listen import (  # noqa: E402
    HELP_TEXT,
    ListenService,
    RelayReporter,
    RequestState,
    has_attachment,
    is_command,
    links_from_message,
)
from tgdl.reporter import Reporter  # noqa: E402


class FakeMessage:
    def __init__(self, mid, text="", media=None):
        self.id = mid
        self.message = text
        self.raw_text = text
        self.media = media


def link_preview():
    """링크를 붙여넣으면 텔레그램이 자동으로 붙이는 미리보기."""
    return MessageMediaWebPage(webpage=WebPageEmpty(id=0))


class FakeEvent:
    """텔레그램 알림 흉내."""

    def __init__(self, chat_id, message):
        self.chat_id = chat_id
        self.message = message


class FakeClient:
    def __init__(self, fail_send_file: str = "", entity=None, history=None,
                 hang_send_message: bool = False, fail_reply_send: bool = False,
                 fail_reply_file: bool = False):
        self.hang_send_message = hang_send_message
        self.fail_reply_send = fail_reply_send
        self.fail_reply_file = fail_reply_file
        self.sent: list = []
        self.edits: list = []
        self.files: list = []
        self.fail_send_file = fail_send_file
        self.entity = entity if entity is not None else User(id=555)
        self.history: list = list(history or [])
        self.handlers: list = []
        self.get_messages_calls: list = []
        self._next_id = 100

    async def get_entity(self, chat):
        return self.entity

    def on(self, builder):
        def register(func):
            self.handlers.append(func)
            return func

        return register

    async def get_messages(self, entity, limit=None, min_id=0, **kwargs):
        self.get_messages_calls.append({"limit": limit, "min_id": min_id})
        found = [msg for msg in self.history if msg.id > (min_id or 0)]
        found.sort(key=lambda msg: msg.id, reverse=True)
        return found[: limit or len(found)]

    async def dispatch(self, chat_id, message):
        for handler in self.handlers:
            await handler(FakeEvent(chat_id, message))

    async def send_message(self, entity, text, reply_to=None):
        self.sent.append((text, reply_to))
        if self.hang_send_message:
            await asyncio.sleep(3600)  # 응답이 오지 않는 상황
        if self.fail_reply_send and reply_to is not None:
            raise RuntimeError("답장을 보낼 수 없는 대화방")
        self._next_id += 1
        return FakeMessage(self._next_id, text)

    async def edit_message(self, entity, mid, text):
        self.edits.append((mid, text))

    async def send_file(
        self, entity, path, caption=None, supports_streaming=None,
        progress_callback=None, reply_to=None,
    ):
        if self.fail_send_file:
            raise RuntimeError(self.fail_send_file)
        if self.fail_reply_file and reply_to is not None:
            raise RuntimeError("답장으로는 파일을 보낼 수 없음")
        size = os.path.getsize(path)
        if progress_callback:
            progress_callback(size, size)
        self.files.append((path, caption, supports_streaming, reply_to))

    @property
    def last_edit(self) -> str:
        return self.edits[-1][1] if self.edits else ""


class FakeDownloader:
    """리포터에 완료/실패를 알려주는 가짜 다운로더."""

    def __init__(self, reporter: Reporter, folder: Path, fail: str = "", size: int = 2048):
        self.reporter = reporter
        self.folder = folder
        self.fail = fail
        self.size = size
        self.processed: list = []

    async def process(self, link):
        self.processed.append(link)
        if self.fail:
            self.reporter.failed("k", "영상.mp4", self.fail)
            return
        path = self.folder / f"영상_{len(self.processed)}.mp4"
        path.write_bytes(b"x" * self.size)
        self.reporter.progress("k", path.name, self.size // 2, self.size)
        self.reporter.finished("k", path.name, path)


def processed_links(created) -> int:
    """워커가 실제로 처리한 링크 수. 큐는 워커가 바로 비우므로 결과로 센다."""
    return sum(len(downloader.processed) for downloader in created)


async def wait_processed(created, expected: int, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if processed_links(created) >= expected:
            break
        await asyncio.sleep(0.02)
    return processed_links(created)


def build(folder, client=None, fail="", send_back=True):
    client = client or FakeClient()
    created: list = []

    def factory(reporter):
        downloader = FakeDownloader(reporter, Path(folder), fail=fail)
        created.append(downloader)
        return downloader

    service = ListenService(
        client, Reporter(), factory, chat="me", send_back=send_back, edit_interval=3.0
    )
    service.entity = "entity:me"
    return service, client, created


# ------------------------------------------------------------------ 순수 함수
def test_links_from_message():
    assert len(links_from_message(FakeMessage(1, "https://t.me/c/1/2"))) == 1
    assert links_from_message(FakeMessage(2, "그냥 수다")) == []
    assert links_from_message(FakeMessage(3, "")) == []
    # 우리가 되돌려 보낸 파일에 반응하면 무한 반복이 된다.
    assert links_from_message(
        FakeMessage(4, "https://t.me/c/1/2", media=MessageMediaDocument())
    ) == []


def test_link_preview_is_not_treated_as_a_file():
    """링크를 붙여넣으면 미리보기가 붙는다. 이것을 파일로 오해하면 모든 링크를 놓친다."""
    preview_message = FakeMessage(9, "https://t.me/sexymv0001/935", media=link_preview())
    assert has_attachment(preview_message) is False
    assert len(links_from_message(preview_message)) == 1

    file_message = FakeMessage(10, "설명", media=MessageMediaDocument())
    assert has_attachment(file_message) is True
    assert links_from_message(file_message) == []


def test_is_command():
    assert is_command("/help") is True
    assert is_command("  /start ") is True
    assert is_command("https://t.me/a/1") is False
    assert is_command("/" + "가" * 100) is False


# ------------------------------------------------------------------ 메시지 처리
def test_help_reply_for_command():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, _ = build(tmp)
            assert await service.handle_message(FakeMessage(1, "/help")) is False
            assert client.sent and client.sent[0][0] == HELP_TEXT

    asyncio.run(scenario())


def test_link_is_queued_and_chatter_ignored():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, _ = build(tmp)
            assert await service.handle_message(FakeMessage(1, "https://t.me/c/1/2")) is True
            assert service.queue.qsize() == 1
            assert await service.handle_message(FakeMessage(2, "고마워")) is False
            assert await service.handle_message(
                FakeMessage(3, "https://t.me/c/1/9", media=object())
            ) is False
            assert service.queue.qsize() == 1

    asyncio.run(scenario())


# ------------------------------------------------------------------ 전체 흐름
def test_process_downloads_then_sends_back():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            state = await service.process([parse_link("https://t.me/c/1/2")], reply_to=7)

            assert len(state.paths) == 1
            assert len(client.files) == 1, "받은 파일을 되돌려 보내야 한다"
            path, caption, streaming, reply_to = client.files[0]
            assert Path(path).name == "영상_1.mp4"
            assert caption == "영상_1.mp4"
            assert "t.me" not in (caption or ""), "캡션에 링크가 없어야 무한 반복이 안 된다"
            assert streaming is True
            assert reply_to == 7

            assert "✅ 완료" in client.last_edit
            assert "영상_1.mp4" in client.last_edit
            # 아이폰·안드로이드 둘 다 안내해야 한다
            assert "동영상 저장" in client.last_edit
            assert "갤러리에 저장" in client.last_edit
            assert client.sent[0][1] == 7  # 원래 메시지에 답장으로 상태 표시

    asyncio.run(scenario())


def test_no_send_keeps_files_on_pc():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, _ = build(tmp, send_back=False)
            await service.process([parse_link("https://t.me/c/1/2")])
            assert client.files == []
            assert "컴퓨터에" in client.last_edit

    asyncio.run(scenario())


def test_download_failure_is_reported_to_phone():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, _ = build(tmp, fail="가입되지 않은 채널입니다")
            state = await service.process([parse_link("https://t.me/c/1/2")])
            assert state.paths == []
            assert "⚠️ 실패" in client.last_edit
            assert "가입되지 않은 채널입니다" in client.last_edit

    asyncio.run(scenario())


def test_too_big_upload_gives_friendly_message():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(fail_send_file="Request Entity Too Big: file size")
            service, client, _ = build(tmp, client=client)
            state = await service.process([parse_link("https://t.me/c/1/2")])
            assert state.errors
            assert "용량이 너무 커서" in client.last_edit
            assert "컴퓨터에 저장" in client.last_edit

    asyncio.run(scenario())


def test_worker_processes_queue_in_order():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            service._worker = asyncio.create_task(service._run_worker())
            await service.handle_message(FakeMessage(1, "https://t.me/c/1/2"))
            await service.handle_message(FakeMessage(2, "https://t.me/c/1/3"))
            await service.queue.join()
            service._worker.cancel()
            assert len(client.files) == 2, "두 요청 모두 처리해야 한다"

    asyncio.run(scenario())


# ------------------------------------------------------- 알림 / 직접 확인
def test_start_ignores_other_chats_and_accepts_own():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            service.entity = None  # start() 가 직접 정하도록
            service.poll_interval = 0  # 이 테스트에서는 직접 확인 끔
            await service.start()
            assert service.chat_id == 555
            assert client.handlers, "알림 처리기가 등록돼야 한다"

            # 다른 대화방의 메시지는 무시
            await client.dispatch(999, FakeMessage(1, "https://t.me/c/1/2"))
            assert await wait_processed(created, 1, timeout=0.2) == 0

            # 내 대화방의 메시지는 처리
            await client.dispatch(555, FakeMessage(2, "https://t.me/c/1/3"))
            assert await wait_processed(created, 1) == 1
            await service.stop()

    asyncio.run(scenario())


def test_polling_finds_message_when_no_notification():
    """알림이 오지 않아도 대화방을 직접 확인해 찾아야 한다."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            service.entity = None
            service.poll_interval = 0.05
            await service.start()  # 시작 시점에는 대화방이 비어 있음

            # 알림 없이 메시지가 생긴 상황
            client.history.append(FakeMessage(7, "https://t.me/c/1/5"))
            assert await wait_processed(created, 1) == 1, "직접 확인으로 찾아야 한다"
            await service.stop()

    asyncio.run(scenario())


def test_notification_and_polling_do_not_double_process():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            service.entity = None
            service.poll_interval = 0.05
            await service.start()

            message = FakeMessage(11, "https://t.me/c/1/9")
            client.history.append(message)
            await client.dispatch(555, message)  # 알림으로 먼저 처리
            assert await wait_processed(created, 1) == 1

            for _ in range(6):  # 직접 확인이 여러 번 돌아도
                await asyncio.sleep(0.03)
            assert processed_links(created) == 1, "같은 메시지를 두 번 처리하면 안 된다"
            await service.stop()

    asyncio.run(scenario())


def test_catch_up_processes_recent_links():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            history = [
                FakeMessage(3, "수다"),
                FakeMessage(4, "https://t.me/c/1/40"),
                FakeMessage(5, "https://t.me/c/1/41"),
            ]
            service, client, created = build(tmp)
            client.history = history
            service.entity = None
            service.poll_interval = 0
            service.catch_up = 10
            await service.start()
            assert await wait_processed(created, 2) == 2, "지난 메시지의 링크를 찾아야 한다"
            await service.stop()

    asyncio.run(scenario())


def test_old_history_is_not_touched_without_catch_up():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            client.history = [FakeMessage(4, "https://t.me/c/1/40")]
            service.entity = None
            service.poll_interval = 0.05
            await service.start()
            assert await wait_processed(created, 1, timeout=0.25) == 0, "옛 메시지는 건드리지 않는다"
            assert service._last_id == 4
            await service.stop()

    asyncio.run(scenario())


def test_seen_memory_is_bounded():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _ = build(tmp)
            for index in range(3000):
                service._mark_seen(index)
            assert len(service._seen_ids) <= 2000, "기억이 무한히 늘어나면 안 된다"
            assert service._mark_seen(2999) is False, "최근 것은 기억한다"

    asyncio.run(scenario())


# --------------------------------------- 상태 메시지가 막혀도 받고 보내기
def test_status_hang_does_not_block_download_or_sending():
    """상태 메시지가 응답하지 않아도 PC 다운로드와 폰 전송은 끝까지 진행돼야 한다."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(hang_send_message=True)
            service, client, created = build(tmp, client=client)
            service.status_timeout = 0.1  # 테스트에서 빠르게 시간 초과

            state = await service.process([parse_link("https://t.me/c/1/2")], reply_to=7)

            assert len(state.paths) == 1, "컴퓨터에 파일을 받아야 한다"
            assert state.paths[0].exists()
            assert len(client.files) == 1, "폰으로 되돌려 보내야 한다"
            assert processed_links(created) == 1

    asyncio.run(scenario())


def test_status_send_falls_back_to_no_reply():
    """답장으로 보내는 것이 막히면 답장 없이 다시 시도해야 한다."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(fail_reply_send=True)
            service, client, _ = build(tmp, client=client)

            state = await service.process([parse_link("https://t.me/c/1/2")], reply_to=7)

            attempts = [reply for _, reply in client.sent]
            assert 7 in attempts, "먼저 답장으로 시도한다"
            assert None in attempts, "실패하면 답장 없이 다시 시도한다"
            assert len(state.paths) == 1 and len(client.files) == 1

    asyncio.run(scenario())


def test_upload_falls_back_to_no_reply():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(fail_reply_file=True)
            service, client, _ = build(tmp, client=client)

            state = await service.process([parse_link("https://t.me/c/1/2")], reply_to=7)

            assert len(client.files) == 1, "답장 없이 다시 보내 성공해야 한다"
            assert client.files[0][3] is None, "재시도는 답장 없이"
            assert not state.errors, f"실패로 기록되면 안 된다: {state.errors}"

    asyncio.run(scenario())


def test_pasted_link_with_preview_is_processed_end_to_end():
    """실제 상황: 미리보기가 붙은 링크 → 컴퓨터에 다운로드 → 폰으로 전송."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, created = build(tmp)
            service.entity = None
            service.poll_interval = 0
            await service.start()

            pasted = FakeMessage(30, "https://t.me/sexymv0001/935", media=link_preview())
            await client.dispatch(555, pasted)

            assert await wait_processed(created, 1) == 1, "미리보기가 붙어도 처리해야 한다"
            assert len(client.files) == 1, "폰으로 전송까지 되어야 한다"
            await service.stop()

    asyncio.run(scenario())


def test_status_wording_tells_what_is_happening():
    """텔레그램 상태 메시지 문구: 컴퓨터로 다운받는 중 → 폰으로 전송 중 → 완료."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            service, client, _ = build(tmp)
            state = RequestState()

            # 1) 다운로드 중 문구
            reporter = RelayReporter(Reporter(), state)
            reporter.progress("k", "영상.mp4", 300 * 1024 * 1024, 724 * 1024 * 1024)
            assert "컴퓨터로 다운받는 중" in state.line
            assert "41%" in state.line and "724.0MB" in state.line

            # 2) 전송 중 문구
            path = Path(tmp) / "영상.mp4"
            path.write_bytes(b"x" * 2048)
            await service._upload(None, state, path)
            assert "폰으로 전송 중" in state.line
            assert len(client.files) == 1

            # 3) 마무리 문구
            state.paths.append(path)
            summary = service._summary(state)
            assert "완료" in summary and "영상.mp4" in summary

    asyncio.run(scenario())


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
