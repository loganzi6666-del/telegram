"""같은 와이파이 전송용 웹 서버 테스트. `python tests/test_serve.py`"""

from __future__ import annotations

import http.client
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl.serve import Handler, Server, human_size, lan_ip  # noqa: E402

CHANNEL_URL = "/%EB%82%B4%20%EC%B1%84%EB%84%90/"  # "/내 채널/"
VIDEO_URL = CHANNEL_URL + "20260628_938_%ED%95%9C%EA%B8%80%20%EC%98%81%EC%83%81.mp4"


class Fixture:
    """빈 포트에 서버를 띄우는 도우미."""

    def __init__(self, folder: str):
        self.server = Server(("127.0.0.1", 0), partial(Handler, directory=folder))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path: str):
        # 프록시 설정을 무시하고 로컬 서버에 직접 붙는다.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def raw_get(self, path: str):
        """경로를 정규화하지 않고 그대로 보낸다(경로 탈출 시험용)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("GET", path, skip_host=False, skip_accept_encoding=True)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sample_folder(tmp: str) -> Path:
    channel = Path(tmp) / "내 채널"
    channel.mkdir()
    (channel / "받는중.mp4.part").write_bytes(b"x" * 10)
    (channel / ".숨김").write_text("무시", encoding="utf-8")
    return channel


def test_human_size():
    assert human_size(0) == "0B"
    assert human_size(1536) == "1.5KB"
    assert human_size(724 * 1024**2) == "724.0MB"


def test_lan_ip_looks_like_an_address():
    parts = lan_ip().split(".")
    assert len(parts) == 4 and all(part.isdigit() for part in parts)


def test_listing_shows_files_and_hides_noise():
    with tempfile.TemporaryDirectory() as tmp:
        channel = sample_folder(tmp)
        (channel / "20260628_938_한글 영상.mp4").write_bytes(b"v" * 20000)
        fixture = Fixture(tmp)
        try:
            root = fixture.get("/").read().decode("utf-8")
            assert "내 채널" in root

            listing = fixture.get(CHANNEL_URL).read().decode("utf-8")
            assert "20260628_938_한글 영상.mp4" in listing
            assert "19.5KB" in listing
            # 받는 중인 파일은 보여주되 누를 수 없어야 한다
            assert "받는 중" in listing
            assert ".part\"" not in listing
            assert "숨김" not in listing
        finally:
            fixture.close()


def test_download_saves_instead_of_playing():
    payload = os.urandom(20000)
    with tempfile.TemporaryDirectory() as tmp:
        channel = sample_folder(tmp)
        (channel / "20260628_938_한글 영상.mp4").write_bytes(payload)
        fixture = Fixture(tmp)
        try:
            response = fixture.get(VIDEO_URL)
            disposition = response.headers["Content-Disposition"]
            # 아이폰 사파리가 재생하지 않고 '파일' 앱에 저장하도록
            assert disposition.startswith("attachment;")
            assert "filename*=UTF-8''" in disposition
            assert response.read() == payload, "파일 내용이 그대로 전달되어야 한다"
        finally:
            fixture.close()


def test_directory_escape_is_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "안전.txt").write_text("ok", encoding="utf-8")
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, "비밀.txt").write_text("보이면 안 됨", encoding="utf-8")
            fixture = Fixture(tmp)
            try:
                for attack in (
                    "/../%EB%B9%84%EB%B0%80.txt",
                    "/..%2f%EB%B9%84%EB%B0%80.txt",
                    "/%2e%2e/%EB%B9%84%EB%B0%80.txt",
                    "/../../etc/passwd",
                ):
                    status, body = fixture.raw_get(attack)
                    text = body.decode("utf-8", "replace")
                    assert "보이면 안 됨" not in text, f"{attack} 로 상위 폴더가 노출됨"
                    assert "root:" not in text, f"{attack} 로 시스템 파일이 노출됨"
            finally:
                fixture.close()


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
