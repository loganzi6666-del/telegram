"""텔레그램 원격 조종 명령 테스트(가짜 대화방·가짜 전원). `python tests/test_control.py`"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HOME = tempfile.mkdtemp(prefix="tgdl-test-home-")
os.environ["TGDL_HOME"] = _HOME

from tgdl.control import (  # noqa: E402
    CONTROL_HELP,
    ControlRouter,
    WakeRouter,
    parse_command,
)
from tgdl.program import Program, ProgramError, ProgramManager, Status  # noqa: E402
from tgdl.reporter import Reporter  # noqa: E402

ME = 12345
STRANGER = 99999


class FakeManager(ProgramManager):
    """실제 프로세스를 띄우지 않는 관리자."""

    def __init__(self, running: bool = False, names=("매매봇",), broken: str = ""):
        super().__init__([Program(name=name, command=f"{name}.py") for name in names])
        self.running = running
        self.broken = broken
        self.calls: list = []

    def _status(self, name):
        return Status(name, self.running, pid=4242 if self.running else 0, since=1.0)

    def status(self, name=None):
        program = self.find(name)
        return self._status(program.name)

    def status_all(self):
        return [self._status(name) for name in self.names]

    def start(self, name=None):
        program = self.find(name)
        self.calls.append(("start", program.name))
        if self.broken:
            raise ProgramError(self.broken)
        if self.running:
            status = self._status(program.name)
            status.note = "이미 켜져 있습니다"
            return status
        self.running = True
        return self._status(program.name)

    def stop(self, name=None, grace=None):
        program = self.find(name)
        self.calls.append(("stop", program.name))
        if self.broken:
            raise ProgramError(self.broken)
        self.running = False
        status = self._status(program.name)
        status.note = "정상적으로 멈췄습니다"
        return status

    def restart(self, name=None):
        self.stop(name)
        return self.start(name)

    def tail(self, name=None, lines=20):
        self.find(name)
        self.calls.append(("tail", lines))
        return "\n".join(f"기록 {index}" for index in range(lines))


class Chat:
    """되돌려 보낸 메시지를 모아 두는 가짜 대화방."""

    def __init__(self):
        self.messages: list = []

    async def reply(self, text: str) -> None:
        self.messages.append(text)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ""

    @property
    def joined(self) -> str:
        return "\n".join(self.messages)


def make_router(manager=None, allow_power=False, **kwargs) -> tuple:
    manager = manager or FakeManager()
    ran: list = []
    router = ControlRouter(
        manager,
        console=Reporter(),
        allow_power=allow_power,
        owner_ids=(ME,),
        power_delay=0.0,
        power_runner=lambda argv: ran.append(argv),
        **kwargs,
    )
    return router, manager, ran


def send(router, text: str, chat: Chat = None, sender=ME) -> tuple:
    chat = chat or Chat()
    handled = asyncio.run(router.handle(text, sender_id=sender, reply=chat.reply))
    return handled, chat


# ------------------------------------------------------------------ 명령 해석
def test_parse_command():
    assert parse_command("/로그 50") == ("로그", ["50"])
    assert parse_command("  /상태  ") == ("상태", [])
    assert parse_command("/상태@내봇") == ("상태", [])
    assert parse_command("안녕") == ("", [])
    assert parse_command("") == ("", [])


def test_plain_text_is_not_a_command():
    router, _manager, _ran = make_router()
    handled, chat = send(router, "https://t.me/c/123/45")
    assert not handled, "링크는 다운로더가 처리해야 한다"
    assert chat.messages == []


def test_unknown_command_shows_help():
    router, _manager, _ran = make_router()
    handled, chat = send(router, "/아무거나")
    assert handled
    assert "모르는 명령" in chat.last


def test_unknown_command_passes_through_in_listen_mode():
    """다운로더와 같이 쓸 때는 모르는 명령을 다운로더 도움말에 넘긴다."""
    router, _manager, _ran = make_router(fallback_help=False)
    handled, chat = send(router, "/아무거나")
    assert not handled
    assert chat.messages == []


def test_help_lists_commands():
    router, _manager, _ran = make_router()
    _handled, chat = send(router, "/도움")
    assert chat.last == CONTROL_HELP
    assert "/중지" in chat.last


# ------------------------------------------------------------------ 매매봇
def test_start_and_stop_bot():
    router, manager, _ran = make_router()
    _handled, chat = send(router, "/시작")
    assert ("start", "매매봇") in manager.calls
    assert "켰습니다" in chat.last

    _handled, chat = send(router, "/중지")
    assert ("stop", "매매봇") in manager.calls
    assert "멈췄습니다" in chat.last


def test_english_and_short_aliases():
    for text, expected in (("/on", "start"), ("/off", "stop"), ("/restart", "start")):
        router, manager, _ran = make_router()
        send(router, text)
        assert any(call[0] == expected for call in manager.calls), text


def test_start_twice_says_already_running():
    router, _manager, _ran = make_router(FakeManager(running=True))
    _handled, chat = send(router, "/시작")
    assert "이미" in chat.last


def test_status_shows_running_state():
    router, _manager, _ran = make_router(FakeManager(running=True))
    _handled, chat = send(router, "/상태")
    assert "매매봇" in chat.last
    assert "실행 중" in chat.last


def test_log_takes_line_count():
    router, manager, _ran = make_router()
    _handled, chat = send(router, "/로그 7")
    assert ("tail", 7) in manager.calls
    assert "기록 6" in chat.last


def test_log_escapes_telegram_formatting():
    """로그에 ` 나 ** 가 있으면 텔레그램 서식으로 해석돼 글자가 사라진다."""

    class Backticks(FakeManager):
        def tail(self, name=None, lines=20):
            self.find(name)
            return "예외: `주문실패` **중요**"

    router, _manager, _ran = make_router(Backticks())
    _handled, chat = send(router, "/로그")
    assert "`" not in chat.last
    assert "**" not in chat.last
    assert "주문실패" in chat.last


