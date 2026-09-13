"""설정 파일과 세션 경로 관리.

설정은 ``~/.tgdl/config.json`` 에 저장된다(환경변수 ``TGDL_HOME`` 으로 변경 가능).
api_id / api_hash 는 https://my.telegram.org → API development tools 에서 발급받는다.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

HOME_ENV = "TGDL_HOME"
DEFAULT_HOME = Path.home() / ".tgdl"

API_HELP = """
텔레그램 API 키가 필요합니다 (한 번만 발급받으면 계속 사용합니다).

  1. https://my.telegram.org 접속 → 본인 전화번호로 로그인
  2. 'API development tools' 클릭
  3. App title / Short name 을 아무 이름으로 채우고 생성
  4. 화면에 표시되는 api_id(숫자)와 api_hash(문자열)를 아래에 입력
""".strip()


def on_termux() -> bool:
    """안드로이드 Termux 안에서 실행 중인지."""
    return "com.termux" in os.environ.get("PREFIX", "") or Path(
        "/data/data/com.termux"
    ).exists()


def media_scan(path) -> None:
    """안드로이드 갤러리가 새 파일을 바로 인식하도록 스캔을 요청한다.

    Termux 에서 `pkg install termux-api` 를 했을 때만 동작하고,
    없으면 조용히 넘어간다(파일은 이미 저장돼 있다).
    """
    import shutil
    import subprocess

    tool = shutil.which("termux-media-scan")
    if not tool:
        return
    try:
        subprocess.run(
            [tool, str(path)],
            timeout=20,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def default_download_dir() -> str:
    """기본 저장 폴더.

    안드로이드(Termux)에서는 갤러리에서 바로 보이는 공용 다운로드 폴더에
    저장한다. Termux 내부 폴더에 넣으면 갤러리나 다른 앱이 볼 수 없다.
    """
    if on_termux():
        return "/sdcard/Download/telegram"
    return str(Path.home() / "Downloads" / "telegram")


@dataclass
class Config:
    api_id: int = 0
    api_hash: str = ""
    download_dir: str = field(default_factory=default_download_dir)
    concurrency: int = 2  # 동시에 받을 파일 수
    connections: int = 4  # 파일 하나에 쓸 연결 수(클수록 빠름, 최대 16)
    media: str = "video"  # "video" 또는 "all"
    per_chat_folder: bool = True
    limit: int = 200  # 메시지 번호 없는 링크에서 탐색할 최대 개수
    proxy: str = ""  # 예: socks5://127.0.0.1:1080  (python-socks 필요)

    @property
    def ready(self) -> bool:
        return bool(self.api_id) and bool(self.api_hash)


def app_home() -> Path:
    path = Path(os.environ.get(HOME_ENV) or DEFAULT_HOME).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_home() / "config.json"


def session_path() -> Path:
    """Telethon 세션 파일 경로(확장자 없이). 한 번 로그인하면 계속 재사용된다."""
    session_dir = app_home() / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "tgdl"


def load_config() -> Config:
    cfg = Config()
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if hasattr(cfg, key) and value not in (None, ""):
                    setattr(cfg, key, value)

    # 환경변수가 파일보다 우선한다.
    env_id = os.environ.get("TGDL_API_ID") or os.environ.get("TG_API_ID")
    env_hash = os.environ.get("TGDL_API_HASH") or os.environ.get("TG_API_HASH")
    env_dir = os.environ.get("TGDL_DOWNLOAD_DIR")
    if env_id and env_id.isdigit():
        cfg.api_id = int(env_id)
    if env_hash:
        cfg.api_hash = env_hash.strip()
    if env_dir:
        cfg.download_dir = env_dir

    try:
        cfg.api_id = int(cfg.api_id or 0)
    except (TypeError, ValueError):
        cfg.api_id = 0
    cfg.concurrency = max(1, min(8, int(cfg.concurrency or 1)))
    cfg.connections = max(1, min(16, int(cfg.connections or 4)))
    if cfg.media not in {"video", "all"}:
        cfg.media = "video"
    return cfg


def save_config(cfg: Config) -> Path:
    path = config_path()
    path.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(path, 0o600)  # api_hash 가 들어 있으므로 본인만 읽도록
    except OSError:
        pass
    return path


def _ask(prompt: str) -> str:
    """입력을 받되, 입력할 수 없는 환경이면 안내 후 종료한다."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(
            "\n입력을 받을 수 없는 환경입니다. 아래 중 하나를 사용하세요.\n"
            "  · 터미널에서 직접 실행: python -m tgdl config\n"
            "  · 환경변수로 지정: TGDL_API_ID=숫자 TGDL_API_HASH=문자열"
        )


def setup_wizard(cfg: Optional[Config] = None, ask_all: bool = False) -> Config:
    """api_id / api_hash / 저장 폴더를 대화형으로 입력받아 저장한다."""
    cfg = cfg or load_config()
    if not sys.stdin.isatty():
        raise SystemExit(
            "텔레그램 API 키가 설정되지 않았습니다.\n"
            "  · 터미널에서 `python -m tgdl config` 를 실행해 설정하거나,\n"
            "  · TGDL_API_ID / TGDL_API_HASH 환경변수를 지정하세요.\n"
            "  · 키 발급: https://my.telegram.org → API development tools"
        )
    print(API_HELP)
    print()

    while True:
        current = f" [{cfg.api_id}]" if cfg.api_id else ""
        raw = _ask(f"api_id{current}: ")
        if not raw and cfg.api_id:
            break
        if raw.isdigit():
            cfg.api_id = int(raw)
            break
        print("  숫자만 입력해 주세요.")

    while True:
        current = " [입력됨]" if cfg.api_hash else ""
        raw = _ask(f"api_hash{current}: ")
        if not raw and cfg.api_hash:
            break
        if len(raw) >= 20:
            cfg.api_hash = raw
            break
        print("  api_hash 가 너무 짧습니다. 다시 확인해 주세요.")

    if ask_all:
        raw = _ask(f"저장 폴더 [{cfg.download_dir}]: ")
        if raw:
            cfg.download_dir = str(Path(raw).expanduser())

    path = save_config(cfg)
    print(f"\n설정을 저장했습니다: {path}")
    return cfg


def parse_proxy(value: str):
    """``socks5://user:pass@host:port`` 형태를 Telethon proxy 인자로 변환."""
    value = (value or "").strip()
    if not value:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(value if "://" in value else f"socks5://{value}")
    scheme = (parsed.scheme or "socks5").lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise ValueError(f"지원하지 않는 프록시 방식입니다: {scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"프록시 주소를 이해할 수 없습니다: {value}")
    proxy = {
        "proxy_type": scheme,
        "addr": parsed.hostname,
        "port": int(parsed.port),
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy
