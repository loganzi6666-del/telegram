"""간단한 창(GUI) 모드.

파이썬 기본 tkinter 만 사용한다. 링크를 붙여넣고 [다운로드] 를 누르거나,
'클립보드 자동 감지'를 켜두면 링크를 복사하는 순간 자동으로 내려받는다.
"""

from __future__ import annotations

import asyncio
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .config import Config, load_config, parse_proxy, save_config, session_path
from .downloader import Downloader, LinkPump
from .links import extract_links
from .reporter import Reporter, human_size


class GuiReporter(Reporter):
    """다운로더의 알림을 UI 큐로 흘려보낸다."""

    def __init__(self, ui_queue: "queue.Queue") -> None:
        self.ui = ui_queue
        self._last: dict = {}

    def log(self, message: str, level: str = "info") -> None:
        self.ui.put(("log", level, message))

    def progress(self, key: str, label: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._last.get(key, 0) < 0.15 and not (total and done >= total):
            return
        self._last[key] = now
        self.ui.put(("progress", key, label, done, total))

    def finished(self, key: str, label: str, path, skipped: bool = False) -> None:
        self._last.pop(key, None)
        self.ui.put(("finished", key, label, str(path), skipped))

    def failed(self, key: str, label: str, error: str) -> None:
        self._last.pop(key, None)
        self.ui.put(("failed", key, label, error))


class App:
    def __init__(self, args) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.args = args
        self.cfg: Config = load_config()
        if getattr(args, "out", None):
            self.cfg.download_dir = str(Path(args.out).expanduser())
        if getattr(args, "all", False):
            self.cfg.media = "all"

        self.ui_queue: "queue.Queue" = queue.Queue()
        self.reporter = GuiReporter(self.ui_queue)
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.downloader: Optional[Downloader] = None
        self.pump: Optional[LinkPump] = None
        self.ready = threading.Event()
        self._clip_last = ""
        self._active: dict = {}
        self._closing = False

        self._build_ui()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.root.after(150, self._drain)
        self.root.after(1200, self._poll_clipboard)

    # --------------------------------------------------------------- 화면
    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        self.root = tk.Tk()
        self.root.title(f"텔레그램 동영상 다운로더 {__version__}")
        self.root.geometry("720x520")
        self.root.minsize(560, 420)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="링크").grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(frame)
        self.entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.entry.bind("<Return>", lambda _event: self._submit_entry())
        self.button = ttk.Button(frame, text="다운로드", command=self._submit_entry)
        self.button.grid(row=0, column=2, sticky="e")

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="클립보드 자동 감지 (링크를 복사하면 바로 받기)",
            variable=self.auto_var,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.all_var = tk.BooleanVar(value=self.cfg.media == "all")
        ttk.Checkbutton(
            frame,
            text="동영상 외 사진·파일도 받기",
            variable=self.all_var,
            command=self._toggle_media,
        ).grid(row=2, column=0, columnspan=3, sticky="w")

        folder_row = ttk.Frame(frame)
        folder_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="저장 폴더").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=str(Path(self.cfg.download_dir).expanduser()))
        ttk.Entry(folder_row, textvariable=self.folder_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(folder_row, text="변경", command=self._choose_folder).grid(row=0, column=2)
        ttk.Button(folder_row, text="열기", command=self._open_folder).grid(
            row=0, column=3, padx=(6, 0)
        )

        self.status_var = tk.StringVar(value="텔레그램에 연결하는 중…")
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(12, 2)
        )
        self.bar = ttk.Progressbar(frame, maximum=1000)
        self.bar.grid(row=5, column=0, columnspan=3, sticky="ew")

        log_row = ttk.Frame(frame)
        log_row.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
        frame.rowconfigure(6, weight=1)
        log_row.columnconfigure(0, weight=1)
        log_row.rowconfigure(0, weight=1)
        self.log = self.tk.Text(log_row, height=12, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_row, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    # --------------------------------------------------------------- 도우미
    def _append_log(self, message: str, level: str = "info") -> None:
        prefix = {"info": "", "ok": "✔ ", "warn": "⚠ ", "error": "✖ "}.get(level, "")
        self.log.configure(state="normal")
        self.log.insert("end", prefix + message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def ask(self, prompt: str, secret: bool = False, title: str = "텔레그램 로그인") -> str:
        """작업 스레드에서 UI 입력창을 띄우고 결과를 기다린다."""
        from tkinter import simpledialog

        box: dict = {}
        done = threading.Event()

        def show() -> None:
            try:
                box["value"] = simpledialog.askstring(
                    title, prompt, show="*" if secret else None, parent=self.root
                )
            finally:
                done.set()

        self.root.after(0, show)
        done.wait()
        value = box.get("value")
        if value is None:
            raise RuntimeError("입력이 취소되었습니다.")
        return value.strip()

    def _toggle_media(self) -> None:
        self.cfg.media = "all" if self.all_var.get() else "video"
        if self.downloader:
            self.downloader.media = self.cfg.media
        save_config(self.cfg)

    def _choose_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(initialdir=self.folder_var.get())
        if not chosen:
            return
        self.cfg.download_dir = chosen
        self.folder_var.set(chosen)
        save_config(self.cfg)
        if self.downloader:
            self.downloader.out_dir = Path(chosen)
        self._append_log(f"저장 폴더를 바꿨습니다: {chosen}")

    def _open_folder(self) -> None:
        path = Path(self.folder_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"폴더를 열 수 없습니다: {exc}", "warn")

    # --------------------------------------------------------------- 제출
    def _submit_text(self, text: str, quiet: bool = False) -> None:
        links = extract_links(text or "")
        if not links:
            if not quiet:
                self._append_log("텔레그램 링크를 찾지 못했습니다.", "warn")
            return
        if not self.ready.is_set() or self.pump is None:
            self._append_log("아직 연결 중입니다. 잠시 후 다시 시도해 주세요.", "warn")
            return

        def submit() -> None:
            added = self.pump.submit(links)
            if added:
                self.ui_queue.put(("log", "info", f"{added}개 링크를 대기열에 넣었습니다."))
            elif not quiet:
                self.ui_queue.put(("log", "warn", "이미 처리한 링크입니다."))

        self.loop.call_soon_threadsafe(submit)

    def _submit_entry(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._submit_text(text)

    def _poll_clipboard(self) -> None:
        if not self._closing:
            if self.auto_var.get():
                try:
                    current = self.root.clipboard_get()
                except Exception:  # noqa: BLE001 - 클립보드가 비었거나 텍스트가 아님
                    current = None
                if current and current != self._clip_last:
                    self._clip_last = current
                    self._submit_text(current, quiet=True)
            self.root.after(1000, self._poll_clipboard)

    # --------------------------------------------------------------- 이벤트
    def _drain(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[2], event[1])
                elif kind == "progress":
                    _, key, label, done, total = event
                    self._active[key] = (label, done, total)
                    percent = (done / total * 1000) if total else 0
                    self.bar.configure(value=percent)
                    extra = f" · 대기/진행 {len(self._active)}개" if len(self._active) > 1 else ""
                    self.status_var.set(
                        f"{label} — {human_size(done)}/{human_size(total)}"
                        f" ({(done / total * 100) if total else 0:.1f}%){extra}"
                    )
                elif kind == "finished":
                    _, key, label, path, skipped = event
                    self._active.pop(key, None)
                    if skipped:
                        self._append_log(f"이미 있음: {label}")
                    else:
                        self._append_log(f"완료: {path}", "ok")
                    if not self._active:
                        self.bar.configure(value=0)
                        self.status_var.set("대기 중 — 링크를 붙여넣거나 복사하세요.")
                elif kind == "failed":
                    _, key, label, error = event
                    self._active.pop(key, None)
                    self._append_log(f"실패: {label} — {error}", "error")
                elif kind == "status":
                    self.status_var.set(event[1])
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(150, self._drain)

    # --------------------------------------------------------------- 백엔드
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._boot())
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("log", "error", f"연결 실패: {exc}"))
            self.ui_queue.put(("status", "연결 실패 — 프로그램을 다시 실행해 주세요."))
            return
        self.loop.run_forever()

    async def _boot(self) -> None:
        from telethon import TelegramClient

        if not self.cfg.ready:
            self.ui_queue.put(("log", "info", "처음 실행: 텔레그램 API 키가 필요합니다."))
            self.ui_queue.put(
                (
                    "log",
                    "info",
                    "https://my.telegram.org → API development tools 에서 발급받으세요.",
                )
            )
            api_id = await asyncio.to_thread(
                self.ask, "api_id (숫자)", False, "텔레그램 API 설정"
            )
            api_hash = await asyncio.to_thread(
                self.ask, "api_hash", False, "텔레그램 API 설정"
            )
            if not api_id.isdigit():
                raise RuntimeError("api_id 는 숫자여야 합니다.")
            self.cfg.api_id = int(api_id)
            self.cfg.api_hash = api_hash
            save_config(self.cfg)

        proxy = parse_proxy(self.cfg.proxy) if self.cfg.proxy else None
        self.client = TelegramClient(
            str(session_path()),
            self.cfg.api_id,
            self.cfg.api_hash,
            proxy=proxy,
            connection_retries=5,
            retry_delay=2,
            request_retries=5,
            device_model="tgdl-gui",
            app_version=__version__,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            self.ui_queue.put(("status", "로그인 필요 — 입력창을 확인하세요."))
            await self.client.start(
                phone=lambda: asyncio.to_thread(
                    self.ask, "전화번호 (국가번호 포함, 예: +821012345678)"
                ),
                code_callback=lambda: asyncio.to_thread(
                    self.ask, "텔레그램으로 받은 인증코드"
                ),
                password=lambda: asyncio.to_thread(
                    self.ask, "2단계 인증 비밀번호", True
                ),
            )

        me = await self.client.get_me()
        name = getattr(me, "first_name", None) or getattr(me, "username", None) or "사용자"

        self.downloader = Downloader(
            self.client,
            self.reporter,
            out_dir=self.cfg.download_dir,
            media=self.cfg.media,
            concurrency=self.cfg.concurrency,
            per_chat_folder=self.cfg.per_chat_folder,
            include_album=not getattr(self.args, "no_album", False),
            overwrite=getattr(self.args, "overwrite", False),
            limit=self.cfg.limit,
        )
        self.pump = LinkPump(self.downloader)
        self.ready.set()
        self.ui_queue.put(("log", "ok", f"로그인 상태: {name}"))
        self.ui_queue.put(("status", "대기 중 — 링크를 붙여넣거나 복사하세요."))

    # --------------------------------------------------------------- 종료
    def _on_close(self) -> None:
        self._closing = True

        async def shutdown() -> None:
            if self.pump:
                await self.pump.close()
            if self.client:
                await self.client.disconnect()

        if self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(args) -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "tkinter 가 없어 창 모드를 쓸 수 없습니다.\n"
            "  · 우분투/데비안: sudo apt install python3-tk\n"
            "  · 또는 터미널 모드를 사용하세요: python -m tgdl",
            file=sys.stderr,
        )
        return 2
    try:
        import telethon  # noqa: F401
    except ImportError:
        print("telethon 이 필요합니다: pip install -r requirements.txt", file=sys.stderr)
        return 2
    return App(args).run()
