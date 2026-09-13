"""CLI 인자 처리 테스트(네트워크 없이). `python tests/test_cli.py`"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.cli import (  # noqa: E402
    apply_overrides,
    build_parser,
    collect_targets,
    ensure_download_dir,
    split_argv,
)
from tgdl.config import (  # noqa: E402
    Config,
    default_download_dir,
    on_termux,
    parse_proxy,
)
from tgdl.reporter import ConsoleReporter, human_size, human_time  # noqa: E402


def parse(argv):
    command, rest = split_argv(argv)
    return command, build_parser().parse_args(rest)


def test_split_argv_commands():
    assert split_argv(["watch"]) == ("watch", [])
    assert split_argv(["gui", "--all"]) == ("gui", ["--all"])
    assert split_argv([]) == ("get", [])
    assert split_argv(["https://t.me/a/1"]) == ("get", ["https://t.me/a/1"])


def test_links_as_positional_args():
    command, args = parse(["https://t.me/a/1", "https://t.me/c/2/3"])
    assert command == "get"
    assert args.targets == ["https://t.me/a/1", "https://t.me/c/2/3"]
    links = collect_targets(args, ConsoleReporter(live=False))
    assert [link.describe() for link in links] == [
        "@a / 메시지 1",
        "비공개채널 2 / 메시지 3",
    ]


def test_flags_override_config():
    command, args = parse(["--all", "-c", "5", "--flat", "--limit", "9", "-o", "/tmp/tgdl-x"])
    cfg = apply_overrides(Config(), args)
    assert cfg.media == "all"
    assert cfg.concurrency == 5
    assert cfg.per_chat_folder is False
    assert cfg.limit == 9
    assert cfg.download_dir == "/tmp/tgdl-x"


def test_concurrency_is_clamped():
    _, args = parse(["-c", "99"])
    assert apply_overrides(Config(), args).concurrency == 8


def test_collect_targets_from_file_and_bad_input():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "links.txt"
        path.write_text(
            "https://t.me/a/1\n\n# 메모\nhttps://t.me/a/1\nhttps://t.me/b/2\n",
            encoding="utf-8",
        )
        _, args = parse(["--from-file", str(path), "not-a-link"])
        links = collect_targets(args, ConsoleReporter(live=False))
        assert len(links) == 2, [link.raw for link in links]


def test_proxy_parsing():
    proxy = parse_proxy("socks5://user:pw@127.0.0.1:1080")
    assert proxy["proxy_type"] == "socks5"
    assert proxy["addr"] == "127.0.0.1" and proxy["port"] == 1080
    assert proxy["username"] == "user" and proxy["password"] == "pw"
    assert parse_proxy("127.0.0.1:9050")["proxy_type"] == "socks5"
    assert parse_proxy("") is None
    for bad in ("ftp://host:1", "socks5://nohost"):
        try:
            parse_proxy(bad)
        except ValueError:
            continue
        raise AssertionError(f"거부해야 합니다: {bad}")


def test_config_env_and_clamps(tmpdir=None):
    from tgdl import config as config_module

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TGDL_HOME"] = tmp
        os.environ["TGDL_API_ID"] = "12345"
        os.environ["TGDL_API_HASH"] = "abc"
        try:
            cfg = config_module.load_config()
            assert cfg.api_id == 12345 and cfg.api_hash == "abc"
            assert cfg.ready is True
            saved = config_module.save_config(cfg)
            assert saved.exists()
            assert config_module.session_path().parent.exists()
        finally:
            for key in ("TGDL_HOME", "TGDL_API_ID", "TGDL_API_HASH"):
                os.environ.pop(key, None)


def test_human_helpers():
    assert human_size(0) == "0B"
    assert human_size(1536) == "1.5KB"
    assert human_size(5 * 1024**3).endswith("GB")
    assert human_time(0) == "--:--"
    assert human_time(75) == "01:15"
    assert human_time(3725) == "1:02:05"


def test_termux_gets_shared_download_folder():
    """Termux 내부 폴더에 저장하면 갤러리가 볼 수 없으므로 공용 폴더를 써야 한다."""
    saved = os.environ.get("PREFIX")
    try:
        os.environ.pop("PREFIX", None)
        if not on_termux():  # 실제 Termux 에서 테스트할 때는 건너뛴다
            assert default_download_dir().endswith(os.path.join("Downloads", "telegram"))
        os.environ["PREFIX"] = "/data/data/com.termux/files/usr"
        assert on_termux() is True
        assert default_download_dir() == "/sdcard/Download/telegram"
        assert Config().download_dir == "/sdcard/Download/telegram"
    finally:
        os.environ.pop("PREFIX", None)
        if saved is not None:
            os.environ["PREFIX"] = saved


def test_download_dir_falls_back_when_not_writable():
    reporter = ConsoleReporter(live=False)
    saved_home = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as home:
        try:
            os.environ["HOME"] = home
            cfg = Config(download_dir="/proc/tgdl-cannot-create/here")
            folder = ensure_download_dir(cfg, reporter)
            assert folder == Path(home) / "tgdl-downloads"
            assert folder.is_dir()
            assert cfg.download_dir == str(folder), "설정도 대체 폴더로 갱신돼야 한다"

            good = Path(home) / "받은영상"
            cfg2 = Config(download_dir=str(good))
            assert ensure_download_dir(cfg2, reporter) == good
            assert good.is_dir()
            assert not list(good.iterdir()), "쓰기 확인용 파일은 남기지 않는다"
        finally:
            os.environ.pop("HOME", None)
            if saved_home is not None:
                os.environ["HOME"] = saved_home


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
