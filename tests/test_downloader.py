"""다운로더 저장 로직 테스트(네트워크 없이 가짜 클라이언트 사용).

`python tests/test_downloader.py` 로 실행한다. telethon 이 설치되어 있어야 한다.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.downloader import CHUNK_SIZE, Downloader, LinkPump, sanitize  # noqa: E402
from tgdl.links import parse_link  # noqa: E402
from tgdl.reporter import Reporter  # noqa: E402


class FakeFile:
    def __init__(self, name, size, mime="video/mp4", ext=".mp4"):
        self.name = name
        self.size = size
        self.mime_type = mime
        self.ext = ext


class FakeMessage:
    def __init__(self, mid, size, name="clip.mp4", mime="video/mp4", caption=""):
        self.id = mid
        self.file = FakeFile(name, size, mime)
        self.media = object()
        self.message = caption
        self.date = datetime.datetime(2026, 2, 3)
        self.video = mime.startswith("video/")
        self.video_note = None
        self.gif = None
        self.grouped_id = None


class FakeEntity:
    def __init__(self, title="테스트 채널", eid=777):
        self.title = title
        self.id = eid
        self.username = None


class FakeClient:
    """요청한 offset 부터 정해진 내용을 흘려주는 가짜 클라이언트."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.offsets = []

    def iter_download(self, media, offset=0, chunk_size=CHUNK_SIZE):
        self.offsets.append(offset)
        payload = self.payload

        async def gen():
            position = offset
            while position < len(payload):
                yield payload[position : position + chunk_size]
                position += chunk_size

        return gen()


class CollectingReporter(Reporter):
    def __init__(self):
        self.logs = []
        self.done = []
        self.errors = []

    def log(self, message, level="info"):
        self.logs.append((level, message))

    def finished(self, key, label, path, skipped=False):
        self.done.append((label, str(path), skipped))

    def failed(self, key, label, error):
        self.errors.append((label, error))


def make_downloader(tmp, client, reporter, **kwargs):
    return Downloader(client, reporter, out_dir=tmp, concurrency=1, **kwargs)


def test_sanitize():
    assert sanitize('a/b:c*?"<>|x.mp4') == "a_b_c______x.mp4"
    assert sanitize("  ..  ") == "untitled"
    assert sanitize("여러   공백\t정리\n") == "여러 공백 정리"
    assert len(sanitize("가" * 500)) == 120
    for char in '<>:"/\\|?*':
        assert char not in sanitize(f"이름{char}.mp4")


def test_download_writes_file_and_removes_part():
    payload = os.urandom(CHUNK_SIZE * 2 + 123)
    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient(payload)
        reporter = CollectingReporter()
        downloader = make_downloader(tmp, client, reporter)
        msg = FakeMessage(56, len(payload), "영상.mp4")
        entity = FakeEntity()
        asyncio.run(downloader._download(msg, entity))

        target = Path(tmp) / "테스트 채널" / "20260203_56_영상.mp4"
        assert target.exists(), f"파일이 없습니다: {list(Path(tmp).rglob('*'))}"
        assert target.read_bytes() == payload
        assert not target.with_name(target.name + ".part").exists()
        assert downloader.stats.downloaded == 1
        assert downloader.stats.bytes == len(payload)
        assert client.offsets == [0]


def test_skip_existing_same_size():
    payload = os.urandom(4096)
    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient(payload)
        reporter = CollectingReporter()
        downloader = make_downloader(tmp, client, reporter)
        msg, entity = FakeMessage(7, len(payload)), FakeEntity()

        asyncio.run(downloader._download(msg, entity))
        asyncio.run(downloader._download(msg, entity))

        assert downloader.stats.downloaded == 1
        assert downloader.stats.skipped == 1
        assert reporter.done[-1][2] is True  # skipped 플래그
        assert client.offsets == [0]  # 두 번째는 네트워크를 쓰지 않음


def test_resume_from_partial_part_file():
    payload = os.urandom(CHUNK_SIZE * 3)
    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient(payload)
        reporter = CollectingReporter()
        downloader = make_downloader(tmp, client, reporter)
        msg, entity = FakeMessage(9, len(payload), "big.mp4"), FakeEntity()

        # 중간에 끊긴 .part 파일을 미리 만들어 둔다(경계에 걸치지 않는 길이).
        dest = downloader._dest_path(msg, entity)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(payload[: CHUNK_SIZE + 500])

        asyncio.run(downloader._download(msg, entity))

        assert client.offsets == [CHUNK_SIZE], client.offsets  # 정렬된 지점부터 재개
        assert dest.read_bytes() == payload
        assert any("이어받기" in msg for _, msg in reporter.logs)


