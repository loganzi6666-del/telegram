"""자동매매 봇처럼 오래 도는 프로그램을 켜고 끄고 상태를 보는 모듈.

텔레그램 명령과 분리해 둔다. 여기서는 텔레그램을 전혀 모르고, 프로세스만
다룬다. 덕분에 터미널에서 `python -m tgdl bot start` 로 똑같이 시험할 수 있다.

중요한 원칙 두 가지.

1. **두 번 켜지 않는다.** 매매봇을 두 개 띄우면 주문이 두 번 나갈 수 있다.
   그래서 켜기 전에 반드시 살아 있는지 확인하고, 살아 있으면 켜지 않는다.
2. **부드럽게 끈다.** 곧바로 강제 종료하면 봇이 정리(주문 취소·로그 저장)를
   할 수 없다. Ctrl+C 와 같은 신호를 먼저 보내고, 정해진 시간을 기다린 뒤에만
   강제로 끝낸다.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import app_home

WINDOWS = os.name == "nt"

#: 부드럽게 멈추기를 기다리는 기본 시간(초). 매매봇이 주문을 정리할 시간이다.
DEFAULT_GRACE = 20.0

#: 윈도우에서 프로세스가 아직 살아 있을 때 나오는 종료 코드
_STILL_ACTIVE = 259


# --------------------------------------------------------------------- 프로세스
def pid_alive(pid: int) -> bool:
    """그 번호의 프로세스가 지금 살아 있는지."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 내 것이 아니지만 살아 있다
        return True
    # 리눅스: 죽었지만 아직 치우지 않은(좀비) 프로세스는 살아 있는 것이 아니다.
    state = _proc_field("stat", pid)
    if state and ") " in state:
        return state.rsplit(") ", 1)[1][:1] != "Z"
    return True


def _alive_windows(pid: int) -> bool:  # pragma: no cover - 윈도우에서만
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _proc_field(name: str, pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/{name}").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def process_image(pid: int) -> str:
    """그 프로세스가 실행 중인 파일 이름(소문자). 알 수 없으면 빈 문자열.

    번호(PID)는 프로세스가 죽으면 다른 프로그램에 다시 배정될 수 있다. 매매봇이
    이미 돌고 있다고 잘못 판단하면 정작 켜야 할 때 켜지지 않으므로, 이름까지
    맞는지 한 번 더 확인한다.
    """
    if WINDOWS:
        return _image_windows(pid)
    raw = _proc_field("cmdline", pid)
    parts = [item for item in raw.split("\0") if item]
    if not parts:
        return ""
    return Path(parts[0]).name.lower()


def _image_windows(pid: int) -> str:  # pragma: no cover - 윈도우에서만
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    line = (out or "").strip().splitlines()[:1]
    if not line or not line[0].startswith('"'):
        return ""
    return line[0].split('","')[0].strip('"').lower()


def human_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분 {seconds}초"


# ----------------------------------------------------------------------- 설정
@dataclass
class Program:
    """켜고 끌 프로그램 하나(보통 자동매매 봇)."""

    name: str = "봇"
    command: object = ""  # 문자열 또는 리스트
    cwd: str = ""
    log: str = ""  # 비우면 ~/.tgdl/logs/<이름>.log
    grace: float = DEFAULT_GRACE
    python: str = ""  # .py 를 실행할 파이썬 (비우면 지금 이 파이썬)
    stop_command: object = ""  # 봇이 자체 종료 절차를 가진 경우

    @classmethod
    def from_dict(cls, data: dict) -> "Program":
        program = cls()
        for key, value in (data or {}).items():
            if hasattr(program, key) and value not in (None, ""):
                setattr(program, key, value)
        program.name = str(program.name or "봇")
        try:
            program.grace = max(0.0, float(program.grace))
        except (TypeError, ValueError):
            program.grace = DEFAULT_GRACE
        return program

    @property
    def slug(self) -> str:
        """파일 이름으로 쓸 수 있게 다듬은 이름."""
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self.name)
        return safe.strip("_") or "program"

    @property
    def log_path(self) -> Path:
        if self.log:
            return Path(self.log).expanduser()
        folder = app_home() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.slug}.log"

    @property
    def state_path(self) -> Path:
        folder = app_home() / "state"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.slug}.json"

    def argv(self) -> List[str]:
        """실행할 명령을 낱말로 쪼갠다.

        `.py` 로 끝나면 파이썬을 앞에 붙여 준다. 설정 파일에 경로만 적어도
        동작하게 하기 위한 배려다.
        """
        return _argv(self.command, self.python)

    def stop_argv(self) -> List[str]:
        return _argv(self.stop_command, self.python)

    @property
    def target(self) -> str:
        """실행할 파일 자체(파이썬을 붙이기 전의 첫 낱말)."""
        parts = _split(self.command)
        return parts[0] if parts else ""


def _split(command: object) -> List[str]:
    if not command:
        return []
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command if str(item)]
    text = str(command).strip()
    if WINDOWS:
        # 윈도우 경로의 역슬래시를 탈출문자로 오해하지 않도록 posix=False.
        return [item.strip('"') for item in shlex.split(text, posix=False)]
    return shlex.split(text)


