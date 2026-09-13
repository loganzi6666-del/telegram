"""명령줄 인터페이스.

사용 예
  python -m tgdl                       링크를 붙여넣는 대화형 모드
  python -m tgdl <링크> [<링크> …]      링크를 바로 다운로드
  python -m tgdl watch                 클립보드 감시 (복사하면 자동 다운로드)
  python -m tgdl gui                   간단한 창 모드
  python -m tgdl serve                 받은 파일을 휴대폰으로 옮기기        
  python -m tgdl listen                텔레그램으로 링크를 보내면 받아서 되돌려줌
                                       (밖에서 휴대폰만으로 사용 · 기종 무관)
  python -m tgdl login                 텔레그램 로그인만 수행
  python -m tgdl config                api_id / api_hash / 저장 폴더 설정
  python -m tgdl logout                로그인 세션 삭제
  python -m tgdl update                최신 코드로 갱신 (git 필요)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import (
    Config,
    config_path,
    load_config,
    media_scan,
    on_termux,
    parse_proxy,
    save_config,
    session_path,
    setup_wizard,
)
from .downloader import Downloader, LinkPump
from .links import extract_links, parse_link, LinkError
from .reporter import ConsoleReporter

COMMANDS = {
    "get",
    "watch",
    "gui",
    "serve",
    "listen",
    "login",
    "logout",
    "config",
    "update",
    "help",
}
QUIT_WORDS = {"q", "quit", "exit", "종료", "끝"}

BANNER = f"""텔레그램 동영상 다운로더 (tgdl {__version__})
  · 링크를 붙여넣고 Enter 를 누르면 바로 다운로드가 시작됩니다.
  · 한 줄에 여러 링크를 붙여넣어도 됩니다.
  · 종료: q + Enter (또는 Ctrl+C)"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgdl",
        description="텔레그램 동영상 링크를 붙여넣으면 자동으로 내려받습니다(비공개 채널 지원).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("targets", nargs="*", help="텔레그램 링크 (여러 개 가능)")
    parser.add_argument("-o", "--out", help="저장 폴더 (기본값은 설정 파일)")
    parser.add_argument(
        "-c", "--concurrency", type=int, help="동시에 받을 파일 수 (기본 2)"
    )
    parser.add_argument(
        "--connections",
        type=int,
        help="파일 하나를 받을 때 쓸 연결 수 (기본 4, 최대 16). 클수록 빠릅니다",
    )
    parser.add_argument(
        "--all", action="store_true", help="동영상뿐 아니라 사진·문서 등 모든 첨부 받기"
    )
    parser.add_argument("--overwrite", action="store_true", help="같은 파일이 있어도 다시 받기")
    parser.add_argument("--no-album", action="store_true", help="앨범의 다른 항목은 받지 않기")
    parser.add_argument("--flat", action="store_true", help="채널별 폴더를 만들지 않기")
    parser.add_argument(
        "--limit", type=int, help="메시지 번호 없는 링크에서 탐색할 최대 개수 (기본 200)"
    )
    parser.add_argument("--from-file", help="링크가 줄바꿈으로 적힌 파일에서 읽기")
    parser.add_argument("--proxy", help="프록시 (예: socks5://127.0.0.1:1080)")
    parser.add_argument("--interval", type=float, default=1.0, help="클립보드 확인 간격(초)")
    parser.add_argument(
        "--port", type=int, default=8000, help="serve: 휴대폰이 접속할 포트 번호 (기본 8000)"
    )
    parser.add_argument(
        "--chat",
        default="me",
        help="listen: 감시할 대화방 (기본 me = 저장한 메시지, 예: @내봇, 채널이름)",
    )
    parser.add_argument(
        "--no-send", action="store_true", help="listen: 받은 파일을 되돌려 보내지 않기"
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=15.0,
        help="listen: 대화방을 직접 확인하는 간격(초). 0 이면 확인 안 함 (기본 15)",
    )
    parser.add_argument(
        "--catch-up",
        type=int,
        nargs="?",
        const=20,
        default=0,
        help="listen: 시작할 때 최근 메시지에서 링크를 찾아 처리 (기본 20개)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="무슨 메시지가 오는지 자세히 표시"
    )
    parser.add_argument("--show", action="store_true", help="config: 현재 설정만 보기")
    parser.add_argument("--version", action="version", version=f"tgdl {__version__}")
    return parser


def split_argv(argv: List[str]) -> tuple:
    """첫 인자가 명령어인지 링크인지 구분한다."""
    if argv and argv[0] in COMMANDS:
        return argv[0], argv[1:]
    return "get", argv


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.out:
        cfg.download_dir = str(Path(args.out).expanduser())
    if args.concurrency:
        cfg.concurrency = max(1, min(8, args.concurrency))
    if args.connections:
        cfg.connections = max(1, min(16, args.connections))
    if args.all:
        cfg.media = "all"
    if args.limit:
        cfg.limit = args.limit
    if args.flat:
        cfg.per_chat_folder = False
    if args.proxy:
        cfg.proxy = args.proxy
    return cfg


