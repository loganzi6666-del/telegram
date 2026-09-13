"""여러 연결로 동시에 받는 빠른 다운로드.

텔레그램은 연결 하나당 속도를 제한한다. 그래서 연결을 여러 개 열어 파일의
서로 다른 구간을 동시에 받으면 몇 배 빨라진다.

주의: 조각이 순서와 무관하게 도착하므로, 중간에 끊기면 파일에 '구멍'이 남는다.
그러면 이어받기가 깨지므로, 끝날 때 **빈틈 없이 채워진 앞부분까지만** 남기고
자른다. 이어받기는 항상 그 지점부터 안전하게 다시 시작한다.
"""

from __future__ import annotations

import asyncio
import math
from typing import Awaitable, Callable, List, Optional, Sequence

from telethon import errors, utils
from telethon.tl import functions

#: 한 번에 요청하는 크기. 4096의 배수여야 하고 512KB 를 넘을 수 없다.
CHUNK_SIZE = 512 * 1024

#: 기본 연결 수
DEFAULT_CONNECTIONS = 4
MAX_CONNECTIONS = 16

SendFunc = Callable[[object], Awaitable[object]]


class ParallelUnavailable(RuntimeError):
    """병렬 방식을 쓸 수 없어 기본(연결 1개) 방식으로 돌아가야 할 때."""


def clamp_connections(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CONNECTIONS
    return max(1, min(MAX_CONNECTIONS, number))


async def _request_chunk(
    send: SendFunc, location, offset: int, limit: int, retries: int
) -> bytes:
    """조각 하나를 받아온다. 일시적 오류는 스스로 재시도한다."""
    request = functions.upload.GetFileRequest(
        location, offset=offset, limit=limit, precise=False, cdn_supported=False
    )
    attempt = 0
    while True:
        try:
            result = await send(request)
        except errors.FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0)) + 1
            if wait > 300:
                raise
            await asyncio.sleep(wait)
            continue
        except (asyncio.TimeoutError, ConnectionError, OSError):
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(min(2**attempt, 15))
            continue

        data = getattr(result, "bytes", None)
        if data is None:
            # upload.FileCdnRedirect 처럼 우리가 다루지 않는 응답
            raise ParallelUnavailable(
                f"지원하지 않는 응답: {result.__class__.__name__}"
            )
        return data


async def parallel_download(
    sends: Sequence[SendFunc],
    location,
    handle,
    *,
    total: int,
    start: int = 0,
    chunk_size: int = CHUNK_SIZE,
    on_progress: Optional[Callable[[int], None]] = None,
    retries: int = 4,
) -> int:
    """``start`` 부터 ``total`` 까지를 여러 연결로 나눠 받아 ``handle`` 에 쓴다.

    반환값은 이번에 받은 바이트 수. 중단되면 빈틈 없는 앞부분까지만 남긴다.
    """
    if total <= 0:
        raise ParallelUnavailable("파일 크기를 알 수 없습니다")
    if not sends:
        raise ParallelUnavailable("사용할 연결이 없습니다")
    if chunk_size % 4096 or chunk_size > CHUNK_SIZE:
        raise ValueError("조각 크기는 4096의 배수이면서 512KB 이하여야 합니다")

    remaining = total - start
    if remaining <= 0:
        return 0

    chunk_count = math.ceil(remaining / chunk_size)
    completed: set = set()
    written = 0
    cursor = 0  # 다음에 가져갈 조각 번호

    def contiguous_end() -> int:
        """앞에서부터 빈틈 없이 이어지는 지점(파일 길이)."""
        index = 0
        while index in completed:
            index += 1
        return min(start + index * chunk_size, total)

    async def worker(send: SendFunc) -> None:
        nonlocal cursor, written
        while True:
            index = cursor
            cursor += 1
            if index >= chunk_count:
                return

            offset = start + index * chunk_size
            data = await _request_chunk(send, location, offset, chunk_size, retries)
            if data:
                handle.seek(offset)
                handle.write(data)
                written += len(data)
            completed.add(index)
            if on_progress is not None:
                on_progress(start + written)

    workers = [asyncio.create_task(worker(send)) for send in sends[:chunk_count]]
    try:
        # 하나라도 실패하면 곧바로 알리고 나머지를 정리한다.
        done, pending = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.exception() is not None:
                raise task.exception()
        return written
    finally:
        # 성공이면 total, 실패·중단이면 빈틈 없는 지점까지만 남긴다.
        try:
            handle.flush()
            handle.truncate(contiguous_end())
            handle.flush()
        except OSError:
            pass


