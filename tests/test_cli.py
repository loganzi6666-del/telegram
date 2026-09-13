"""CLI 인자 처리 테스트(네트워크 없이). `python tests/test_cli.py`"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.cli import apply_overrides, build_parser, collect_targets, split_argv  # noqa: E402
from tgdl.config import Config, parse_proxy  # noqa: E402
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
    links = collect_targets(args, ConsoleReporter(single_line=False))
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
        links = collect_targets(args, ConsoleReporter(single_line=False))
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
