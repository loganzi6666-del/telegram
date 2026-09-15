"""컴퓨터 전원 다루기 — 절전·종료·재부팅·화면 잠금.

운영체제마다 명령이 다르므로 한곳에 모아 둔다. 실제로 실행하는 부분은
``run`` 하나뿐이어서 시험할 때는 가짜 실행기를 끼워 넣을 수 있다.

**켜기는 여기 없다.** 꺼진 컴퓨터는 어떤 프로그램도 돌릴 수 없으므로 자기 자신을
켤 수 없다. 켜는 일은 같은 공유기에 붙은 다른 기기가 매직 패킷을 보내 주는
:mod:`tgdl.wake` 가 담당한다.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List

WINDOWS = os.name == "nt"
MACOS = platform.system() == "Darwin"


class PowerError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


@dataclass
class Action:
    key: str
    label: str  # "절전" 처럼 사람이 읽는 이름
    argv: List[str] = field(default_factory=list)
    danger: bool = True  # 확인 절차가 필요한가
    note: str = ""

    def describe(self) -> str:
        return self.label


#: 운영체제별 명령. 값은 (사람이 읽는 이름, 명령, 확인 필요, 덧붙일 설명).
_TABLE: Dict[str, Dict[str, tuple]] = {
    "windows": {
        # 최대 절전이 켜져 있으면 SetSuspendState 가 절전 대신 최대 절전으로
        # 들어간다. 그러면 깨우기(WoL)가 안 되는 메인보드가 많아 README 에서
        # `powercfg /hibernate off` 를 안내한다.
        "sleep": ("절전", ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], True, ""),
        "shutdown": ("종료", ["shutdown", "/s", "/t", "0"], True, ""),
        "reboot": ("재부팅", ["shutdown", "/r", "/t", "0"], True, ""),
        "lock": ("화면 잠금", ["rundll32.exe", "user32.dll,LockWorkStation"], False, ""),
    },
    "darwin": {
        "sleep": ("절전", ["pmset", "sleepnow"], True, ""),
        "shutdown": ("종료", ["osascript", "-e", 'tell app "System Events" to shut down'], True, ""),
        "reboot": ("재부팅", ["osascript", "-e", 'tell app "System Events" to restart'], True, ""),
        "lock": ("화면 잠금", ["pmset", "displaysleepnow"], False, ""),
    },
    "linux": {
        "sleep": ("절전", ["systemctl", "suspend"], True, ""),
        "shutdown": ("종료", ["systemctl", "poweroff"], True, ""),
        "reboot": ("재부팅", ["systemctl", "reboot"], True, ""),
        "lock": ("화면 잠금", ["loginctl", "lock-sessions"], False, ""),
    },
}


def platform_key() -> str:
    if WINDOWS:
        return "windows"
    if MACOS:
        return "darwin"
    return "linux"


def plan(key: str) -> Action:
    """이 컴퓨터에서 그 동작을 어떤 명령으로 수행할지 결정한다."""
    table = _TABLE[platform_key()]
    entry = table.get(key)
    if entry is None:
        raise PowerError(f"이 컴퓨터에서는 '{key}' 를 지원하지 않습니다.")
    label, argv, danger, note = entry
    return Action(key=key, label=label, argv=list(argv), danger=danger, note=note)


def run(action: Action, runner: Callable[[List[str]], object] = None) -> None:
    """전원 명령을 실제로 실행한다.

    실패하면 :class:`PowerError` 를 낸다. 리눅스에서 권한이 없으면
    `systemctl` 이 오류를 내므로 그 내용을 그대로 전달한다.
    """
    if runner is not None:
        runner(action.argv)
        return
    try:
        done = subprocess.run(
            action.argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        raise PowerError(f"{action.label} 명령을 찾을 수 없습니다: {action.argv[0]}")
    except (OSError, subprocess.SubprocessError) as exc:
        raise PowerError(f"{action.label} 실행 실패: {exc}")
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"종료 코드 {done.returncode}"
        raise PowerError(f"{action.label} 실패 — {reason}")


def uptime_seconds() -> float:
    """컴퓨터가 켜진 뒤 지난 시간(초). 알 수 없으면 0."""
    if not WINDOWS:
        try:
            with open("/proc/uptime", encoding="utf-8") as handle:
                return float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            pass
    try:  # 윈도우·맥 공통으로 쓸 수 있는 방법
        import time

        if WINDOWS:  # pragma: no cover - 윈도우에서만
            import ctypes

            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        done = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, check=False
        )
        for part in (done.stdout or "").replace(",", " ").split():
            if part.isdigit() and len(part) >= 10:
                return max(0.0, time.time() - int(part))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        pass
    return 0.0