async def _open_sender(client, dc_id: int):
    """지정한 DC 로 가는 새 연결 하나를 만든다."""
    from telethon.network import MTProtoSender

    session_dc = getattr(client.session, "dc_id", None)
    if dc_id is None or dc_id == session_dc:
        # 내 세션과 같은 DC 면 인증키를 그대로 쓰는 새 연결을 만든다.
        dc = await client._get_dc(dc_id or session_dc)
        sender = MTProtoSender(client.session.auth_key, loggers=client._log)
        await sender.connect(
            client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=client._log,
                proxy=getattr(client, "_proxy", None),
            )
        )
        return sender
    # 다른 DC 면 텔레그램이 정한 절차(권한 내보내기)를 따른다.
    return await client._create_exported_sender(dc_id)


async def _close_senders(senders: List) -> None:
    for sender in senders:
        try:
            await sender.disconnect()
        except Exception:  # noqa: BLE001 - 정리 실패는 무시
            pass


def _make_send(client, sender) -> SendFunc:
    """RPC 오류를 제대로 변환해주는 client._call 을 쓰되, 없으면 직접 보낸다."""
    call = getattr(client, "_call", None)
    if call is not None:
        async def send(request):
            return await call(sender, request)
    else:  # pragma: no cover - 아주 오래된 telethon
        async def send(request):
            return await sender.send(request)

    return send


class SenderPool:
    """DC 별로 연결을 열어두고 여러 파일에서 재사용한다.

    파일마다 연결을 새로 만들면 권한 내보내기(auth.exportAuthorization) 요청이
    너무 잦아져 텔레그램이 제한을 걸 수 있다. 그래서 한 번 만든 연결은
    :meth:`close` 할 때까지 계속 쓴다.
    """

    def __init__(self, client, connections: int = DEFAULT_CONNECTIONS) -> None:
        self.client = client
        self.connections = clamp_connections(connections)
        self._pools: dict = {}
        self._lock = asyncio.Lock()

    async def sends_for(self, dc_id, count: Optional[int] = None) -> List[SendFunc]:
        """해당 DC 로 가는 연결들의 전송 함수 목록."""
        wanted = clamp_connections(count if count is not None else self.connections)
        async with self._lock:
            senders = self._pools.get(dc_id)
            if not senders:
                senders = []
                for _ in range(wanted):
                    try:
                        senders.append(await _open_sender(self.client, dc_id))
                    except Exception as exc:  # noqa: BLE001
                        if not senders:
                            raise ParallelUnavailable(
                                f"연결을 열 수 없습니다: {exc}"
                            ) from exc
                        break  # 열린 것만으로 진행한다
                self._pools[dc_id] = senders
        return [_make_send(self.client, sender) for sender in senders[:wanted]]

    async def invalidate(self, dc_id) -> None:
        """고장난 연결을 버린다. 다음 시도에서 새로 만든다."""
        async with self._lock:
            senders = self._pools.pop(dc_id, None)
        if senders:
            await _close_senders(senders)

    async def close(self) -> None:
        async with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for senders in pools:
            await _close_senders(senders)


async def download_parallel(
    pool: SenderPool,
    media,
    handle,
    *,
    total: int,
    start: int = 0,
    on_progress: Optional[Callable[[int], None]] = None,
) -> int:
    """미디어를 여러 연결로 동시에 받는다.

    구조적으로 불가능하면 :class:`ParallelUnavailable` 을 올린다. 부르는 쪽은
    기본(연결 1개) 방식으로 돌아가면 된다.
    """
    try:
        dc_id, location = utils.get_input_location(media)
    except Exception as exc:  # noqa: BLE001 - 위치를 못 구하면 기본 방식으로
        raise ParallelUnavailable(f"파일 위치를 알 수 없습니다: {exc}") from exc
    if location is None:
        raise ParallelUnavailable("파일 위치를 알 수 없습니다")

    needed = max(1, math.ceil(max(total - start, 1) / CHUNK_SIZE))
    sends = await pool.sends_for(dc_id, min(pool.connections, needed))
    try:
        return await parallel_download(
            sends, location, handle, total=total, start=start, on_progress=on_progress
        )
    except ParallelUnavailable:
        raise
    except Exception:
        # 연결이 상했을 수 있으니 버리고, 다음 시도에서 새로 만든다.
        await pool.invalidate(dc_id)
        raise
