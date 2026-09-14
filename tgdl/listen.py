"""텔레그램 채팅방을 감시해, 내가 붙여넣은 링크를 자동으로 받아 되돌려주는 모드.

밖에서 휴대폰으로 링크만 보내면 집 컴퓨터가 받아서 다시 보내준다.
텔레그램만 있으면 아이폰·안드로이드·태블릿·PC 어디서나 똑같이 쓸 수 있다.
봇(BotFather)은 파일 전송이 50MB 로 막혀 있어 큰 동영상을 보낼 수 없으므로,
내 계정으로 직접 보낸다(최대 2GB, 프리미엄 4GB).

기본 감시 대상은 '저장한 메시지'(Saved Messages)다. `--chat` 으로 내가 만든
비공개 채널이나 봇 대화방을 지정할 수도 있다.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .links import ParsedLink, extract_links
from .reporter import Reporter, human_size

#: 상태 메시지를 고쳐 쓰는 최소 간격(초). 너무 자주 고치면 텔레그램이 제한한다.
STATUS_EDIT_INTERVAL = 6.0

#: 알림(update)이 오지 않는 경우를 대비해 대화방을 직접 확인하는 간격(초)
POLL_INTERVAL = 15.0

#: 같은 메시지를 두 번 처리하지 않도록 기억해 두는 개수
SEEN_LIMIT = 2000

#: 상태 메시지 전송·수정을 기다리는 한계(초).
#: 상태 표시는 장식이므로, 이게 늦어도 다운로드를 막으면 안 된다.
STATUS_TIMEOUT = 20.0

HELP_TEXT = (
    "🎬 텔레그램 동영상 다운로더\n\n"
    "받고 싶은 동영상의 링크를 이 대화방에 붙여넣으세요.\n"
    "집 컴퓨터가 받아서 여기로 다시 보내드립니다.\n\n"
    "· 여러 링크를 한 번에 붙여넣어도 됩니다\n"
    "· 비공개 채널도 내 계정이 가입돼 있으면 받을 수 있습니다\n"
    "· 받은 영상을 꾹 눌러 저장하면 사진첩(갤러리)에 들어갑니다\n"
    "  아이폰: '동영상 저장' · 안드로이드: '갤러리에 저장'"
)


def links_from_message(text: Optional[str], has_media: bool) -> List[ParsedLink]:
    """이 메시지를 처리해야 하는지 판단한다.

    사진·동영상이 붙은 메시지는 무시한다. 우리가 되돌려 보낸 파일에 반응해
    무한 반복하는 것을 막기 위한 안전장치다.
    """
    if has_media:
        return []
    return extract_links(text or "")


def _is_too_big(exc: Exception) -> bool:
    message = str(exc).lower()
    return "too big" in message or "file size" in message or "file_too_large" in message


def is_command(text: Optional[str]) -> bool:
    stripped = (text or "").strip()
    return stripped.startswith("/") and len(stripped) <= 32


class RequestState:
    """한 번의 요청(링크 하나 이상)에 대한 진행 상황."""

    def __init__(self) -> None:
        self.line = "⏳ 확인 중…"
        self.paths: List[Path] = []
        self.errors: List[str] = []
        self.skipped: List[Path] = []


class RelayReporter(Reporter):
    """다운로더의 알림을 터미널과 텔레그램 상태 메시지 양쪽으로 보낸다."""

    def __init__(self, console: Reporter, state: RequestState) -> None:
        self.console = console
        self.state = state

    def log(self, message: str, level: str = "info") -> None:
        self.console.log(message, level)
        if level in {"warn", "error"}:
            self.state.line = f"⚠️ {message}"

    def progress(self, key: str, label: str, done: int, total: int) -> None:
        self.console.progress(key, label, done, total)
        percent = (done / total * 100) if total else 0
        self.state.line = (
            f"⬇️ 받는 중 {percent:.0f}%\n"
            f"{label}\n{human_size(done)} / {human_size(total)}"
        )

    def finished(self, key: str, label: str, path, skipped: bool = False) -> None:
        self.console.finished(key, label, path, skipped)
        if skipped:
            self.state.skipped.append(Path(path))
        else:
            self.state.paths.append(Path(path))

    def failed(self, key: str, label: str, error: str) -> None:
        self.console.failed(key, label, error)
        self.state.errors.append(f"{label} — {error}")


class ListenService:
    """채팅방을 감시하고 요청을 하나씩 순서대로 처리한다."""

    def __init__(
        self,
        client,
        console: Reporter,
        make_downloader: Callable[[Reporter], object],
        chat: str = "me",
        send_back: bool = True,
        edit_interval: float = STATUS_EDIT_INTERVAL,
        poll_interval: float = POLL_INTERVAL,
        catch_up: int = 0,
        debug: bool = False,
    ) -> None:
        self.client = client
        self.console = console
        self.make_downloader = make_downloader
        self.chat = chat
        self.send_back = send_back
        self.edit_interval = max(3.0, float(edit_interval))
        #: 알림이 오지 않을 때를 대비한 직접 확인 간격(0 이면 확인하지 않음)
        self.poll_interval = max(0.0, float(poll_interval))
        #: 시작할 때 거슬러 올라가 확인할 메시지 수
        self.catch_up = max(0, int(catch_up))
        self.debug = bool(debug)
        #: 상태 메시지 호출을 기다리는 한계(초)
        self.status_timeout = STATUS_TIMEOUT
        self.entity = None
        self.chat_id: Optional[int] = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self._last_id = 0
        self._seen_ids: set = set()
        self._seen_order: deque = deque()
        self._poller: Optional[asyncio.Task] = None
        self._worker: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ 시작
    async def start(self) -> None:
        from telethon import events, utils

        self.entity = await self.client.get_entity(self.chat)
        try:
            self.chat_id = utils.get_peer_id(self.entity)
        except Exception:  # noqa: BLE001 - 못 구해도 알림은 모두 확인한다
            self.chat_id = None

        # 시작 시점의 마지막 메시지를 기억해, 옛 메시지를 다시 받지 않는다.
        recent = await self._recent_messages(max(1, self.catch_up))
        self._last_id = max((msg.id for msg in recent), default=0)
        if self.debug:
            self.console.log(
                f"[진단] 대화방 번호 {self.chat_id} · 마지막 메시지 {self._last_id}"
            )

        if self.catch_up and recent:
            self.console.log(f"최근 메시지 {len(recent)}개에서 링크를 찾습니다…")
            for message in sorted(recent, key=lambda msg: msg.id):
                await self.handle_message(message, source="지난 메시지")

        self._worker = asyncio.create_task(self._run_worker())
        if self.poll_interval:
            self._poller = asyncio.create_task(self._poll_loop())

        # 대화방 확인을 직접 한다. 텔레그램 라이브러리의 대화방 필터 해석에
        # 의존하지 않으므로, 그쪽이 어긋나도 놓치지 않는다.
        @self.client.on(events.NewMessage())
        async def _on_message(event):  # pragma: no cover - 실제 연결에서만
            try:
                if self.chat_id is not None and event.chat_id != self.chat_id:
                    return
                await self.handle_message(event.message, source="알림")
            except Exception as exc:  # noqa: BLE001
                self.console.log(f"메시지 처리 중 오류: {exc}", "error")

        self._handler = _on_message

    async def stop(self) -> None:
        for task in (self._poller, self._worker):
            if task is not None:
                task.cancel()
        tasks = [task for task in (self._poller, self._worker) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------ 대화방 직접 확인
    async def _recent_messages(self, limit: int) -> List:
        try:
            messages = await self.client.get_messages(self.entity, limit=limit)
        except Exception as exc:  # noqa: BLE001
            self.console.log(f"대화방을 읽을 수 없습니다: {exc}", "warn")
            return []
        return [msg for msg in (messages or []) if msg]

    async def _poll_loop(self) -> None:
        """알림이 오지 않아도 동작하도록 대화방을 주기적으로 직접 확인한다."""
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                try:
                    messages = await self.client.get_messages(
                        self.entity, limit=30, min_id=self._last_id
                    )
                except Exception as exc:  # noqa: BLE001
                    self.console.log(f"대화방 확인 실패(무시): {exc}", "warn")
                    continue
                messages = [msg for msg in (messages or []) if msg]
                if self.debug:
                    self.console.log(
                        f"[진단] 직접 확인: 새 메시지 {len(messages)}개"
                        f" (마지막 {self._last_id})"
                    )
                for message in sorted(messages, key=lambda msg: msg.id):
                    await self.handle_message(message, source="직접 확인")
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 확인 실패로 멈추지 않는다
                self.console.log(f"대화방 확인 중 오류(무시): {exc}", "warn")

    # ------------------------------------------------------------------ 판별
    def _mark_seen(self, message_id) -> bool:
        """처음 본 메시지면 True. 알림과 직접 확인이 겹쳐도 한 번만 처리한다."""
        if message_id is None:
            return True
        if message_id in self._seen_ids:
            return False
        self._seen_ids.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > SEEN_LIMIT:
            self._seen_ids.discard(self._seen_order.popleft())
        return True

    async def handle_message(self, message, source: str = "") -> bool:
        """새 메시지를 확인해 처리할 것이면 대기열에 넣는다."""
        message_id = getattr(message, "id", None)
        if not self._mark_seen(message_id):
            return False
        if message_id is not None:
            self._last_id = max(self._last_id, message_id)

        text = getattr(message, "message", None) or getattr(message, "raw_text", None)
        if self.debug:
            preview = (text or "").replace("\n", " ")[:60]
            self.console.log(f"[진단] {source} 메시지 {message_id}: {preview!r}")

        links = links_from_message(text, bool(getattr(message, "media", None)))
        if not links:
            if is_command(text):
                await self._send(HELP_TEXT, reply_to=message_id)
            return False

        if source == "직접 확인":
            self.console.log("알림이 늦어 직접 확인해서 찾았습니다.", "warn")
        await self.queue.put((links, message_id))
        return True

    # ------------------------------------------------------------------ 처리
    async def _run_worker(self) -> None:
        while True:
            links, reply_to = await self.queue.get()
            try:
                await self.process(links, reply_to)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 건 실패로 멈추지 않는다
                self.console.log(f"요청 처리 중 오류: {exc}", "error")
            finally:
                self.queue.task_done()

    async def process(self, links: Sequence[ParsedLink], reply_to=None) -> RequestState:
        state = RequestState()
        # 텔레그램 상태 메시지가 늦거나 막혀도 다운로드는 진행돼야 하므로,
        # 먼저 화면에 알리고 시작한다.
        self.console.log(
            "요청 받음: " + " / ".join(link.describe() for link in links)
        )

        waiting = self.queue.qsize()
        first = state.line + (f"\n(대기 중인 요청 {waiting}건)" if waiting else "")
        status = await self._send(first, reply_to=reply_to)
        if status is None:
            self.console.log(
                "상태 메시지를 보낼 수 없어 화면에만 표시합니다(다운로드는 계속됩니다).",
                "warn",
            )

        reporter = RelayReporter(self.console, state)
        downloader = self.make_downloader(reporter)
        ticker = asyncio.create_task(self._ticker(status, state))
        try:
            for link in links:
                await downloader.process(link)
        finally:
            ticker.cancel()

        if self.send_back:
            for path in state.paths:
                await self._upload(status, state, path, reply_to=reply_to)

        await self._edit(status, self._summary(state))
        return state

    def _summary(self, state: RequestState) -> str:
        lines: List[str] = []
        if state.paths:
            total = sum(self._size(path) for path in state.paths)
            if self.send_back:
                lines.append(f"✅ 완료 — {len(state.paths)}개 보냈습니다 ({human_size(total)})")
                lines.append(
                    "영상을 꾹 눌러 저장하세요 — "
                    "아이폰은 '동영상 저장', 안드로이드는 '갤러리에 저장'."
                )
            else:
                lines.append(f"✅ 완료 — 컴퓨터에 {len(state.paths)}개 저장했습니다")
            for path in state.paths:
                lines.append(f"· {path.name}")
        if state.skipped:
            lines.append(f"⏭ 이미 받아둔 파일 {len(state.skipped)}개는 건너뜀")
        if state.errors:
            lines.append("⚠️ 실패:")
            lines.extend(f"· {error}" for error in state.errors)
        if not lines:
            lines.append("⚠️ 받을 동영상을 찾지 못했습니다.")
        return "\n".join(lines)[:4000]

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    # ------------------------------------------------------------------ 전송
    async def _upload(self, status, state: RequestState, path: Path, reply_to=None) -> None:
        size = self._size(path)
        state.line = f"⬆️ 휴대폰으로 보내는 중 0%\n{path.name}\n{human_size(size)}"
        sent = {"value": 0}

        def on_progress(current, total):  # Telethon 이 동기로 호출한다
            sent["value"] = current
            percent = (current / total * 100) if total else 0
            state.line = (
                f"⬆️ 휴대폰으로 보내는 중 {percent:.0f}%\n"
                f"{path.name}\n{human_size(current)} / {human_size(total or size)}"
            )

        async def send(with_reply) -> None:
            await self.client.send_file(
                self.entity,
                str(path),
                caption=path.name[:1000],  # 링크를 넣지 않는다(무한 반복 방지)
                supports_streaming=True,
                progress_callback=on_progress,
                reply_to=with_reply,
            )

        ticker = asyncio.create_task(self._ticker(status, state))
        try:
            try:
                await send(reply_to)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if reply_to is None or _is_too_big(exc):
                    raise
                # 답장으로 보내는 것이 문제일 수 있으니 답장 없이 한 번 더.
                self.console.log(
                    f"답장으로 보내지 못해 답장 없이 다시 시도합니다: {exc}", "warn"
                )
                await send(None)
            self.console.log(f"휴대폰으로 전송 완료: {path.name}", "ok")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            if _is_too_big(exc):
                message = (
                    "용량이 너무 커서 텔레그램으로 보낼 수 없습니다"
                    f"({human_size(size)}). 파일은 컴퓨터에 저장돼 있습니다."
                )
            state.errors.append(f"{path.name} 전송 실패 — {message}")
            self.console.log(f"전송 실패: {path.name} — {message}", "error")
        finally:
            ticker.cancel()

    # ------------------------------------------------------------ 상태 메시지
    async def _ticker(self, status, state: RequestState) -> None:
        """진행 상황을 일정 간격으로 텔레그램 메시지에 반영한다."""
        last = ""
        try:
            while True:
                await asyncio.sleep(self.edit_interval)
                if state.line != last:
                    last = state.line
                    await self._edit(status, state.line)
        except asyncio.CancelledError:
            pass

    async def _call(self, make_coro: Callable[[], object], what: str, quiet: bool = False):
        """상태 메시지용 호출. 늦거나 실패해도 다운로드를 막지 않는다."""
        try:
            return await asyncio.wait_for(make_coro(), timeout=self.status_timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.console.log(
                f"{what}이(가) {self.status_timeout:.0f}초 안에 끝나지 않아 건너뜁니다.",
                "warn",
            )
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self.console.log(f"{what} 실패(무시): {exc}", "warn")
            elif self.debug:
                self.console.log(f"[진단] {what} 실패: {exc}")
        return None

    async def _send(self, text: str, reply_to=None):
        status = await self._call(
            lambda: self.client.send_message(self.entity, text, reply_to=reply_to),
            "상태 메시지 보내기",
        )
        if status is None and reply_to is not None:
            # 답장으로 보내는 것이 문제일 수 있으니 답장 없이 한 번 더 시도한다.
            status = await self._call(
                lambda: self.client.send_message(self.entity, text),
                "상태 메시지 보내기(답장 없이)",
            )
        return status

    async def _edit(self, status, text: str) -> None:
        if status is None:
            return
        # 내용이 같거나 제한에 걸린 경우가 흔하므로 조용히 넘어간다.
        await self._call(
            lambda: self.client.edit_message(self.entity, status.id, text[:4000]),
            "상태 메시지 수정",
            quiet=True,
        )


async def run_listen(
    client,
    console: Reporter,
    make_downloader: Callable[[Reporter], object],
    chat: str = "me",
    send_back: bool = True,
    hello: bool = True,
    poll_interval: float = POLL_INTERVAL,
    catch_up: int = 0,
    debug: bool = False,
) -> None:
    """감시를 시작하고 연결이 끊어질 때까지 기다린다."""
    service = ListenService(
        client,
        console,
        make_downloader,
        chat=chat,
        send_back=send_back,
        poll_interval=poll_interval,
        catch_up=catch_up,
        debug=debug,
    )
    await service.start()

    where = "저장한 메시지" if chat == "me" else str(chat)
    console.log(f"텔레그램 감시를 시작했습니다: {where}", "ok")
    console.log("휴대폰에서 그 대화방에 링크를 붙여넣으면 자동으로 받습니다.")
    if service.poll_interval:
        console.log(
            f"알림이 오지 않아도 {service.poll_interval:.0f}초마다 대화방을 직접 확인합니다."
        )
    console.log("이 창을 닫거나 Ctrl+C 를 누르면 멈춥니다.")

    if hello:
        sent = await service._send(
            "✅ 다운로더가 켜졌습니다.\n여기에 텔레그램 동영상 링크를 붙여넣으세요."
        )
        if sent is None:
            console.log(
                "시작 알림을 보내지 못했습니다. 대화방을 잘못 지정했을 수 있습니다.", "warn"
            )
        else:
            console.log(
                f"'{where}' 로 시작 알림을 보냈습니다. 휴대폰에서 확인해 보세요.", "ok"
            )

    try:
        await client.run_until_disconnected()
    finally:
        await service.stop()
