"""병렬 다운로드 테스트(가짜 연결 사용). `python tests/test_fastdl.py`"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import errors  # noqa: E402

from tgdl.fastdl import (  # noqa: E402
    ParallelUnavailable,
    clamp_connections,
    parallel_download,
)

CHUNK = 4096  # 테스트용 작은 조각


class Reply:
    def __init__(self, data: bytes):
        self.bytes = data


class FakeSender:
    """요청한 구간을 돌려주는 가짜 연결."""

    def __init__(self, payload: bytes, delay: float = 0.0, fail_on_call: int = 0,
                 error: Exception = None, flood_once: bool = False):
        self.payload = payload
        self.delay = delay
        self.fail_on_call = fail_on_call
        self.error = error or RuntimeError("연결 끊김")
        self.flood_once = flood_once
        self.calls = 0
        self.offsets: list = []

    async def __call__(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.flood_once and self.calls == 1:
            self.flood_once = False
            raise errors.FloodWaitError(request=None)
        if self.fail_on_call and self.calls == self.fail_on_call:
            raise self.error
        self.offsets.append(request.offset)
        return Reply(self.payload[request.offset : request.offset + request.limit])


class CdnSender:
    async def __call__(self, request):
        class Redirect:  # bytes 속성이 없는 응답
            pass

        return Redirect()


def run(coro):
    return asyncio.run(coro)


def test_clamp_connections():
    assert clamp_connections(4) == 4
    assert clamp_connections(0) == 1
    assert clamp_connections(999) == 16
    assert clamp_connections("이상한값") == 4
    assert clamp_connections(None) == 4


def test_parallel_download_writes_exact_bytes():
    payload = os.urandom(CHUNK * 9 + 123)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        seen: list = []
        # 연결마다 속도를 달리해 조각이 뒤섞여 도착하게 만든다
        senders = [FakeSender(payload, delay=d) for d in (0.0, 0.003, 0.001, 0.002)]

        with open(target, "wb") as handle:
            written = run(
                parallel_download(
                    senders, "LOC", handle,
                    total=len(payload), chunk_size=CHUNK,
                    on_progress=seen.append,
                )
            )

        assert written == len(payload)
        assert target.read_bytes() == payload, "순서가 뒤섞여도 내용이 정확해야 한다"
        assert seen and seen[-1] == len(payload)
        assert sum(len(s.offsets) for s in senders) == 10, "조각 수가 맞아야 한다"
        assert len({o for s in senders for o in s.offsets}) == 10, "같은 조각을 두 번 받지 않는다"


def test_start_offset_is_respected():
    payload = os.urandom(CHUNK * 5)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        target.write_bytes(payload[: CHUNK * 2])  # 앞부분은 이미 받아둔 상태

        senders = [FakeSender(payload) for _ in range(3)]
        with open(target, "r+b") as handle:
            written = run(
                parallel_download(
                    senders, "LOC", handle,
                    total=len(payload), start=CHUNK * 2, chunk_size=CHUNK,
                )
            )

        assert written == CHUNK * 3
        assert target.read_bytes() == payload
        assert min(o for s in senders for o in s.offsets) == CHUNK * 2, "이미 받은 구간은 다시 안 받는다"


def broken_senders(payload):
    """2번째 연결만 실패하는 연결 묶음.

    모든 연결에 지연을 줘서 워커마다 조각을 하나씩 맡게 한다(실제 네트워크와
    같은 상황). 지연이 없으면 빠른 워커 하나가 조각을 다 가져가 버린다.
    """
    return [
        FakeSender(payload, delay=0.001),                      # 0번 조각 성공
        FakeSender(payload, delay=0.05, fail_on_call=1),       # 1번 조각 실패(늦게)
        FakeSender(payload, delay=0.001),                      # 2번 조각 성공
        FakeSender(payload, delay=0.001),                      # 3번 조각 성공
    ]


def test_failure_leaves_no_holes():
    """중간 조각이 실패하면, 빈틈 없는 앞부분까지만 남아야 한다."""
    payload = os.urandom(CHUNK * 8)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        senders = broken_senders(payload)
        with open(target, "wb") as handle:
            try:
                run(
                    parallel_download(
                        senders, "LOC", handle,
                        total=len(payload), chunk_size=CHUNK,
                    )
                )
            except RuntimeError as exc:
                assert "연결 끊김" in str(exc)
            else:
                raise AssertionError("실패가 전달되어야 합니다")

        size = target.stat().st_size
        assert size == CHUNK, f"빈틈 없는 앞부분만 남아야 하는데 {size} 바이트"
        assert target.read_bytes() == payload[:CHUNK], "남은 부분은 내용이 정확해야 한다"


def test_resume_after_failure_completes_file():
    """실패 후 다시 받으면 이어서 완성되어야 한다(구멍 없는 이어받기)."""
    payload = os.urandom(CHUNK * 8)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        broken = broken_senders(payload)
        with open(target, "wb") as handle:
            try:
                run(parallel_download(broken, "LOC", handle, total=len(payload), chunk_size=CHUNK))
            except RuntimeError:
                pass

        resume_from = (target.stat().st_size // CHUNK) * CHUNK
        with open(target, "r+b") as handle:
            run(
                parallel_download(
                    [FakeSender(payload) for _ in range(3)], "LOC", handle,
                    total=len(payload), start=resume_from, chunk_size=CHUNK,
                )
            )

        assert target.read_bytes() == payload, "이어받아 완성된 파일이 원본과 같아야 한다"


def test_flood_wait_is_retried():
    payload = os.urandom(CHUNK * 2)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        sender = FakeSender(payload, flood_once=True)
        with open(target, "wb") as handle:
            run(parallel_download([sender], "LOC", handle, total=len(payload), chunk_size=CHUNK))
        assert target.read_bytes() == payload
        assert sender.calls == 3, "속도 제한 한 번을 재시도해 총 3번 호출"


def test_cdn_response_falls_back():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.bin"
        with open(target, "wb") as handle:
            try:
                run(parallel_download([CdnSender()], "LOC", handle, total=CHUNK, chunk_size=CHUNK))
            except ParallelUnavailable as exc:
                assert "지원하지 않는 응답" in str(exc)
                return
        raise AssertionError("CDN 응답은 기본 방식으로 넘겨야 합니다")


def test_rejects_bad_arguments():
    with tempfile.TemporaryDirectory() as tmp:
        with open(Path(tmp) / "x.bin", "wb") as handle:
            for kwargs, expected in (
                ({"total": 0}, ParallelUnavailable),
                ({"total": CHUNK, "chunk_size": 1000}, ValueError),
                ({"total": CHUNK, "chunk_size": 1024 * 1024}, ValueError),
            ):
                try:
                    run(parallel_download([FakeSender(b"")], "LOC", handle, **kwargs))
                except expected:
                    continue
                raise AssertionError(f"{kwargs} 는 거부해야 합니다")

        with open(Path(tmp) / "y.bin", "wb") as handle:
            try:
                run(parallel_download([], "LOC", handle, total=CHUNK, chunk_size=CHUNK))
            except ParallelUnavailable:
                pass
            else:
                raise AssertionError("연결이 없으면 거부해야 합니다")


def test_sender_pool_reuses_connections():
    """파일마다 연결을 새로 만들면 권한 내보내기 요청이 과도해진다."""
    import tgdl.fastdl as fastdl

    created: list = []

    class FakeConn:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    async def fake_open(client, dc_id):
        conn = FakeConn()
        created.append(conn)
        return conn

    original = fastdl._open_sender
    fastdl._open_sender = fake_open
    try:
        async def scenario():
            pool = fastdl.SenderPool(client=None, connections=3)
            first = await pool.sends_for(2)
            assert len(first) == 3 and len(created) == 3

            await pool.sends_for(2)
            assert len(created) == 3, "같은 DC 는 연결을 재사용해야 한다"

            await pool.sends_for(5)
            assert len(created) == 6, "다른 DC 는 따로 연결한다"

            await pool.invalidate(2)
            assert sum(conn.disconnected for conn in created) == 3
            await pool.sends_for(2)
            assert len(created) == 9, "버린 DC 는 다음에 새로 만든다"

            await pool.close()
            assert all(conn.disconnected for conn in created)

        run(scenario())
    finally:
        fastdl._open_sender = original


def test_pool_without_any_connection_falls_back():
    import tgdl.fastdl as fastdl

    async def always_fail(client, dc_id):
        raise OSError("연결 거부")

    original = fastdl._open_sender
    fastdl._open_sender = always_fail
    try:
        async def scenario():
            pool = fastdl.SenderPool(client=None, connections=4)
            try:
                await pool.sends_for(2)
            except ParallelUnavailable as exc:
                assert "연결을 열 수 없습니다" in str(exc)
                return
            raise AssertionError("연결이 하나도 없으면 기본 방식으로 넘겨야 합니다")

        run(scenario())
    finally:
        fastdl._open_sender = original


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
