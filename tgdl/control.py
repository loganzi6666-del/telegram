"""텔레그램 메시지로 자동매매 봇과 컴퓨터 전원을 다루는 모드.

밖에서 휴대폰으로 `/상태` `/시작` `/중지` `/절전` 같은 짧은 메시지만 보내면
집 컴퓨터가 그대로 실행하고 결과를 되돌려 준다.

안전장치가 셋 있다.

1. **나만 쓸 수 있다.** 기본 감시 대상은 '저장한 메시지'(나 혼자만 보는 방)이고,
   그 밖의 대화방을 쓰더라도 내 계정이 보낸 명령만 실행한다.
2. **전원 명령은 확인을 받는다.** `/종료` 를 보내면 곧바로 끄지 않고, 실행 중인
   매매봇이 있는지 알려 준 뒤 한 번 더 물어본다.
3. **전원 명령은 기본으로 꺼져 있다.** `--power` 를 붙여 켠 경우만 동작한다.

컴퓨터를 **켜는** 일은 이 모드가 할 수 없다(꺼진 컴퓨터는 메시지를 받지
못한다). 집에 항상 켜져 있는 다른 기기에서 :class:`WakeRouter` 를 돌리는
`python -m tgdl waker` 가 그 일을 한다.
"""

from __future__ import annotations

import asyncio
import platform
import socket
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from .power import Action, PowerError, plan, run as run_power, uptime_seconds
from .program import ProgramError, ProgramManager, human_uptime
from .reporter import Reporter
from .wake import WakeError, normalize_mac, pretty_mac, send_magic, wait_awake

#: 확인(`/종료 확인`)을 기다리는 시간(초)
CONFIRM_WINDOW = 90.0

#: 답장이 먼저 전달되도록 전원 명령 전에 잠깐 기다리는 시간(초)
POWER_DELAY = 2.0

_YES = {"확인", "네", "예", "응", "yes", "y", "ok", "okay", "확실"}

CONTROL_HELP = (
    "🖥 집 컴퓨터 원격 조종\n\n"
    "[자동매매 봇]\n"
    "/시작 — 봇 켜기\n"
    "/중지 — 봇 끄기 (정리할 시간을 주고 부드럽게)\n"
    "/재시작 — 껐다 다시 켜기\n"
    "/상태 — 지금 돌고 있는지, 얼마나 됐는지\n"
    "/로그 — 최근 기록 20줄 (`/로그 50` 처럼 줄 수 지정 가능)\n\n"
    "[컴퓨터]\n"
    "/절전 — 절전으로 내리기 (깨우기가 빠름 · 권장)\n"
    "/종료 — 완전히 끄기\n"
    "/재부팅 — 다시 시작\n"
    "/잠금 — 화면만 잠그기\n\n"
    "전원 명령은 한 번 더 확인을 받습니다. 그때 `/확인` 을 보내세요.\n"
    "취소하려면 `/취소`."
)


def _words(text: str) -> List[str]:
    return [item for item in (text or "").replace("\n", " ").split(" ") if item]


