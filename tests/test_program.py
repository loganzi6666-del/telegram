"""매매봇 켜기/끄기 테스트(실제 프로세스를 띄운다). `python tests/test_program.py`"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 프로그램 상태·로그가 개인 설정 폴더를 건드리지 않게 따로 둔다.
_HOME = tempfile.mkdtemp(prefix="tgdl-test-home-")
os.environ["TGDL_HOME"] = _HOME

from tgdl.program import (  # noqa: E402
    Program,
    ProgramError,
    ProgramManager,
    human_uptime,
    load_programs,
    pid_alive,
)

#: Ctrl+C 를 받으면 정리 기록을 남기고 끝나는 가짜 매매봇
BOT = """
import signal, sys, time
def bye(signum, frame):
    print("정리 완료", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, bye)
signal.signal(signal.SIGTERM, bye)
print("시작", flush=True)
while True:
    time.sleep(0.2)
"""

#: 신호를 모두 무시하는 고집스러운 프로그램(강제 종료까지 가는지 확인용)
STUBBORN = """
import signal, time
for name in ("SIGINT", "SIGTERM"):
    try:
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    except Exception:
        pass
print("고집", flush=True)
while True:
    time.sleep(0.2)
"""

#: 곧바로 죽는 프로그램
CRASHER = """
import sys
print("설정 파일이 없습니다", flush=True)
sys.exit(3)
"""


def write(tmp: str, name: str, body: str) -> str:
    path = Path(tmp, name)
    path.write_text(body, encoding="utf-8")
    return str(path)


def manager_for(tmp: str, body: str = BOT, name: str = "매매봇", grace: float = 3.0):
    script = write(tmp, f"{name}.py", body)
    program = Program(name=name, command=script, grace=grace, log=str(Path(tmp, f"{name}.log")))
    return ProgramManager([program]), program


def test_start_stop_leaves_clean_state():
    with tempfile.TemporaryDirectory() as tmp:
        manager, program = manager_for(tmp)
        status = manager.start()
        try:
            assert status.running, "시작 직후 실행 중이어야 한다"
            assert pid_alive(status.pid)
            assert manager.status().pid == status.pid
        finally:
            done = manager.stop()
        assert not done.running, "멈춘 뒤에는 꺼져 있어야 한다"
        assert not pid_alive(status.pid)
        assert not program.state_path.exists(), "상태 파일이 지워져야 한다"
        assert "정리 완료" in program.log_path.read_text(encoding="utf-8"), (
            "부드럽게 멈춰서 봇이 정리할 기회를 줘야 한다"
        )


def test_start_twice_does_not_run_two_copies():
    """매매봇이 두 개 돌면 주문이 두 번 나간다. 반드시 막아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp)
        first = manager.start()
        try:
            second = manager.start()
            assert second.pid == first.pid, "두 번째 시작이 새 프로세스를 띄웠다"
            assert second.note == "이미 켜져 있습니다"
        finally:
            manager.stop()


def test_stop_when_already_stopped():
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp)
        status = manager.stop()
        assert not status.running
        assert "이미" in status.note


def test_force_kill_when_signals_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp, STUBBORN, name="고집봇", grace=1.0)
        status = manager.start()
        try:
            done = manager.stop()
        except ProgramError:
            manager._signal_kill(status.pid)
            raise
        assert not done.running
        assert not pid_alive(status.pid)


def test_crash_at_start_is_reported_with_log():
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp, CRASHER, name="고장봇")
        try:
            manager.start()
        except ProgramError as exc:
            assert "설정 파일이 없습니다" in str(exc), "로그를 함께 보여 줘야 한다"
        else:
            raise AssertionError("바로 죽은 프로그램을 성공으로 보고했다")
        assert not manager.status().running


def test_restart_gives_new_process():
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp)
        first = manager.start()
        try:
            again = manager.restart()
            assert again.running
            assert again.pid != first.pid, "재시작은 새 프로세스여야 한다"
        finally:
            manager.stop()