def collect_targets(args: argparse.Namespace, reporter: ConsoleReporter) -> List:
    raw_inputs = list(args.targets)
    if args.from_file:
        path = Path(args.from_file).expanduser()
        if not path.exists():
            reporter.log(f"파일을 찾을 수 없습니다: {path}", "error")
        else:
            raw_inputs.extend(path.read_text(encoding="utf-8").splitlines())

    links = []
    keys = set()
    for item in raw_inputs:
        item = (item or "").strip()
        if not item:
            continue
        found = extract_links(item)
        if not found:
            try:
                found = [parse_link(item)]
            except LinkError as exc:
                reporter.log(str(exc), "warn")
                continue
        for link in found:
            if link.key not in keys:
                keys.add(link.key)
                links.append(link)
    return links


def ensure_download_dir(cfg: Config, reporter) -> Path:
    """저장 폴더를 만들고 쓸 수 있는지 확인한다. 안 되면 대체 폴더를 쓴다."""
    folder = Path(cfg.download_dir).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".tgdl-write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return folder
    except OSError as exc:
        reporter.log(f"{folder} 에 저장할 수 없습니다 ({exc.strerror or exc}).", "warn")
        if on_termux():
            reporter.log(
                "Termux 라면 먼저 `termux-setup-storage` 를 실행해 저장 권한을 허용하세요.",
                "warn",
            )
        fallback = Path.home() / "tgdl-downloads"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError as exc2:
            raise SystemExit(f"저장 폴더를 만들 수 없습니다: {exc2}")
        reporter.log(f"대신 여기에 저장합니다: {fallback}", "warn")
        cfg.download_dir = str(fallback)
        return fallback


async def make_client(cfg: Config, reporter: ConsoleReporter):
    from telethon import TelegramClient

    proxy = None
    if cfg.proxy:
        try:
            proxy = parse_proxy(cfg.proxy)
        except ValueError as exc:
            reporter.log(str(exc), "error")
            raise SystemExit(2)

    client = TelegramClient(
        str(session_path()),
        cfg.api_id,
        cfg.api_hash,
        proxy=proxy,
        connection_retries=5,
        retry_delay=2,
        request_retries=5,
        auto_reconnect=True,
        device_model="tgdl",
        system_version="1.0",
        app_version=__version__,
    )
    return client


async def start_client(client, reporter: ConsoleReporter) -> None:
    from telethon import errors

    was_authorized = False
    try:
        await client.connect()
        was_authorized = await client.is_user_authorized()
    except OSError as exc:
        raise SystemExit(f"텔레그램에 연결할 수 없습니다: {exc}")

    if not was_authorized:
        reporter.log("텔레그램 로그인이 필요합니다. (전화번호 → 인증코드)")
        reporter.log("전화번호는 국가번호를 포함해 입력하세요. 예: +821012345678")
    try:
        await client.start()
    except errors.SessionPasswordNeededError:
        raise SystemExit("2단계 인증 비밀번호가 필요합니다. 다시 실행해 입력해 주세요.")
    except errors.PhoneNumberInvalidError:
        raise SystemExit("전화번호 형식이 올바르지 않습니다. 예: +821012345678")
    except errors.ApiIdInvalidError:
        raise SystemExit(
            "api_id / api_hash 가 올바르지 않습니다. `python -m tgdl config` 로 다시 설정하세요."
        )
    except EOFError:
        raise SystemExit("로그인 입력이 필요합니다. 터미널에서 `python -m tgdl login` 을 실행하세요.")

    me = await client.get_me()
    name = getattr(me, "first_name", None) or getattr(me, "username", None) or "사용자"
    if not was_authorized:
        reporter.log(f"로그인 성공: {name}", "ok")
    else:
        reporter.log(f"로그인 상태: {name}")


def make_downloader(client, cfg: Config, args: argparse.Namespace, reporter) -> Downloader:
    downloader = Downloader(
        client,
        reporter,
        out_dir=cfg.download_dir,
        media=cfg.media,
        concurrency=cfg.concurrency,
        per_chat_folder=cfg.per_chat_folder,
        include_album=not args.no_album,
        overwrite=args.overwrite,
        limit=cfg.limit,
        connections=cfg.connections,
    )
    if on_termux():
        # 안드로이드: 받은 영상이 갤러리에 바로 보이게 한다.
        downloader.after_save = media_scan
    return downloader


async def run_interactive(pump: LinkPump, reporter: ConsoleReporter) -> None:
    print(BANNER)
    print()
    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break
        if line == "":  # EOF
            break
        text = line.strip()
        if not text:
            continue
        if text.lower() in QUIT_WORDS:
            break
        links = extract_links(text)
        if not links:
            reporter.log("텔레그램 링크를 찾지 못했습니다. 예: https://t.me/c/1234567890/56", "warn")
            continue
        added = pump.submit(links)
        if added == 0:
            reporter.log("이미 받은(또는 대기 중인) 링크입니다.", "warn")
        else:
            reporter.log(f"{added}개 링크를 대기열에 넣었습니다.")

    reporter.log("남은 다운로드를 마무리하는 중…")
    await pump.join()