def parse_command(text: str) -> tuple:
    """``/로그 50`` → ``("로그", ["50"])``. 명령이 아니면 ``("", [])``."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", []
    parts = _words(stripped)
    head = parts[0][1:]
    if "@" in head:  # 봇 대화방에서 쓰는 `/상태@내봇` 형태
        head = head.split("@", 1)[0]
    return head.strip().lower(), parts[1:]


@dataclass
class Pending:
    """확인을 기다리는 전원 명령."""

    action: Action
    expires: float


class ControlRouter:
    """텔레그램 명령을 해석해 실행한다(집 컴퓨터에서 동작)."""

    #: 명령 이름 → 하는 일
    ALIASES = {
        "help": ("도움", "help", "start", "명령", "명령어", "도와줘", "?"),
        "status": ("상태", "status", "확인상태", "지금"),
        "start": ("시작", "봇시작", "on", "봇on", "run", "매매시작", "켜기"),
        "stop": ("중지", "봇중지", "정지", "off", "봇off", "stop", "매매중지"),
        "restart": ("재시작", "봇재시작", "restart"),
        "log": ("로그", "log", "기록"),
        "sleep": ("절전", "sleep", "잠자기", "수면"),
        "shutdown": ("종료", "shutdown", "poweroff", "컴퓨터끄기", "전원끄기"),
        "reboot": ("재부팅", "reboot", "다시시작"),
        "lock": ("잠금", "lock", "화면잠금"),
        "wake": ("깨우기", "wake", "wakeup", "컴퓨터켜기", "전원켜기"),
        "confirm": tuple(_YES),
        "cancel": ("취소", "cancel", "no", "n", "아니"),
    }

    def __init__(
        self,
        manager: ProgramManager,
        console: Optional[Reporter] = None,
        allow_power: bool = False,
        owner_ids: Iterable[int] = (),
        fallback_help: bool = True,
        stop_bot_first: bool = True,
        confirm_window: float = CONFIRM_WINDOW,
        power_delay: float = POWER_DELAY,
        power_runner: Optional[Callable[[List[str]], object]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.manager = manager
        self.console = console or Reporter()
        self.allow_power = bool(allow_power)
        self.owner_ids = {int(value) for value in owner_ids if value}
        self.fallback_help = bool(fallback_help)
        self.stop_bot_first = bool(stop_bot_first)
        self.confirm_window = max(10.0, float(confirm_window))
        self.power_delay = max(0.0, float(power_delay))
        self.power_runner = power_runner
        self.clock = clock
        self.pending: Optional[Pending] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- 해석
    def which(self, head: str) -> str:
        for key, names in self.ALIASES.items():
            if head in names:
                return key
        return ""

    def allowed(self, sender_id) -> bool:
        """내(또는 허용된) 계정이 보낸 명령인가."""
        if not self.owner_ids:
            return True
        try:
            return int(sender_id) in self.owner_ids
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------- 처리
    async def handle(self, text: str, sender_id=None, reply=None) -> bool:
        """명령을 처리했으면 True. 우리가 모르는 메시지면 False."""
        head, args = parse_command(text)
        if not head:
            return False
        key = self.which(head)
        if not key:
            if not self.fallback_help:
                return False
            await self._say(reply, f"❓ 모르는 명령입니다: /{head}\n\n{CONTROL_HELP}")
            return True

        if not self.allowed(sender_id):
            self.console.log(f"허용되지 않은 계정({sender_id})의 명령을 무시했습니다.", "warn")
            await self._say(reply, "⛔ 이 대화방에서는 주인만 명령을 쓸 수 있습니다.")
            return True

        async with self._lock:
            try:
                await self._dispatch(key, args, reply)
            except (ProgramError, PowerError, WakeError) as exc:
                await self._say(reply, f"⚠️ {exc}")
            except Exception as exc:  # noqa: BLE001 - 한 명령 실패로 멈추지 않는다
                self.console.log(f"명령 처리 중 오류: {exc}", "error")
                await self._say(reply, f"⚠️ 처리 중 오류가 났습니다: {exc}")
        return True

    async def _dispatch(self, key: str, args: Sequence[str], reply) -> None:
        if key == "help":
            await self._say(reply, CONTROL_HELP)
        elif key == "status":
            await self._say(reply, self.status_text())
        elif key == "start":
            await self._start(args, reply)
        elif key == "stop":
            await self._stop(args, reply)
        elif key == "restart":
            await self._restart(args, reply)
        elif key == "log":
            await self._log(args, reply)
        elif key == "wake":
            await self._say(reply, "✅ 컴퓨터는 이미 켜져 있습니다.\n" + self.status_text())
        elif key == "lock":
            await self._power("lock", args, reply)
        elif key in {"sleep", "shutdown", "reboot"}:
            await self._power(key, args, reply)
        elif key == "confirm":
            await self._confirm(reply)
        elif key == "cancel":
            self.pending = None
            await self._say(reply, "취소했습니다.")

    # ------------------------------------------------------------- 매매봇
    async def _start(self, args: Sequence[str], reply) -> None:
        name = args[0] if args else None
        await self._say(reply, "⏳ 켜는 중…")
        status = await asyncio.to_thread(self.manager.start, name)
        if status.running and status.note == "이미 켜져 있습니다":
            await self._say(reply, f"ℹ️ 이미 켜져 있습니다.\n{status.line()}")
            return
        self.console.log(f"{status.name} 을 텔레그램 명령으로 시작했습니다.", "ok")
        await self._say(reply, f"✅ 켰습니다.\n{status.line()}")

    async def _stop(self, args: Sequence[str], reply) -> None:
        name = args[0] if args else None
        program = self.manager.find(name)
        await self._say(
            reply,
            f"⏳ {program.name} 을 멈추는 중… "
            f"(정리할 시간을 {program.grace:.0f}초까지 기다립니다)",
        )
        status = await asyncio.to_thread(self.manager.stop, name)
        self.console.log(f"{status.name} 을 텔레그램 명령으로 멈췄습니다.", "ok")
        await self._say(reply, f"🛑 멈췄습니다.\n{status.line()}")

    async def _restart(self, args: Sequence[str], reply) -> None:
        name = args[0] if args else None
        await self._say(reply, "⏳ 껐다 다시 켜는 중…")
        status = await asyncio.to_thread(self.manager.restart, name)
        await self._say(reply, f"🔄 다시 켰습니다.\n{status.line()}")

    async def _log(self, args: Sequence[str], reply) -> None:
        lines = 20
        name = None
        for arg in args:
            if arg.isdigit():
                lines = int(arg)
            else:
                name = arg
        text = await asyncio.to_thread(self.manager.tail, name, lines)
        program = self.manager.find(name)
        # 로그에 `나 ** 이 들어 있으면 텔레그램 서식으로 해석돼 깨진다.
        safe = text[-3500:].replace("`", "'").replace("**", "*")
        await self._say(reply, f"📄 {program.name} 최근 기록\n\n{safe}")

    # ------------------------------------------------------------- 전원
    async def _power(self, key: str, args: Sequence[str], reply) -> None:
        action = plan(key)
        if not self.allow_power:
            await self._say(
                reply,
                f"🔒 전원 명령({action.label})이 꺼져 있습니다.\n"
                "집 컴퓨터에서 `python -m tgdl remote --power` 로 실행하면 켜집니다.",
            )
            return

        if not action.danger:
            await self._run_power(action, reply)
            return

        if any(arg.strip().lower() in _YES for arg in args):
            await self._run_power(action, reply)
            return

        self.pending = Pending(action=action, expires=self.clock() + self.confirm_window)
        warning = ""
        try:
            running = [status for status in self.manager.status_all() if status.running]
        except ProgramError:
            running = []
        if running:
            names = " · ".join(status.name for status in running)
            warning = (
                f"\n⚠️ 지금 실행 중: {names}\n"
                "먼저 부드럽게 멈춘 뒤에 전원을 내립니다."
                if self.stop_bot_first
                else f"\n⚠️ 지금 실행 중: {names}"
            )
        await self._say(
            reply,
            f"❓ 컴퓨터를 **{action.label}** 합니다.{warning}\n\n"
            f"정말이면 {self.confirm_window:.0f}초 안에 `/확인` 을 보내세요. "
            "그만두려면 `/취소`.",
        )

    async def _confirm(self, reply) -> None:
        pending = self.pending
        if pending is None:
            await self._say(reply, "확인을 기다리는 명령이 없습니다.")
            return
        if self.clock() > pending.expires:
            self.pending = None
            await self._say(reply, "⌛ 시간이 지나 취소했습니다. 다시 보내 주세요.")
            return
        self.pending = None
        await self._run_power(pending.action, reply)

    async def _run_power(self, action: Action, reply) -> None:
        if self.stop_bot_first and action.key in {"sleep", "shutdown", "reboot"}:
            await self._stop_all(reply)
        self.console.log(f"텔레그램 명령으로 {action.label} 합니다.", "warn")
        await self._say(reply, f"🔌 {action.label} 합니다. 다녀오세요!")
        if self.power_delay:
            # 답장이 먼저 전달되도록 잠깐 기다린다.
            await asyncio.sleep(self.power_delay)
        try:
            await asyncio.to_thread(run_power, action, self.power_runner)
        except PowerError as exc:
            self.console.log(str(exc), "error")
            await self._say(reply, f"⚠️ {exc}")

    async def _stop_all(self, reply) -> None:
        """전원을 내리기 전에 돌고 있는 프로그램을 부드럽게 멈춘다."""
        try:
            running = [status for status in self.manager.status_all() if status.running]
        except ProgramError:
            return
        for status in running:
            try:
                await self._say(reply, f"⏳ 먼저 {status.name} 을 멈춥니다…")
                done = await asyncio.to_thread(self.manager.stop, status.name)
                await self._say(reply, f"🛑 {done.line()}")
            except ProgramError as exc:
                await self._say(reply, f"⚠️ {status.name} 을 멈추지 못했습니다: {exc}")

    # ------------------------------------------------------------- 상태
    def status_text(self) -> str:
        lines = [f"🖥 {socket.gethostname()} · {platform.system()}"]
        boot = uptime_seconds()
        if boot:
            lines.append(f"켜진 지 {human_uptime(boot)}")
        lines.append("지금 " + time.strftime("%m월 %d일 %H:%M"))
        lines.append("")
        try:
            statuses = self.manager.status_all()
        except ProgramError as exc:
            lines.append(str(exc))
            return "\n".join(lines)
        for status in statuses:
            lines.append(status.line())
        if self.allow_power:
            lines.append("")
            lines.append("전원 명령 사용 가능 (/절전 · /종료 · /재부팅)")
        return "\n".join(lines)

    async def _say(self, reply, text: str) -> None:
        if reply is None:
            self.console.log(text)
            return
        result = reply(text)
        if asyncio.iscoroutine(result):
            await result


class WakeRouter:
    """항상 켜져 있는 보조기기에서 도는, 깨우기 전용 처리기.

    `/깨우기` 만 담당하고 나머지 명령은 **조용히 넘긴다**. 집 컴퓨터가 켜져
    있으면 그쪽이 대답하므로, 같은 명령에 두 번 답하지 않게 하기 위해서다.
    """

    NAMES = ("깨우기", "wake", "wakeup", "컴퓨터켜기", "전원켜기", "켜기", "on")

    def __init__(
        self,
        mac: str,
        host: str = "",
        port: int = 0,
        broadcast: str = "",
        console: Optional[Reporter] = None,
        owner_ids: Iterable[int] = (),
        timeout: float = 120.0,
        sender: Optional[Callable[[bytes, str, int], None]] = None,
        waiter: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.mac = normalize_mac(mac)
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.broadcast = (broadcast or "").strip()
        self.console = console or Reporter()
        self.owner_ids = {int(value) for value in owner_ids if value}
        self.timeout = max(10.0, float(timeout))
        self.sender = sender
        self.waiter = waiter
        self._lock = asyncio.Lock()

    def allowed(self, sender_id) -> bool:
        if not self.owner_ids:
            return True
        try:
            return int(sender_id) in self.owner_ids
        except (TypeError, ValueError):
            return False

    async def handle(self, text: str, sender_id=None, reply=None) -> bool:
        head, _args = parse_command(text)
        if not head or head not in self.NAMES:
            return False
        if not self.allowed(sender_id):
            self.console.log(f"허용되지 않은 계정({sender_id})의 깨우기 요청 무시", "warn")
            return True
        async with self._lock:
            await self._wake(reply)
        return True

    async def _wake(self, reply) -> None:
        # 이미 켜져 있으면 조용히 넘긴다(집 컴퓨터가 직접 대답한다).
        if self.host and await asyncio.to_thread(self._awake_now):
            self.console.log("이미 켜져 있어 매직 패킷을 보내지 않았습니다.")
            return

        self.console.log(f"매직 패킷을 보냅니다 → {pretty_mac(self.mac)}", "ok")
        sent = await asyncio.to_thread(
            send_magic, self.mac, self.broadcast, (9, 7), 3, self.sender
        )
        await self._say(reply, f"📡 깨우기 신호를 보냈습니다 ({sent}번).\n곧 켜집니다…")

        if not self.host:
            await self._say(
                reply,
                "확인할 주소(--host)가 없어 켜졌는지는 확인하지 못합니다.\n"
                "1~2분 뒤에 컴퓨터 쪽 봇에게 `/상태` 를 보내 보세요.",
            )
            return

        awake = await asyncio.to_thread(self._wait_for_awake)
        if awake:
            await self._say(reply, "✅ 컴퓨터가 켜졌습니다. 이제 `/상태` 를 보내 보세요.")
        else:
            await self._say(
                reply,
                f"⚠️ {self.timeout:.0f}초 안에 응답이 없습니다.\n"
                "· 메인보드 설정(Wake on LAN)과 랜카드 전원 관리 설정을 확인하세요\n"
                "· 유선 랜이어야 하고, 윈도우의 '빠른 시작'은 꺼져 있어야 합니다",
            )

    def _awake_now(self) -> bool:
        from .wake import is_awake

        return is_awake(self.host, self.port)

    def _wait_for_awake(self) -> bool:
        if self.waiter is not None:
            return self.waiter()
        return wait_awake(self.host, self.port, timeout=self.timeout)

    async def _say(self, reply, text: str) -> None:
        if reply is None:
            self.console.log(text)
            return
        result = reply(text)
        if asyncio.iscoroutine(result):
            await result


# ------------------------------------------------------------------ 실행 진입점
async def run_control(
    client,
    console: Reporter,
    router,
    chat: str = "me",
    poll_interval: float = 15.0,
    debug: bool = False,
    greeting: str = "",
    where: str = "",
) -> None:
    """원격 조종 전용 모드. 링크는 처리하지 않고 명령만 받는다.

    감시·재확인 같은 까다로운 부분은 :class:`tgdl.listen.ListenService` 를 그대로
    쓴다(알림을 놓쳐도 주기적으로 대화방을 직접 확인한다).
    """
    from .listen import ListenService

    service = ListenService(
        client,
        console,
        None,
        chat=chat,
        send_back=False,
        poll_interval=poll_interval,
        debug=debug,
        commands=router,
        handle_links=False,
    )
    await service.start()

    place = where or ("저장한 메시지" if chat == "me" else str(chat))
    console.log(f"텔레그램 명령 대기를 시작했습니다: {place}", "ok")
    if service.poll_interval:
        console.log(
            f"알림이 오지 않아도 {service.poll_interval:.0f}초마다 대화방을 직접 확인합니다."
        )
    console.log("이 창을 닫거나 Ctrl+C 를 누르면 멈춥니다.")

    if greeting:
        sent = await service._send(greeting)
        if sent is None:
            console.log(
                "시작 알림을 보내지 못했습니다. 대화방을 잘못 지정했을 수 있습니다.", "warn"
            )

    try:
        await client.run_until_disconnected()
    finally:
        await service.stop()