def test_program_error_is_reported_not_raised():
    router, _manager, _ran = make_router(FakeManager(broken="실행 파일이 없습니다"))
    handled, chat = send(router, "/시작")
    assert handled
    assert "실행 파일이 없습니다" in chat.last


def test_no_program_registered_gives_hint():
    router, _manager, _ran = make_router(ProgramManager([]))
    _handled, chat = send(router, "/시작")
    assert "bot add" in chat.last


# ------------------------------------------------------------------ 권한
def test_stranger_cannot_run_commands():
    router, manager, ran = make_router(FakeManager(running=True), allow_power=True)
    _handled, chat = send(router, "/종료", sender=STRANGER)
    assert "주인만" in chat.last
    assert ran == [], "남이 보낸 전원 명령이 실행됐다"
    assert manager.calls == []


def test_owner_list_empty_allows_everyone_in_saved_messages():
    """'저장한 메시지'는 나 혼자만 쓰는 방이므로 번호를 못 구해도 동작해야 한다."""
    manager = FakeManager()
    router = ControlRouter(manager, console=Reporter(), owner_ids=())
    handled, _chat = send(router, "/상태", sender=None)
    assert handled


# ------------------------------------------------------------------ 전원
def test_power_disabled_by_default():
    router, _manager, ran = make_router()
    _handled, chat = send(router, "/종료")
    assert "꺼져 있습니다" in chat.last
    assert ran == []


def test_power_needs_confirmation():
    router, _manager, ran = make_router(allow_power=True)
    chat = Chat()
    send(router, "/종료", chat)
    assert "확인" in chat.last
    assert ran == [], "확인 전에 전원을 내렸다"

    send(router, "/확인", chat)
    assert ran and ran[0], "확인했는데 전원 명령이 실행되지 않았다"
    assert "종료 합니다" in chat.joined or "종료합니다" in chat.joined


def test_confirmation_in_one_message():
    router, _manager, ran = make_router(allow_power=True)
    _handled, chat = send(router, "/절전 확인")
    assert ran, "한 번에 확인까지 보냈는데 실행되지 않았다"


def test_confirmation_expires():
    clock = [1000.0]
    router, _manager, ran = make_router(
        allow_power=True, confirm_window=60.0, clock=lambda: clock[0]
    )
    chat = Chat()
    send(router, "/종료", chat)
    clock[0] += 120.0
    send(router, "/확인", chat)
    assert ran == [], "시간이 지난 확인으로 전원을 내렸다"
    assert "시간이 지나" in chat.last


def test_cancel_clears_pending():
    router, _manager, ran = make_router(allow_power=True)
    chat = Chat()
    send(router, "/재부팅", chat)
    send(router, "/취소", chat)
    send(router, "/확인", chat)
    assert ran == []
    assert "기다리는 명령이 없습니다" in chat.last


def test_confirm_without_pending():
    router, _manager, ran = make_router(allow_power=True)
    _handled, chat = send(router, "/확인")
    assert ran == []
    assert "없습니다" in chat.last


def test_power_stops_bot_first():
    """전원을 내리기 전에 매매봇을 부드럽게 멈춰야 한다(주문 정리)."""
    manager = FakeManager(running=True)
    router, _manager, ran = make_router(manager, allow_power=True)
    chat = Chat()
    send(router, "/종료", chat)
    assert "지금 실행 중" in chat.joined, "실행 중인 봇을 알려 줘야 한다"
    send(router, "/확인", chat)
    assert ("stop", "매매봇") in manager.calls
    assert ran, "봇을 멈춘 뒤 전원 명령이 실행돼야 한다"
    assert manager.calls.index(("stop", "매매봇")) >= 0


def test_lock_needs_no_confirmation():
    router, _manager, ran = make_router(allow_power=True)
    _handled, chat = send(router, "/잠금")
    assert ran, "화면 잠금은 위험하지 않으므로 바로 실행한다"


def test_wake_on_running_computer_answers_already_on():
    router, _manager, _ran = make_router()
    _handled, chat = send(router, "/깨우기")
    assert "이미 켜져 있습니다" in chat.last


