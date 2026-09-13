"""링크 파서 테스트. pytest 없이도 `python tests/test_links.py` 로 실행된다."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.links import LinkError, extract_links, parse_link  # noqa: E402


def test_public_message():
    link = parse_link("https://t.me/somechannel/123")
    assert link.kind == "public"
    assert link.username == "somechannel"
    assert link.message_ids == [123]
    assert link.topic_id is None


def test_public_without_scheme_and_trailing_punctuation():
    link = parse_link("t.me/somechannel/123.")
    assert link.username == "somechannel"
    assert link.message_ids == [123]


def test_private_channel():
    link = parse_link("https://t.me/c/1234567890/56")
    assert link.kind == "private"
    assert link.channel_id == 1234567890
    assert link.message_ids == [56]


def test_private_forum_topic():
    link = parse_link("https://t.me/c/1234567890/45/56")
    assert link.channel_id == 1234567890
    assert link.topic_id == 45
    assert link.message_ids == [56]


def test_range_and_list():
    assert parse_link("https://t.me/chan/100-103").message_ids == [100, 101, 102, 103]
    assert parse_link("https://t.me/chan/103~100").message_ids == [100, 101, 102, 103]
    assert parse_link("https://t.me/chan/1,5,9").message_ids == [1, 5, 9]
    assert parse_link("https://t.me/c/999/7..9").message_ids == [7, 8, 9]


def test_range_too_wide():
    try:
        parse_link("https://t.me/chan/1-999999")
    except LinkError:
        pass
    else:  # pragma: no cover
        raise AssertionError("넓은 범위는 거부해야 합니다")


def test_invite_links():
    for raw in ("https://t.me/+AbCdEfGhIj", "t.me/joinchat/AbCdEfGhIj"):
        link = parse_link(raw)
        assert link.kind == "invite"
        assert link.invite_hash == "AbCdEfGhIj"


def test_comment_link():
    link = parse_link("https://t.me/chan/55?comment=77")
    assert link.message_ids == [55]
    assert link.comment_id == 77


def test_thread_query():
    link = parse_link("https://t.me/chan/99?thread=12&single")
    assert link.topic_id == 12
    assert link.message_ids == [99]


def test_web_preview_prefix():
    link = parse_link("https://t.me/s/chan/42")
    assert link.username == "chan"
    assert link.message_ids == [42]


def test_tg_scheme():
    link = parse_link("tg://resolve?domain=chan&post=9")
    assert link.username == "chan" and link.message_ids == [9]
    link = parse_link("tg://privatepost?channel=1234&post=7")
    assert link.kind == "private" and link.channel_id == 1234 and link.message_ids == [7]
    link = parse_link("tg://join?invite=Zz9")
    assert link.kind == "invite" and link.invite_hash == "Zz9"


def test_channel_only():
    link = parse_link("https://t.me/chan")
    assert link.message_ids == []
    assert "전체" in link.describe()


def test_rejects_non_telegram():
    for raw in ("https://example.com/video/1", "그냥 텍스트", ""):
        try:
            parse_link(raw)
        except LinkError:
            continue
        raise AssertionError(f"거부해야 합니다: {raw!r}")


def test_extract_links_from_blob():
    blob = """
    친구가 보낸 링크: https://t.me/c/1234567890/56 그리고
    https://t.me/chan/1 (중복) https://t.me/chan/1
    https://example.com/nope
    """
    links = extract_links(blob)
    assert len(links) == 2
    assert links[0].channel_id == 1234567890
    assert links[1].username == "chan"


def test_dedupe_key():
    first = parse_link("https://t.me/chan/1")
    second = parse_link("t.me/chan/1")
    assert first.key == second.key


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
