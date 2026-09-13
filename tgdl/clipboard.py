"""클립보드 읽기.

pyperclip 이 있으면 그걸 쓰고, 없으면 OS 기본 명령(pbpaste / wl-paste /
xclip / xsel / powershell)을 찾아 쓴다.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, List, Optional

_CANDIDATES: List[List[str]] = [
    ["pbpaste"],  # macOS
    ["wl-paste", "--no-newline"],  # Linux / Wayland
    ["xclip", "-selection", "clipboard", "-o"],  # Linux / X11
    ["xsel", "--clipboard", "--output"],
    ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],  # Windows
]

_backend: Optional[Callable[[], Optional[str]]] = None
_checked = False


class ClipboardUnavailable(RuntimeError):
    """클립보드를 읽을 방법이 없을 때."""


def _pyperclip_backend() -> Optional[Callable[[], Optional[str]]]:
    try:
        import pyperclip  # type: ignore
    except Exception:
        return None

    def read() -> Optional[str]:
        try:
            return pyperclip.paste()
        except Exception:
            return None

    try:
        pyperclip.paste()
    except Exception:
        return None
    return read


def _command_backend() -> Optional[Callable[[], Optional[str]]]:
    for command in _CANDIDATES:
        if not shutil.which(command[0]):
            continue

        def read(cmd=command) -> Optional[str]:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=5, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode != 0:
                return None
            return proc.stdout.decode("utf-8", errors="replace")

        if read() is not None:
            return read
    return None


def get_backend() -> Callable[[], Optional[str]]:
    global _backend, _checked
    if not _checked:
        _backend = _pyperclip_backend() or _command_backend()
        _checked = True
    if _backend is None:
        raise ClipboardUnavailable(
            "클립보드를 읽을 수 없습니다.\n"
            "  · 해결 1: pip install pyperclip\n"
            "  · 해결 2(리눅스): sudo apt install xclip  (또는 wl-clipboard)\n"
            "  · 또는 클립보드 감시 대신 링크를 직접 붙여넣는 기본 모드를 쓰세요."
        )
    return _backend


def read_clipboard() -> Optional[str]:
    return get_backend()()