def test_stale_state_file_is_ignored():
    """컴퓨터가 갑자기 꺼져 상태 파일만 남은 경우, 꺼진 것으로 봐야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        manager, program = manager_for(tmp)
        program.state_path.write_text(
            '{"pid": 999999, "started": 1, "image": "python3"}', encoding="utf-8"
        )
        status = manager.status()
        assert not status.running
        assert not program.state_path.exists(), "낡은 상태 파일은 치워야 한다"


def test_reused_pid_is_not_mistaken_for_running():
    """번호(PID)가 다른 프로그램에 재배정된 경우를 실행 중으로 착각하면 안 된다."""
    with tempfile.TemporaryDirectory() as tmp:
        manager, program = manager_for(tmp)
        program.state_path.write_text(
            f'{{"pid": {os.getpid()}, "started": 1, "image": "전혀다른프로그램.exe"}}',
            encoding="utf-8",
        )
        assert not manager.status().running


def test_tail_returns_last_lines():
    with tempfile.TemporaryDirectory() as tmp:
        manager, program = manager_for(tmp)
        program.log_path.write_text(
            "\n".join(f"줄 {index}" for index in range(1, 501)) + "\n", encoding="utf-8"
        )
        text = manager.tail(lines=5)
        assert text.splitlines() == ["줄 496", "줄 497", "줄 498", "줄 499", "줄 500"]
        assert manager.tail(lines=3).count("\n") == 2


def test_tail_without_log():
    with tempfile.TemporaryDirectory() as tmp:
        manager, _program = manager_for(tmp)
        assert "없습니다" in manager.tail()


def test_argv_adds_python_for_py_files():
    program = Program(command="/집/매매봇/bot.py --실거래")
    argv = program.argv()
    assert argv[0] == sys.executable
    assert argv[1:] == ["/집/매매봇/bot.py", "--실거래"]


def test_argv_keeps_plain_commands():
    program = Program(command=["python3", "-c", "print(1)"])
    assert program.argv() == ["python3", "-c", "print(1)"]


def test_missing_program_gives_setup_hint():
    manager = ProgramManager([])
    assert manager.empty
    try:
        manager.status()
    except ProgramError as exc:
        assert "bot add" in str(exc), "등록 방법을 안내해야 한다"
    else:
        raise AssertionError("등록된 프로그램이 없는데 오류를 내지 않았다")


def test_find_by_partial_name_and_unknown():
    manager = ProgramManager([Program(name="매매봇", command="x.py"), Program(name="보조봇", command="y.py")])
    assert manager.find("매매").name == "매매봇"
    try:
        manager.find("없는봇")
    except ProgramError as exc:
        assert "매매봇" in str(exc)
    else:
        raise AssertionError("없는 이름을 찾아냈다")


def test_name_required_when_several_programs():
    manager = ProgramManager([Program(name="가", command="a.py"), Program(name="나", command="b.py")])
    try:
        manager.find()
    except ProgramError as exc:
        assert "여러" in str(exc)
    else:
        raise AssertionError("이름 없이도 하나를 골라 버렸다")


def test_load_programs_accepts_plain_strings():
    programs = load_programs(["/집/bot.py", {"name": "둘째", "command": "x.py"}])
    assert [program.name for program in programs] == ["봇", "둘째"]


def test_missing_file_is_reported_before_start():
    with tempfile.TemporaryDirectory() as tmp:
        program = Program(name="없음봇", command=str(Path(tmp, "없는파일.py")))
        manager = ProgramManager([program])
        try:
            manager.start()
        except ProgramError as exc:
            assert "찾을 수 없습니다" in str(exc)
        else:
            raise AssertionError("없는 파일을 실행했다고 보고했다")


def test_human_uptime():
    assert human_uptime(5) == "5초"
    assert human_uptime(125) == "2분 5초"
    assert human_uptime(3700) == "1시간 1분"
    assert human_uptime(90000) == "1일 1시간"


def test_pid_alive_on_self_and_nonsense():
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-1)
    assert not pid_alive("없음")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    started = time.monotonic()
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과 ({time.monotonic() - started:.1f}초)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
