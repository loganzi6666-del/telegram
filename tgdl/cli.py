"""명령줄 인터페이스.

사용 예
  python -m tgdl                       링크를 붙여넣는 대화형 모드
  python -m tgdl <링크> [<링크> …]      링크를 바로 다운로드
  python -m tgdl watch                 클립보드 감시 (복사하면 자동 다운로드)
  python -m tgdl gui                   간단한 창 모드
  python -m tgdl serve                 받은 파일을 휴대폰으로 옮기기        
  python -m tgdl listen                텔레그램으로 링크를 보내면 받아서 되돌려줌
                                       (밖에서 휴대폰만으로 사용 · 기종 무관)
  python -m tgdl remote                텔레그램 명령으로 자동매매 봇·전원 조종
                                       (집 컴퓨터에서 켜 둔다)
  python -m tgdl bot start|stop|status 매매봇을 이 컴퓨터에서 바로 켜기/끄기
  python -m tgdl waker                 보조기기에서 `/깨우기` 를 기다림 (WoL)
  python -m tgdl wake                  집 컴퓨터를 지금 깨우기 (한 번)
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
    "remote",
    "bot",
    "waker",
    "wake",
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
    parser.add_argument(
        "--control",
        action="store_true",
        help="listen: 다운로드와 함께 원격 조종 명령(/상태 /시작 …)도 받기",
    )
    parser.add_argument(
        "--power",
        action="store_true",
        help="remote: 전원 명령(/절전 /종료 /재부팅) 허용",
    )
    parser.add_argument(
        "--no-power", action="store_true", help="remote: 전원 명령을 허용하지 않기"
    )
    parser.add_argument("--name", help="bot: 프로그램 이름 (기본 '매매봇')")
    parser.add_argument("--cwd", help="bot add: 프로그램을 실행할 폴더")
    parser.add_argument(
        "--grace", type=float, help="bot: 부드럽게 멈추기를 기다리는 시간(초)"
    )
    parser.add_argument("--lines", type=int, default=20, help="bot log: 보여줄 줄 수")
    parser.add_argument("--mac", help="wake/waker: 깨울 컴퓨터의 랜카드 주소(MAC)")
    parser.add_argument("--host", help="wake/waker: 켜졌는지 확인할 주소(랜 IP)")
    parser.add_argument(
        "--wake-port", type=int, help="wake/waker: 확인에 쓸 포트 (0 이면 ping)"
    )
    parser.add_argument("--broadcast", help="wake/waker: 매직 패킷을 보낼 브로드캐스트 주소")
    parser.add_argument(
        "--wait", type=float, default=120.0, help="wake: 켜질 때까지 기다릴 시간(초)"
    )
    parser.add_argument(
        "--save", action="store_true", help="wake: 지정한 MAC·주소를 설정에 저장"
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
        print(f"전원 명령 : {'허용' if cfg.allow_power else '허용 안 함'}")
        print(f"깨우기 MAC: {cfg.wake_mac or '(없음)'}")
        print(f"확인 주소 : {cfg.wake_host or '(없음)'}"
              + (f":{cfg.wake_port}" if cfg.wake_port else ""))
        if cfg.programs:
            print("등록된 프로그램:")
            for item in cfg.programs:
                if isinstance(item, dict):
                    where = f" (폴더 {item.get('cwd')})" if item.get("cwd") else ""
                    print(f"  · {item.get('name')} → {item.get('command')}{where}")
        else:
            print("등록된 프로그램: (없음)")
        return 0

    if args.out:
        cfg.download_dir = str(Path(args.out).expanduser())
        save_config(cfg)
        print(f"저장 폴더를 바꿨습니다: {cfg.download_dir}")
        return 0

    setup_wizard(cfg, ask_all=True)
    return 0


BOT_USAGE = """사용법
  python -m tgdl bot add "C:\\매매봇\\bot.py" [--name 매매봇] [--cwd C:\\매매봇]
  python -m tgdl bot list
  python -m tgdl bot start|stop|restart|status [이름]
  python -m tgdl bot log [이름] [--lines 50]
  python -m tgdl bot remove <이름>"""


def _manager(cfg: Config):
    from .program import ProgramManager, load_programs

    return ProgramManager(load_programs(cfg.programs))


def cmd_bot(args: argparse.Namespace) -> int:
    """텔레그램 없이 이 컴퓨터에서 매매봇을 켜고 끄고 확인한다.

    원격으로 쓰기 전에 여기서 먼저 잘 되는지 확인하는 것이 좋다.
    """
    from .program import Program, ProgramError, load_programs

    cfg = load_config()
    rest = list(args.targets)
    action = (rest.pop(0) if rest else "list").lower()
    name = args.name or (rest.pop(0) if rest else None)

    if action in {"add", "등록"}:
        if not rest and not args.name:
            print(BOT_USAGE, file=sys.stderr)
            return 2
        command = " ".join(rest) if rest else ""
        if not command:
            print("실행할 파일이나 명령을 함께 적어 주세요.\n" + BOT_USAGE, file=sys.stderr)
            return 2
        program = Program(
            name=args.name or "매매봇",
            command=command,
            cwd=args.cwd or "",
            grace=args.grace if args.grace else 20.0,
        )
        entry = {
            "name": program.name,
            "command": program.command,
            "cwd": program.cwd,
            "grace": program.grace,
        }
        programs = [
            item
            for item in cfg.programs
            if not (isinstance(item, dict) and item.get("name") == program.name)
        ]
        programs.append(entry)
        cfg.programs = programs
        save_config(cfg)
        print(f"등록했습니다: {program.name} → {program.command}")
        print(f"설정 파일: {config_path()}")
        print("이제 다음으로 시험해 보세요:  python -m tgdl bot start")
        return 0

    if action in {"remove", "삭제"}:
        if not name:
            print(BOT_USAGE, file=sys.stderr)
            return 2
        before = len(cfg.programs)
        cfg.programs = [
            item
            for item in cfg.programs
            if not (isinstance(item, dict) and str(item.get("name")) == name)
        ]
        save_config(cfg)
        print("삭제했습니다." if len(cfg.programs) < before else f"'{name}' 을 찾지 못했습니다.")
        return 0

    if action in {"list", "목록"}:
        programs = load_programs(cfg.programs)
        if not programs:
            print("등록된 프로그램이 없습니다.\n" + BOT_USAGE)
            return 0
        manager = _manager(cfg)
        for status in manager.status_all():
            print(status.line())
        return 0

    manager = _manager(cfg)
    try:
        if action in {"start", "시작"}:
            print(manager.start(name).line())
        elif action in {"stop", "중지", "정지"}:
            print(manager.stop(name, grace=args.grace).line())
        elif action in {"restart", "재시작"}:
            print(manager.restart(name).line())
        elif action in {"status", "상태"}:
            for status in manager.status_all():
                print(status.line())
        elif action in {"log", "로그"}:
            print(manager.tail(name, args.lines))
        else:
            print(f"모르는 명령입니다: {action}\n" + BOT_USAGE, file=sys.stderr)
            return 2
    except ProgramError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    """집 컴퓨터에 매직 패킷을 한 번 보낸다(같은 공유기 안에서만 동작)."""
    from .wake import WakeError, is_awake, pretty_mac, send_magic, wait_awake

    cfg = load_config()
    mac = args.mac or cfg.wake_mac
    host = args.host or cfg.wake_host
    port = args.wake_port if args.wake_port is not None else cfg.wake_port
    broadcast = args.broadcast or cfg.wake_broadcast

    if not mac:
        print(
            "깨울 컴퓨터의 랜카드 주소(MAC)가 필요합니다.\n"
            "  집 컴퓨터에서 `ipconfig /all` → 이더넷의 '물리적 주소'\n"
            "  예: python -m tgdl wake --mac A1-B2-C3-D4-E5-F6 --host 192.168.0.10 --save",
            file=sys.stderr,
        )
        return 2

    try:
        mac = pretty_mac(mac)
    except WakeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    if args.save:
        cfg.wake_mac = mac
        cfg.wake_host = host or ""
        cfg.wake_port = int(port or 0)
        cfg.wake_broadcast = broadcast or ""
        save_config(cfg)
        print(f"설정에 저장했습니다: {config_path()}")

    if host and is_awake(host, port):
        print(f"이미 켜져 있습니다: {host}")
        return 0

    try:
        sent = send_magic(mac, broadcast)
    except WakeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    print(f"매직 패킷을 {sent}번 보냈습니다 → {mac}")

    if not host:
        print("확인할 주소(--host)가 없어 켜졌는지는 확인하지 않습니다.")
        return 0

    print(f"{host} 이(가) 응답할 때까지 최대 {args.wait:.0f}초 기다립니다…")
    if wait_awake(host, port, timeout=args.wait):
        print("켜졌습니다.")
        return 0
    print(
        "응답이 없습니다. 확인할 것:\n"
        "  · 메인보드(BIOS)에서 Wake on LAN 활성화\n"
        "  · 윈도우: 랜카드 속성 → 전원 관리 → 'Magic Packet 에서만 깨우기'\n"
        "  · 윈도우: `powercfg /h off` (빠른 시작·최대 절전 끄기)\n"
        "  · 유선 랜인지 (무선은 대부분 안 됩니다)",
        file=sys.stderr,
    )
    return 1


def cmd_logout() -> int:
    removed = False
    for path in (Path(f"{session_path()}.session"), Path(f"{session_path()}.session-journal")):
        if path.exists():
            path.unlink()
            removed = True
    print("로그인 세션을 삭제했습니다." if removed else "삭제할 세션이 없습니다.")
    return 0


async def build_control_router(
    client,
    cfg: Config,
    args: argparse.Namespace,
    reporter: ConsoleReporter,
    fallback_help: bool = True,
):
    """텔레그램 명령 처리기를 만든다(내 계정만 쓸 수 있도록 번호를 넣는다)."""
    from .control import ControlRouter
    from .program import ProgramManager, load_programs

    owner_ids = set()
    try:
        me = await client.get_me()
        if getattr(me, "id", None):
            owner_ids.add(int(me.id))
    except Exception as exc:  # noqa: BLE001 - 못 구해도 '저장한 메시지'는 나만 쓴다
        reporter.log(f"내 계정 번호를 확인하지 못했습니다(무시): {exc}", "warn")
    for value in cfg.allow_senders or []:
        try:
            owner_ids.add(int(value))
        except (TypeError, ValueError):
            continue

    allow_power = bool(cfg.allow_power)
    if args.power:
        allow_power = True
    if args.no_power:
        allow_power = False

    manager = ProgramManager(load_programs(cfg.programs))
    return ControlRouter(
        manager,
        console=reporter,
        allow_power=allow_power,
        owner_ids=owner_ids,
        fallback_help=fallback_help,
        stop_bot_first=bool(cfg.stop_bot_first),
    )


async def run_control_mode(
    command: str,
    client,
    cfg: Config,
    args: argparse.Namespace,
    reporter: ConsoleReporter,
) -> int:
    """`remote`(집 컴퓨터)와 `waker`(항상 켜진 보조기기) 모드."""
    from .control import CONTROL_HELP, WakeRouter, run_control

    if command == "waker":
        from .wake import WakeError, pretty_mac

        mac = args.mac or cfg.wake_mac
        if not mac:
            reporter.log(
                "깨울 컴퓨터의 랜카드 주소(MAC)가 필요합니다. "
                "예: python -m tgdl waker --mac A1-B2-C3-D4-E5-F6 --host 192.168.0.10",
                "error",
            )
            return 2
        try:
            mac = pretty_mac(mac)
        except WakeError as exc:
            reporter.log(str(exc), "error")
            return 2

        host = args.host or cfg.wake_host
        port = args.wake_port if args.wake_port is not None else cfg.wake_port
        owner_ids = set()
        try:
            me = await client.get_me()
            if getattr(me, "id", None):
                owner_ids.add(int(me.id))
        except Exception:  # noqa: BLE001
            pass
        for value in cfg.allow_senders or []:
            try:
                owner_ids.add(int(value))
            except (TypeError, ValueError):
                continue

        router = WakeRouter(
            mac,
            host=host,
            port=int(port or 0),
            broadcast=args.broadcast or cfg.wake_broadcast,
            console=reporter,
            owner_ids=owner_ids,
            timeout=args.wait,
        )
        reporter.log(f"깨울 대상: {mac}" + (f" · 확인 주소 {host}" if host else ""), "ok")
        reporter.log("이 기기는 계속 켜 두세요. 여기서 집 컴퓨터를 깨웁니다.")
        greeting = (
            "📡 깨우기 대기 중입니다.\n"
            f"집 컴퓨터({mac})를 켜려면 `/깨우기` 를 보내세요."
        )
        try:
            await run_control(
                client,
                reporter,
                router,
                chat=args.chat,
                poll_interval=args.poll,
                debug=args.debug,
                greeting=greeting,
            )
        except KeyboardInterrupt:
            reporter.log("깨우기 대기를 멈췄습니다.", "warn")
        return 0

    router = await build_control_router(client, cfg, args, reporter)
    if router.manager.empty:
        reporter.log(
            "켜고 끌 프로그램이 등록되지 않았습니다. 전원 명령만 쓸 수 있습니다.", "warn"
        )
        reporter.log('등록: python -m tgdl bot add "C:\\매매봇\\bot.py"')
    else:
        reporter.log("등록된 프로그램: " + " · ".join(router.manager.names), "ok")
    if router.allow_power:
        reporter.log("전원 명령(/절전 /종료 /재부팅)을 허용했습니다.", "warn")
    else:
        reporter.log("전원 명령은 꺼져 있습니다(--power 로 켤 수 있습니다).")

    greeting = "✅ 원격 조종을 시작했습니다.\n\n" + router.status_text() + "\n\n" + CONTROL_HELP
    try:
        await run_control(
            client,
            reporter,
            router,
            chat=args.chat,
            poll_interval=args.poll,
            debug=args.debug,
            greeting=greeting,
        )
    except KeyboardInterrupt:
        reporter.log("원격 조종을 멈췄습니다.", "warn")
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

    if command in {"remote", "waker"}:
        try:
            return await run_control_mode(command, client, cfg, args, reporter)
        finally:
            await client.disconnect()

    folder = ensure_download_dir(cfg, reporter)
    downloader = make_downloader(client, cfg, args, reporter)
    reporter.log(f"저장 폴더: {folder}")

    if command == "listen":
        from .listen import run_listen

        router = None
        if args.control:
            router = await build_control_router(client, cfg, args, reporter, fallback_help=False)
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
                commands=router,
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

    if command == "bot":
        return cmd_bot(args)

    if command == "wake":
        return cmd_wake(args)

    if command == "waker" and not (args.mac or load_config().wake_mac):
        # 텔레그램에 접속하기 전에 미리 알려 준다(로그인까지 갔다가 실패하면 답답하다).
        print(
            "깨울 컴퓨터의 랜카드 주소(MAC)가 필요합니다.\n"
            "  집 컴퓨터에서 `ipconfig /all` → 이더넷의 '물리적 주소'\n"
            "  예: python -m tgdl waker --mac A1-B2-C3-D4-E5-F6 --host 192.168.0.10",
            file=sys.stderr,
        )
        return 2

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
