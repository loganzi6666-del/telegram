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


def shorten(text: str, max_len: int = 38) -> str:
    """긴 파일 이름을 가운데를 줄여 한 줄에 들어가게 만든다."""
    text = text or ""
    if len(text) <= max_len:
        return text
    keep = max(6, max_len - 1)
    head = keep * 2 // 3
    return text[:head] + "…" + text[-(keep - head) :]


def progress_bar(percent: float, width: int = 14, ascii_only: bool = False) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    if ascii_only:
        return "[" + "#" * filled + "-" * (width - filled) + "]"
    return "[" + "█" * filled + "░" * (width - filled) + "]"


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
    """터미널 출력.

    화면(TTY)이면 한 줄을 0.25초마다 덮어써서 **실시간**으로 보여준다. 파일이나
    파이프로 넘길 때는 줄을 새로 찍되 10% 단위 또는 5초 간격으로 줄인다.
    여러 파일을 동시에 받는 중이면 합계를 한 줄로 보여준다.
    """

    PREFIX = {"info": "", "ok": "✔ ", "warn": "⚠ ", "error": "✖ "}

    #: 화면 갱신 간격(초)
    LIVE_INTERVAL = 0.25
    #: 여러 줄 모드에서 다시 찍는 최소 간격(초)
    MULTILINE_INTERVAL = 5.0

    def __init__(self, live: bool = True, stream=None) -> None:
        self.stream = stream or sys.stderr
        self.live = bool(live) and getattr(self.stream, "isatty", lambda: False)()
        self._active: Dict[str, dict] = {}
        self._state: Dict[str, dict] = {}
        self._dirty = False
        self._last_draw = 0.0
        self._ascii_only = False

    # -- 내부 --------------------------------------------------------------
    @property
    def _width(self) -> int:
        return shutil.get_terminal_size((80, 20)).columns

    def _clear(self) -> None:
        if self._dirty:
            self.stream.write("\r" + " " * max(0, self._width - 1) + "\r")
            self._dirty = False

    def _write_line(self, text: str) -> None:
        line = text[: max(10, self._width - 1)]
        try:
            self.stream.write("\r" + line)
            self.stream.flush()
        except UnicodeEncodeError:
            # 옛 윈도우 콘솔처럼 막대 문자를 못 쓰는 환경
            self._ascii_only = True
            self.stream.write("\r" + line.encode("ascii", "replace").decode("ascii"))
            self.stream.flush()
        self._dirty = True

    def _draw(self, now: float) -> None:
        entries = list(self._active.values())
        if not entries:
            return
        done = sum(entry["done"] for entry in entries)
        total = sum(entry["total"] for entry in entries)
        speed = sum(
            max(entry["done"] - entry["base"], 0) / max(now - entry["t0"], 1e-6)
            for entry in entries
        )
        percent = (done / total * 100) if total else 0.0
        eta = (total - done) / speed if speed > 0 and total else 0
        tail = (
            shorten(entries[0]["label"])
            if len(entries) == 1
            else f"{len(entries)}개 동시"
        )
        self._write_line(
            f"{progress_bar(percent, ascii_only=self._ascii_only)} {percent:5.1f}%  "
            f"{human_size(done)}/{human_size(total)}  {human_size(speed)}/s  "
            f"남은시간 {human_time(eta)}  {tail}"
        )

    # -- 인터페이스 --------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self._clear()
        print(self.PREFIX.get(level, "") + message, file=self.stream, flush=True)
        if self.live and self._active:
            self._draw(time.monotonic())

    def progress(self, key: str, label: str, done: int, total: int) -> None:
        now = time.monotonic()
        entry = self._active.get(key)
        if entry is None:
            entry = {"base": done, "t0": now}
            self._active[key] = entry
        entry.update(label=label, done=done, total=total)

        finished = bool(total) and done >= total
        if self.live:
            if now - self._last_draw < self.LIVE_INTERVAL and not finished:
                return
            self._last_draw = now
            self._draw(now)
            return

        # 화면이 아닐 때: 10% 단위 또는 몇 초마다 한 줄씩
        state = self._state.setdefault(key, {"last": 0.0, "step": -1})
        step = int((done / total) * 10) if total else 0
        if (
            step <= state["step"]
            and now - state["last"] < self.MULTILINE_INTERVAL
            and not finished
        ):
            return
        state["step"] = max(step, state["step"])
        state["last"] = now
        elapsed = max(now - entry["t0"], 1e-6)
        speed = max(done - entry["base"], 0) / elapsed
        percent = (done / total * 100) if total else 0.0
        eta = (total - done) / speed if speed > 0 and total else 0
        print(
            f"  {label}  {human_size(done)}/{human_size(total)}"
            f"  {percent:5.1f}%  {human_size(speed)}/s  남은시간 {human_time(eta)}",
            file=self.stream,
            flush=True,
        )

    def finished(self, key: str, label: str, path, skipped: bool = False) -> None:
        self._active.pop(key, None)
        self._state.pop(key, None)
        self._clear()
        if skipped:
            print(f"⏭ 이미 있음: {label}", file=self.stream, flush=True)
        else:
            print(f"✔ 완료: {path}", file=self.stream, flush=True)
        if self.live and self._active:
            self._draw(time.monotonic())

    def failed(self, key: str, label: str, error: str) -> None:
        self._active.pop(key, None)
        self._state.pop(key, None)
        self._clear()
        print(f"✖ 실패: {label} — {error}", file=self.stream, flush=True)
        if self.live and self._active:
            self._draw(time.monotonic())