async def run_watch(pump: LinkPump, reporter: ConsoleReporter, interval: float) -> None:
    from .clipboard import ClipboardUnavailable, read_clipboard

    try:
        last = read_clipboard() or ""
    except ClipboardUnavailable as exc:
        reporter.log(str(exc), "error")
        raise SystemExit(2)

    reporter.log("클립보드 감시를 시작합니다. 텔레그램 링크를 복사하면 자동으로 받습니다.", "ok")
    reporter.log("종료: Ctrl+C")
    interval = max(0.3, float(interval or 1.0))

    while True:
        await asyncio.sleep(interval)
        try:
            current = read_clipboard()
        except ClipboardUnavailable:
            break
        if current is None or current == last:
            continue
        last = current
        links = extract_links(current)
        if not links:
            continue
        added = pump.submit(links)
        if added:
            reporter.log(f"클립보드에서 {added}개 링크를 찾았습니다.")


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.show:
        print(f"설정 파일 : {config_path()}")
        print(f"세션 파일 : {session_path()}.session")
        print(f"api_id    : {cfg.api_id or '(없음)'}")
        print(f"api_hash  : {'설정됨' if cfg.api_hash else '(없음)'}")
        print(f"저장 폴더 : {cfg.download_dir}")
        print(f"동시 파일 : {cfg.concurrency}개")
        print(f"연결 수   : {cfg.connections}개 (파일당)")
        print(f"받을 종류 : {'모든 첨부' if cfg.media == 'all' else '동영상만'}")
        print(f"채널 폴더 : {'사용' if cfg.per_chat_folder else '사용 안 함'}")
        print(f"프록시    : {cfg.proxy or '(없음)'}")
        return 0

    if args.out:
        cfg.download_dir = str(Path(args.out).expanduser())
        save_config(cfg)
        print(f"저장 폴더를 바꿨습니다: {cfg.download_dir}")
        return 0

    setup_wizard(cfg, ask_all=True)
    return 0


def cmd_logout() -> int:
    removed = False
    for path in (Path(f"{session_path()}.session"), Path(f"{session_path()}.session-journal")):
        if path.exists():
            path.unlink()
            removed = True
    print("로그인 세션을 삭제했습니다." if removed else "삭제할 세션이 없습니다.")
    return 0


async def amain(command: str, args: argparse.Namespace) -> int:
    cfg = apply_overrides(load_config(), args)
    if not cfg.ready:
        cfg = setup_wizard(cfg, ask_all=True)
        cfg = apply_overrides(cfg, args)

    links = collect_targets(args, ConsoleReporter(live=False)) if command == "get" else []
    reporter = ConsoleReporter()

    client = await make_client(cfg, reporter)
    await start_client(client, reporter)

    if command == "login":
        await client.disconnect()
        return 0

    folder = ensure_download_dir(cfg, reporter)
    downloader = make_downloader(client, cfg, args, reporter)
    reporter.log(f"저장 폴더: {folder}")

    if command == "listen":
        from .listen import run_listen

        try:
            await run_listen(
                client,
                reporter,
                lambda rep: make_downloader(client, cfg, args, rep),
                chat=args.chat,
                send_back=not args.no_send,
                poll_interval=args.poll,
                catch_up=args.catch_up,
                debug=args.debug,
            )
        except KeyboardInterrupt:
            reporter.log("감시를 멈췄습니다.", "warn")
        finally:
            await downloader.aclose()
            reporter.log(downloader.stats.summary())
            await client.disconnect()
        return 0

    pump = LinkPump(downloader)
    try:
        if command == "watch":
            await run_watch(pump, reporter, args.interval)
        elif links:
            pump.submit(links)
            await pump.join()
        else:
            await run_interactive(pump, reporter)
    except KeyboardInterrupt:
        reporter.log("중단합니다…", "warn")
    finally:
        await pump.close()
        await downloader.aclose()
        reporter.log(downloader.stats.summary())
        await client.disconnect()

    return 1 if downloader.stats.failed and not downloader.stats.downloaded else 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command, rest = split_argv(argv)
    parser = build_parser()

    if command == "help":
        parser.print_help()
        return 0

    args = parser.parse_args(rest)
    if command == "config":
        return cmd_config(args)
    if command == "logout":
        return cmd_logout()

    if command == "update":
        from .selfupdate import run_update

        return run_update()

    if command == "gui":
        from .gui import run_gui

        return run_gui(args)

    if command == "serve":
        from .serve import run_server

        cfg = apply_overrides(load_config(), args)
        return run_server(cfg.download_dir, args.port)

    try:
        import telethon  # noqa: F401
    except ImportError:
        print(
            "telethon 이 설치되어 있지 않습니다.\n"
            "  pip install -r requirements.txt\n"
            "또는\n"
            "  pip install telethon cryptg",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(amain(command, args))
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return 130
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return int(exc.code or 0)