# ------------------------------------------------------------------ 깨우기 담당 기기
def test_wake_router_sends_magic_packet():
    sent: list = []
    router = WakeRouter(
        "a1-b2-c3-d4-e5-f6",
        console=Reporter(),
        owner_ids=(ME,),
        sender=lambda packet, address, port: sent.append((packet, address, port)),
    )
    handled, chat = send(router, "/깨우기")
    assert handled
    assert sent, "매직 패킷을 보내지 않았다"
    packet = sent[0][0]
    assert packet.startswith(b"\xff" * 6)
    assert len(packet) == 102
    assert "보냈습니다" in chat.joined


def test_wake_router_ignores_other_commands():
    """집 컴퓨터가 대답할 명령에는 끼어들지 않는다(두 번 답하지 않도록)."""
    router = WakeRouter("a1b2c3d4e5f6", console=Reporter(), owner_ids=(ME,), sender=lambda *a: None)
    for text in ("/상태", "/시작", "/종료", "/로그"):
        handled, chat = send(router, text)
        assert not handled, text
        assert chat.messages == []


def test_wake_router_stays_silent_when_already_awake():
    sent: list = []
    router = WakeRouter(
        "a1b2c3d4e5f6",
        host="192.168.0.10",
        console=Reporter(),
        owner_ids=(ME,),
        sender=lambda *args: sent.append(args),
    )
    router._awake_now = lambda: True
    _handled, chat = send(router, "/깨우기")
    assert sent == [], "이미 켜져 있는데 패킷을 보냈다"
    assert chat.messages == []


def test_wake_router_reports_when_it_does_not_wake():
    router = WakeRouter(
        "a1b2c3d4e5f6",
        host="192.168.0.10",
        console=Reporter(),
        owner_ids=(ME,),
        sender=lambda *args: None,
        waiter=lambda: False,
    )
    router._awake_now = lambda: False
    _handled, chat = send(router, "/깨우기")
    assert "응답이 없습니다" in chat.joined
    assert "Wake on LAN" in chat.joined, "무엇을 확인해야 하는지 알려 줘야 한다"


def test_wake_router_confirms_success():
    router = WakeRouter(
        "a1b2c3d4e5f6",
        host="192.168.0.10",
        console=Reporter(),
        owner_ids=(ME,),
        sender=lambda *args: None,
        waiter=lambda: True,
    )
    router._awake_now = lambda: False
    _handled, chat = send(router, "/깨우기")
    assert "켜졌습니다" in chat.joined


def test_wake_router_ignores_strangers():
    sent: list = []
    router = WakeRouter(
        "a1b2c3d4e5f6",
        console=Reporter(),
        owner_ids=(ME,),
        sender=lambda *args: sent.append(args),
    )
    _handled, chat = send(router, "/깨우기", sender=STRANGER)
    assert sent == []
    assert chat.messages == []


# ------------------------------------------------- 전체 흐름(가짜 대화방 + 실제 프로세스)
class TinyClient:
    """ListenService 가 쓰는 부분만 흉내 낸 텔레그램 클라이언트."""

    def __init__(self):
        self.sent: list = []

    async def get_entity(self, chat):
        return "저장한 메시지"

    async def get_messages(self, entity, limit=None, min_id=None):
        return []

    async def send_message(self, entity, text, reply_to=None):
        self.sent.append(text)

        class Sent:
            id = len(self.sent)

        return Sent()

    def on(self, event):  # 실제 연결에서만 쓰인다
        return lambda func: func


def test_end_to_end_start_status_stop_through_chat():
    """휴대폰에서 `/시작` → `/상태` → `/중지` 를 보낸 것과 같은 흐름."""
    from tgdl.listen import ListenService
    from tgdl.program import Program, ProgramManager

    class Message:
        def __init__(self, mid, text):
            self.id = mid
            self.message = text
            self.raw_text = text
            self.media = None
            self.sender_id = ME

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "bot.py")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(
                    "import signal, sys, time\n"
                    "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
                    "print('매매 시작', flush=True)\n"
                    "while True: time.sleep(0.2)\n"
                )
            manager = ProgramManager(
                [Program(name="매매봇", command=script, grace=3.0, log=os.path.join(tmp, "bot.log"))]
            )
            router = ControlRouter(
                manager, console=Reporter(), owner_ids=(ME,), allow_power=False
            )
            client = TinyClient()
            service = ListenService(
                client, Reporter(), None, chat="me", commands=router, handle_links=False
            )
            service.entity = "저장한 메시지"

            await service.handle_message(Message(1, "/시작"))
            assert manager.status().running, "명령으로 봇이 켜지지 않았다"
            try:
                await service.handle_message(Message(2, "/상태"))
                assert any("실행 중" in text for text in client.sent), client.sent
                await service.handle_message(Message(3, "/로그 5"))
                assert any("매매 시작" in text for text in client.sent), client.sent
            finally:
                await service.handle_message(Message(4, "/중지"))
            assert not manager.status().running, "명령으로 봇이 멈추지 않았다"
            assert any("멈췄습니다" in text for text in client.sent), client.sent

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