def _argv(command: object, python: str = "") -> List[str]:
    parts = _split(command)
    if not parts:
        return []
    first = parts[0]
    if first.lower().endswith(".py"):
        runner = python or sys.executable or "python"
        parts = [runner, first] + parts[1:]
    return parts


# ----------------------------------------------------------------------- 상태
@dataclass
class Status:
    name: str
    running: bool
    pid: int = 0
    since: float = 0.0
    log: str = ""
    note: str = ""

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.since) if (self.running and self.since) else 0.0

    def line(self) -> str:
        if self.running:
            text = f"🟢 {self.name} 실행 중"
            if self.since:
                text += f" · {human_uptime(self.uptime)} 경과"
            text += f" (PID {self.pid})"
            return text + (f" — {self.note}" if self.note else "")
        return f"🔴 {self.name} 꺼져 있음" + (f" — {self.note}" if self.note else "")


class ProgramError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


# ------------------------------------------------------------------- 관리자
class ProgramManager:
    """설정에 적힌 프로그램들을 켜고 끄고 상태를 본다."""

    def __init__(self, programs: Sequence[Program] = ()) -> None:
        self.programs: Dict[str, Program] = {}
        for program in programs:
            if program.command or program.stop_command:
                self.programs[program.name] = program
        #: 이 과정에서 직접 띄운 자식들(좀비로 남지 않게 치우기 위해 들고 있는다)
        self._children: Dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------- 찾기
    @property
    def names(self) -> List[str]:
        return list(self.programs)

    @property
    def empty(self) -> bool:
        return not self.programs

    def find(self, name: Optional[str] = None) -> Program:
        if self.empty:
            raise ProgramError(
                "켜고 끌 프로그램이 등록되지 않았습니다.\n"
                "컴퓨터에서 한 번만 등록해 주세요:\n"
                "  python -m tgdl bot add \"C:\\매매봇\\bot.py\""
            )
        if not name:
            if len(self.programs) > 1:
                raise ProgramError(
                    "프로그램이 여러 개입니다. 이름을 함께 보내 주세요: "
                    + " · ".join(self.names)
                )
            return next(iter(self.programs.values()))
        wanted = name.strip().lower()
        for program in self.programs.values():
            if program.name.lower() == wanted:
                return program
        for program in self.programs.values():  # 앞부분만 맞아도 찾아 준다
            if program.name.lower().startswith(wanted):
                return program
        raise ProgramError(
            f"'{name}' 이라는 프로그램이 없습니다. 등록된 것: " + " · ".join(self.names)
        )

    # ------------------------------------------------------------- 상태
    def _read_state(self, program: Program) -> dict:
        try:
            data = json.loads(program.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, program: Program, data: Optional[dict]) -> None:
        path = program.state_path
        if data is None:
            try:
                path.unlink()
            except OSError:
                pass
            return
        try:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def status(self, name: Optional[str] = None) -> Status:
        program = self.find(name)
        child = self._children.get(program.name)
        if child is not None:
            child.poll()  # 끝난 자식을 치운다(좀비 방지)
        state = self._read_state(program)
        pid = int(state.get("pid") or 0)
        if not pid:
            return Status(program.name, False, log=str(program.log_path))
        if not pid_alive(pid):
            self._write_state(program, None)
            return Status(
                program.name,
                False,
                log=str(program.log_path),
                note="마지막 실행은 끝났습니다",
            )
        image = str(state.get("image") or "")
        if image:
            now = process_image(pid)
            if now and now != image:
                # 번호가 다른 프로그램에 다시 배정된 경우. 꺼진 것으로 본다.
                self._write_state(program, None)
                return Status(program.name, False, log=str(program.log_path))
        return Status(
            program.name,
            True,
            pid=pid,
            since=float(state.get("started") or 0.0),
            log=str(program.log_path),
        )

    def status_all(self) -> List[Status]:
        return [self.status(name) for name in self.names]

    # ------------------------------------------------------------- 켜기
    def start(self, name: Optional[str] = None) -> Status:
        program = self.find(name)
        current = self.status(program.name)
        if current.running:
            current.note = "이미 켜져 있습니다"
            return current

        argv = program.argv()
        if not argv:
            raise ProgramError(f"{program.name} 의 실행 명령이 설정돼 있지 않습니다.")
        target = Path(program.target)
        if target.is_absolute() and target.suffix and not target.exists():
            raise ProgramError(f"실행할 파일을 찾을 수 없습니다: {target}")

        cwd = str(Path(program.cwd).expanduser()) if program.cwd else None
        if cwd and not Path(cwd).is_dir():
            raise ProgramError(f"작업 폴더를 찾을 수 없습니다: {cwd}")

        log_path = program.log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ProgramError(f"로그 파일을 열 수 없습니다: {exc}")

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"\n===== {stamp} 텔레그램 명령으로 시작 =====\n")

        kwargs: dict = {}
        if WINDOWS:  # pragma: no cover - 윈도우에서만
            # 새 프로세스 그룹으로 띄워야 나중에 Ctrl+Break(부드러운 종료)를
            # 이 프로그램에만 보낼 수 있다.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # 새 세션으로 띄운다. 이 창을 닫아도 매매봇은 계속 돈다.
            kwargs["start_new_session"] = True

        try:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except (OSError, ValueError) as exc:
            log.close()
            raise ProgramError(f"실행하지 못했습니다: {exc}")
        finally:
            try:
                log.close()
            except OSError:
                pass

        self._children[program.name] = child
        time.sleep(0.3)  # 곧바로 죽는 경우를 잡아낸다
        if child.poll() is not None:
            tail = self.tail(program.name, 12)
            raise ProgramError(
                f"시작했지만 바로 끝났습니다 (종료 코드 {child.returncode}).\n"
                f"로그 마지막 줄:\n{tail}"
            )

        self._write_state(
            program,
            {
                "pid": child.pid,
                "started": time.time(),
                "argv": argv,
                "image": process_image(child.pid) or Path(argv[0]).name.lower(),
            },
        )
        return self.status(program.name)

    # ------------------------------------------------------------- 끄기
    def stop(self, name: Optional[str] = None, grace: Optional[float] = None) -> Status:
        program = self.find(name)
        current = self.status(program.name)
        if not current.running:
            current.note = "이미 꺼져 있습니다"
            return current

        pid = current.pid
        wait = program.grace if grace is None else max(0.0, float(grace))

        custom = program.stop_argv()
        if custom:
            try:
                subprocess.run(
                    custom,
                    cwd=str(Path(program.cwd).expanduser()) if program.cwd else None,
                    timeout=max(5.0, wait),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            if self._wait_gone(pid, wait):
                return self._stopped(program, "설정된 종료 명령으로 정리했습니다")

        # 1단계: Ctrl+C 와 같은 신호 — 봇이 스스로 정리할 기회를 준다.
        self._signal_soft(pid)
        if self._wait_gone(pid, wait):
            return self._stopped(program, "정상적으로 멈췄습니다")

        # 2단계: 종료 요청
        self._signal_term(pid)
        if self._wait_gone(pid, 10.0):
            return self._stopped(program, "종료 요청으로 멈췄습니다")

        # 3단계: 강제 종료
        self._signal_kill(pid)
        if self._wait_gone(pid, 10.0):
            return self._stopped(program, "⚠️ 강제로 끝냈습니다(정리 못 했을 수 있음)")

        raise ProgramError(
            f"{program.name}(PID {pid}) 을 멈추지 못했습니다. 컴퓨터에서 직접 확인해 주세요."
        )

    def restart(self, name: Optional[str] = None) -> Status:
        program = self.find(name)
        if self.status(program.name).running:
            self.stop(program.name)
        return self.start(program.name)

    def _stopped(self, program: Program, note: str) -> Status:
        self._write_state(program, None)
        child = self._children.pop(program.name, None)
        if child is not None:
            try:
                child.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return Status(program.name, False, log=str(program.log_path), note=note)

    def _wait_gone(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    # -- 신호 보내기 (윈도우와 나머지가 다르다) --------------------------
    def _signal_soft(self, pid: int) -> None:
        if WINDOWS:  # pragma: no cover - 윈도우에서만
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
                return
            except (OSError, AttributeError, ValueError):
                # 콘솔이 없으면 보낼 수 없다. 창 닫기 요청으로 대신한다.
                self._taskkill(pid, force=False)
                return
        try:
            os.killpg(os.getpgid(pid), signal.SIGINT)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass

    def _signal_term(self, pid: int) -> None:
        if WINDOWS:  # pragma: no cover - 윈도우에서만
            self._taskkill(pid, force=False)
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def _signal_kill(self, pid: int) -> None:
        if WINDOWS:  # pragma: no cover - 윈도우에서만
            self._taskkill(pid, force=True)
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _taskkill(pid: int, force: bool) -> None:  # pragma: no cover - 윈도우에서만
        argv = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        try:
            subprocess.run(
                argv,
                timeout=20,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # ------------------------------------------------------------- 로그
    def tail(self, name: Optional[str] = None, lines: int = 20) -> str:
        program = self.find(name)
        path = program.log_path
        lines = max(1, min(200, int(lines or 20)))
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                block = min(size, max(4096, lines * 400))
                handle.seek(size - block)
                data = handle.read()
        except OSError:
            return "(로그가 아직 없습니다)"
        text = data.decode("utf-8", "replace")
        if size > len(data):
            text = text.split("\n", 1)[-1]  # 잘린 첫 줄은 버린다
        found = [line for line in text.splitlines() if line.strip()]
        if not found:
            return "(로그가 비어 있습니다)"
        return "\n".join(found[-lines:])


def load_programs(raw: Sequence[dict]) -> List[Program]:
    """설정 파일의 목록을 :class:`Program` 으로 바꾼다."""
    programs: List[Program] = []
    for item in raw or []:
        if isinstance(item, dict):
            programs.append(Program.from_dict(item))
        elif isinstance(item, str) and item.strip():
            programs.append(Program(name="봇", command=item.strip()))
    return programs
