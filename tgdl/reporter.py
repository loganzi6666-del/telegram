"""진행 상황 출력.

CLI 는 :class:`ConsoleReporter`, GUI 는 자체 Reporter 를 쓴다. 다운로더는
이 인터페이스만 알고 있으면 된다.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import Dict


def human_size(num: float) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def human_time(seconds: float) -> str:
    if not seconds or seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Reporter:
    """아무것도 하지 않는 기본 Reporter."""

    def log(self, message: str, level: str = "info") -> None:  # pragma: no cover
        pass

    def progress(self, key: str, label: str, done: int, total: int) -> None:
        pass

    def finished(self, key: str, label: str, path, skipped: bool = False) -> None:
        pass

    def failed(self, key: str, label: str, error: str) -> None:
        pass


class ConsoleReporter(Reporter):
    """터미널 출력. 동시 다운로드가 1개면 한 줄을 갱신하고, 여러 개면 단계별로 찍는다."""

    PREFIX = {"info": "", "ok": "✔ ", "warn": "⚠ ", "error": "✖ "}

    #: 여러 줄 모드에서 진행 상황을 다시 찍는 최소 간격(초)
    MULTILINE_INTERVAL = 5.0

    def __init__(self, single_line: bool = True, stream=None) -> None:
        self.stream = stream or sys.stderr
        self.single_line = bool(single_line) and getattr(self.stream, "isatty", lambda: False)()
        self._state: Dict[str, dict] = {}
        self._dirty = False

    # -- 내부 --------------------------------------------------------------
    def _clear(self) -> None:
        if self._dirty:
            width = shutil.get_terminal_size((80, 20)).columns
            self.stream.write("\r" + " " * (width - 1) + "\r")
            self._dirty = False

    def _write_line(self, text: str) -> None:
        width = shutil.get_terminal_size((80, 20)).columns
        self.stream.write("\r" + text[: max(10, width - 1)])
        self.stream.flush()
        self._dirty = True

    # -- 인터페이스 --------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self._clear()
        print(self.PREFIX.get(level, "") + message, file=self.stream, flush=True)

    def progress(self, key: str, label: str, done: int, total: int) -> None:
        now = time.monotonic()
        state = self._state.setdefault(key, {"t0": now, "base": done, "last": 0.0, "step": -1})
        finished = bool(total) and done >= total

        if self.single_line:
            if now - state["last"] < 0.2 and not finished:
                return
        else:
            # 줄을 새로 찍는 방식: 10% 단위마다, 그리고 최소 몇 초마다 한 번씩
            # (큰 파일에서 10%를 채우는 데 오래 걸려 멈춘 것처럼 보이지 않도록)
            step = int((done / total) * 10) if total else 0
            if (
                step <= state["step"]
                and now - state["last"] < self.MULTILINE_INTERVAL
                and not finished
            ):
                return
            state["step"] = max(step, state["step"])
        state["last"] = now

        elapsed = max(now - state["t0"], 1e-6)
        speed = max(done - state["base"], 0) / elapsed
        percent = (done / total * 100) if total else 0.0
        eta = (total - done) / speed if speed > 0 and total else 0
        line = (
            f"  {label}  {human_size(done)}/{human_size(total)}"
            f"  {percent:5.1f}%  {human_size(speed)}/s  남은시간 {human_time(eta)}"
        )
        if self.single_line:
            self._write_line(line)
        else:
            print(line, file=self.stream, flush=True)

    def finished(self, key: str, label: str, path, skipped: bool = False) -> None:
        self._state.pop(key, None)
        self._clear()
        if skipped:
            print(f"⏭ 이미 있음: {label}", file=self.stream, flush=True)
        else:
            print(f"✔ 완료: {path}", file=self.stream, flush=True)

    def failed(self, key: str, label: str, error: str) -> None:
        self._state.pop(key, None)
        self._clear()
        print(f"✖ 실패: {label} — {error}", file=self.stream, flush=True)
