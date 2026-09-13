"""텔레그램 감시 모드 테스트(가짜 클라이언트, 네트워크 불필요).

`python tests/test_listen.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.links import parse_link  # noqa: E402
from tgdl.listen import (  # noqa: E402
    HELP_TEXT,
    ListenService,
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


class FakeClient:
    def __init__(self, fail_send_file: str = ""):
        self.sent: list = []
        self.edits: list = []
        self.files: list = []
        self.fail_send_file = fail_send_file
        self._next_id = 100

    async def get_entity(self, chat):
        return f"entity:{chat}"

    async def send_message(self, entity, text, reply_to=None):
        self._next_id += 1
        self.sent.append((text, reply_to))
        return FakeMessage(self._next_id, text)

    async def edit_message(self, entity, mid, text):
        self.edits.append((mid, text))

    async def send_file(
        self, entity, path, caption=None, supports_streaming=None,
        progress_callback=None, reply_to=None,
    ):
        if self.fail_send_file:
            raise RuntimeError(self.fail_send_file)
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
    assert len(links_from_message("https://t.me/c/1/2", False)) == 1
    assert links_from_message("그냥 수다", False) == []
    # 우리가 되돌려 보낸 파일에 반응하면 무한 반복이 된다.
    assert links_from_message("https://t.me/c/1/2", True) == []
    assert links_from_message(None, False) == []


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