def test_media_filter():
    with tempfile.TemporaryDirectory() as tmp:
        downloader = make_downloader(tmp, FakeClient(b""), CollectingReporter())
        video = FakeMessage(1, 10, "a.mp4", "video/mp4")
        photo = FakeMessage(2, 10, "a.jpg", "image/jpeg")
        photo.video = False
        assert downloader._wanted(video) is True
        assert downloader._wanted(photo) is False

        downloader.media = "all"
        assert downloader._wanted(photo) is True

        empty = FakeMessage(3, 0, "x.mp4")
        empty.media = None
        assert downloader._wanted(empty) is False


def test_flat_mode_and_caption_filename():
    payload = os.urandom(1024)
    with tempfile.TemporaryDirectory() as tmp:
        downloader = make_downloader(
            tmp, FakeClient(payload), CollectingReporter(), per_chat_folder=False
        )
        msg = FakeMessage(11, len(payload), name="", caption="제목: 여행 영상\n두번째줄")
        msg.file.name = None
        path = downloader._dest_path(msg, FakeEntity())
        assert path.parent == Path(tmp)
        assert path.name.startswith("20260203_11_")
        assert "여행 영상" in path.name and "두번째줄" not in path.name


def test_link_pump_processes_queue():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            reporter = CollectingReporter()
            downloader = make_downloader(tmp, FakeClient(b""), reporter)
            processed = []

            async def fake_process(link):
                processed.append(link.describe())

            downloader.process = fake_process  # type: ignore[assignment]
            pump = LinkPump(downloader, workers=2)
            added = pump.submit(
                [parse_link("https://t.me/c/1/2"), parse_link("https://t.me/c/1/2")]
            )
            assert added == 1, "중복 링크는 한 번만 처리해야 합니다"
            added += pump.submit([parse_link("https://t.me/chan/5")])
            await pump.join()
            await pump.close()
            assert len(processed) == 2

    asyncio.run(scenario())


def test_failure_is_recorded_not_raised():
    class BrokenClient(FakeClient):
        def iter_download(self, media, offset=0, chunk_size=CHUNK_SIZE):
            async def gen():
                raise ConnectionError("네트워크 끊김")
                yield b""  # pragma: no cover

            return gen()

    with tempfile.TemporaryDirectory() as tmp:
        reporter = CollectingReporter()
        downloader = make_downloader(tmp, BrokenClient(b""), reporter, max_retries=0)
        asyncio.run(downloader._download(FakeMessage(4, 100), FakeEntity()))
        assert downloader.stats.failed == 1
        assert reporter.errors and "네트워크 끊김" in reporter.errors[0][1]
        assert not list(Path(tmp).rglob("*.mp4")), "실패한 파일은 남기지 않는다"


def test_after_save_hook_gets_final_path():
    """안드로이드 갤러리 갱신 훅은 .part 가 아닌 최종 파일 경로를 받아야 한다."""
    payload = os.urandom(4096)
    with tempfile.TemporaryDirectory() as tmp:
        downloader = make_downloader(tmp, FakeClient(payload), CollectingReporter())
        seen = []
        downloader.after_save = seen.append
        asyncio.run(downloader._download(FakeMessage(12, len(payload)), FakeEntity()))

        assert len(seen) == 1
        assert seen[0].suffix == ".mp4" and ".part" not in seen[0].name
        assert seen[0].exists()


def test_after_save_failure_does_not_break_download():
    payload = os.urandom(4096)
    with tempfile.TemporaryDirectory() as tmp:
        reporter = CollectingReporter()
        downloader = make_downloader(tmp, FakeClient(payload), reporter)

        def boom(path):
            raise RuntimeError("갤러리 갱신 실패")

        downloader.after_save = boom
        asyncio.run(downloader._download(FakeMessage(13, len(payload)), FakeEntity()))

        assert downloader.stats.downloaded == 1, "후처리 실패는 다운로드를 망치지 않는다"
        assert downloader.stats.failed == 0
        assert any("무시" in message for _, message in reporter.logs)


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
