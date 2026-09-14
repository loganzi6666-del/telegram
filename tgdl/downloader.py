"""실제 다운로드 로직.

- 링크 → 채널(엔티티) 해석 (공개 / 비공개 / 초대링크)
- 메시지 수집 (단건, 범위, 앨범, 댓글, 채널 전체 탐색)
- 동영상 파일을 이어받기 가능한 방식으로 저장
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from telethon import errors
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetDiscussionMessageRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import InputMessagesFilterVideo, PeerChannel

from .fastdl import (
    ParallelUnavailable,
    SenderPool,
    clamp_connections,
    download_parallel,
)
from .links import ParsedLink
from .reporter import Reporter, human_size

#: iter_download 의 chunk 크기. 4096의 배수여야 하고 1MB 를 넘을 수 없다.
CHUNK_SIZE = 512 * 1024

#: 파일명에 쓸 수 없는 문자
_INVALID_FS = re.compile(r'[<>:"/\\|?*]')
#: 줄바꿈·탭 등 제어문자 (밑줄 대신 공백으로 바꿔 읽기 좋게 만든다)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: 앨범(여러 장 묶음)의 형제 메시지를 찾을 때 좌우로 살펴볼 범위
ALBUM_SPAN = 10


class DownloadError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 오류."""


@dataclass
class MediaInfo:
    """원본 메시지에서 읽어둔 파일 정보.

    이 파일을 다시 텔레그램으로 보낼 때(휴대폰 전송) 원본 그대로 보이려면
    가로·세로·재생시간을 함께 알려줘야 한다. 알려주지 않으면 텔레그램이
    크기를 0으로 보고 임의의 비율로 맞춰 **좌우가 잘린 것처럼** 보인다.
    """

    width: int = 0
    height: int = 0
    duration: int = 0
    mime_type: str = ""
    is_video: bool = False
    round_message: bool = False

    @property
    def has_size(self) -> bool:
        return bool(self.width and self.height)


def media_info_from_message(msg) -> MediaInfo:
    """메시지에서 원본 영상 정보를 읽어온다."""
    info = MediaInfo()
    file = getattr(msg, "file", None)
    if file is not None:
        info.mime_type = getattr(file, "mime_type", "") or ""
        try:
            info.width = int(getattr(file, "width", 0) or 0)
            info.height = int(getattr(file, "height", 0) or 0)
            info.duration = int(float(getattr(file, "duration", 0) or 0))
        except (TypeError, ValueError):
            pass
    info.round_message = bool(getattr(msg, "video_note", None))
    info.is_video = bool(
        getattr(msg, "video", None)
        or getattr(msg, "gif", None)
        or info.round_message
        or info.mime_type.startswith("video/")
    )
    return info


def sanitize(name: str, max_len: int = 120) -> str:
    name = _CONTROL.sub(" ", name or "")
    name = _INVALID_FS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # 잘라낸 뒤에도 끝의 공백·점을 없앤다(윈도우에서 문제가 되는 이름).
    return name[:max_len].strip(" .") or "untitled"


def chat_folder_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return sanitize(title, 80)
    username = getattr(entity, "username", None)
    if username:
        return sanitize(username, 80)
    for attr in ("first_name", "last_name"):
        value = getattr(entity, attr, None)
        if value:
            return sanitize(value, 80)
    return f"chat_{getattr(entity, 'id', 'unknown')}"


class Stats:
    def __init__(self) -> None:
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.bytes = 0

    def summary(self) -> str:
        return (
            f"완료 {self.downloaded}개 · 건너뜀 {self.skipped}개 · "
            f"실패 {self.failed}개 · 받은 용량 {human_size(self.bytes)}"
        )


class Downloader:
    """링크를 받아 동영상을 내려받는다."""

    def __init__(
        self,
        client,
        reporter: Reporter,
        out_dir: str,
        media: str = "video",
        concurrency: int = 2,
        per_chat_folder: bool = True,
        include_album: bool = True,
        overwrite: bool = False,
        limit: int = 200,
        max_retries: int = 4,
        connections: int = 4,
    ) -> None:
        self.client = client
        self.reporter = reporter
        self.out_dir = Path(out_dir).expanduser()
        self.media = media if media in {"video", "all"} else "video"
        self.concurrency = max(1, int(concurrency))
        self.per_chat_folder = per_chat_folder
        self.include_album = include_album
        self.overwrite = overwrite
        self.limit = max(1, int(limit))
        self.max_retries = max(0, int(max_retries))
        #: 파일 하나를 받을 때 동시에 쓸 연결 수(1이면 기본 방식)
        self.connections = clamp_connections(connections)
        #: 여러 파일에서 재사용하는 연결 풀
        self._pool = SenderPool(client, self.connections)
        self.stats = Stats()

        #: 파일 저장이 끝난 뒤 호출되는 선택적 훅(안드로이드 갤러리 갱신 등)
        self.after_save: Optional[Callable[[Path], None]] = None

        self._sem = asyncio.Semaphore(self.concurrency)
        self._dialogs_loaded = False
        self._dialog_map: Dict[int, object] = {}
        self._dialog_lock = asyncio.Lock()
        self._active_paths: set = set()

    # ------------------------------------------------------------------ 채널
    async def _load_dialogs(self) -> None:
        """비공개 채널을 찾으려면 대화 목록을 한 번 읽어 캐시를 채워야 한다."""
        async with self._dialog_lock:
            if self._dialogs_loaded:
                return
            self.reporter.log("대화 목록을 불러오는 중… (비공개 채널 확인)")
            try:
                async for dialog in self.client.iter_dialogs():
                    entity = dialog.entity
                    raw_id = getattr(entity, "id", None)
                    if raw_id is not None:
                        self._dialog_map[int(raw_id)] = entity
            except errors.FloodWaitError as exc:
                raise DownloadError(
                    f"텔레그램이 잠시 요청을 제한했습니다. {exc.seconds}초 후 다시 시도하세요."
                ) from exc
            self._dialogs_loaded = True

    async def _resolve_private(self, channel_id: int):
        try:
            return await self.client.get_entity(PeerChannel(channel_id))
        except (
            ValueError,
            errors.ChannelInvalidError,
            errors.PeerIdInvalidError,
            errors.ChannelPrivateError,
        ) as exc:
            await self._load_dialogs()
            entity = self._dialog_map.get(int(channel_id))
            if entity is not None:
                return entity
            raise DownloadError(
                "비공개 채널을 찾을 수 없습니다. 로그인한 계정이 그 채널에 "
                "가입되어 있어야 하고, 대화 목록에 보여야 합니다."
            ) from exc

    async def _join_invite(self, invite_hash: str):
        try:
            updates = await self.client(ImportChatInviteRequest(invite_hash))
            chats = list(getattr(updates, "chats", None) or [])
            if chats:
                self.reporter.log(f"채널에 참여했습니다: {chat_folder_name(chats[0])}", "ok")
                return chats[0]
        except errors.UserAlreadyParticipantError:
            pass
        except errors.InviteHashExpiredError as exc:
            raise DownloadError("초대 링크가 만료되었습니다.") from exc
        except errors.InviteHashInvalidError as exc:
            raise DownloadError("잘못된 초대 링크입니다.") from exc
        except errors.InviteRequestSentError as exc:
            raise DownloadError(
                "가입 요청을 보냈습니다. 관리자 승인 후 다시 시도해 주세요."
            ) from exc
        except errors.FloodWaitError as exc:
            raise DownloadError(
                f"가입 요청이 제한되었습니다. {exc.seconds}초 후 다시 시도하세요."
            ) from exc

        invite = await self.client(CheckChatInviteRequest(invite_hash))
        chat = getattr(invite, "chat", None)
        if chat is not None:
            return chat
        raise DownloadError("초대 링크로 채널 정보를 가져올 수 없습니다.")

    async def _resolve(self, link: ParsedLink):
        if link.kind == "invite":
            return await self._join_invite(link.invite_hash or "")
        if link.kind == "public":
            try:
                return await self.client.get_entity(link.username)
            except errors.UsernameNotOccupiedError as exc:
                raise DownloadError(f"존재하지 않는 사용자명입니다: @{link.username}") from exc
            except errors.UsernameInvalidError as exc:
                raise DownloadError(f"잘못된 사용자명입니다: @{link.username}") from exc
            except ValueError as exc:
                raise DownloadError(
                    f"@{link.username} 을(를) 찾을 수 없습니다. 비공개 채널이면 "
                    "먼저 가입한 뒤 채널에서 '링크 복사'로 얻은 링크를 사용하세요."
                ) from exc
        return await self._resolve_private(int(link.channel_id or 0))

    # ---------------------------------------------------------------- 메시지
    async def _expand_albums(self, entity, messages: Sequence) -> List:
        """앨범(묶음) 메시지 하나를 집으면 같은 묶음 전체를 받는다."""
        result = list(messages)
        known = {msg.id for msg in result}
        grouped = {msg.grouped_id for msg in result if getattr(msg, "grouped_id", None)}
        if not grouped:
            return result

        for msg in list(messages):
            group_id = getattr(msg, "grouped_id", None)
            if group_id is None:
                continue
            candidate_ids = [
                mid
                for mid in range(msg.id - ALBUM_SPAN, msg.id + ALBUM_SPAN + 1)
                if mid > 0 and mid not in known
            ]
            if not candidate_ids:
                continue
            try:
                siblings = await self.client.get_messages(entity, ids=candidate_ids)
            except (errors.RPCError, ValueError):
                continue
            for sibling in siblings or []:
                if sibling and getattr(sibling, "grouped_id", None) == group_id:
                    known.add(sibling.id)
                    result.append(sibling)

        result.sort(key=lambda m: m.id)
        return result

    async def _collect_comment(self, link: ParsedLink, entity) -> Tuple[object, List]:
        """``?comment=`` 링크: 채널 글의 토론 그룹에서 해당 댓글을 가져온다."""
        post_id = link.message_ids[0] if link.message_ids else None
        if not post_id:
            raise DownloadError("댓글 링크에 원본 글 번호가 없습니다.")
        try:
            discussion = await self.client(
                GetDiscussionMessageRequest(peer=entity, msg_id=post_id)
            )
        except errors.RPCError as exc:
            raise DownloadError(f"토론 그룹을 찾을 수 없습니다: {exc.__class__.__name__}") from exc

        root = (getattr(discussion, "messages", None) or [None])[0]
        if root is None:
            raise DownloadError("토론 그룹 메시지를 찾을 수 없습니다.")
        group_entity = await self.client.get_entity(root.peer_id)
        messages = await self.client.get_messages(group_entity, ids=[link.comment_id])
        return group_entity, [msg for msg in (messages or []) if msg]

    async def _collect(self, link: ParsedLink, entity) -> Tuple[object, List]:
        if link.comment_id:
            return await self._collect_comment(link, entity)

        if link.message_ids:
            try:
                messages = await self.client.get_messages(entity, ids=link.message_ids)
            except errors.ChannelPrivateError as exc:
                raise DownloadError(
                    "이 채널에 접근할 수 없습니다. 로그인한 계정이 가입되어 있는지 확인하세요."
                ) from exc
            messages = [msg for msg in (messages or []) if msg]
            missing = len(link.message_ids) - len(messages)
            if missing > 0:
                self.reporter.log(
                    f"{missing}개 메시지는 삭제되었거나 볼 수 없어 건너뜁니다.", "warn"
                )
            if self.include_album:
                messages = await self._expand_albums(entity, messages)
            return entity, messages

        # 메시지 번호가 없는 링크 → 채널을 거슬러 올라가며 동영상을 찾는다.
        self.reporter.log(
            f"메시지 번호가 없어 {chat_folder_name(entity)} 에서 최신 동영상 "
            f"최대 {self.limit}개를 찾습니다."
        )
        media_filter = None if self.media == "all" else InputMessagesFilterVideo
        kwargs = {"limit": self.limit, "filter": media_filter}
        if link.topic_id:
            kwargs["reply_to"] = link.topic_id
        try:
            found = [msg async for msg in self.client.iter_messages(entity, **kwargs)]
        except TypeError:
            kwargs.pop("reply_to", None)
            found = [msg async for msg in self.client.iter_messages(entity, **kwargs)]
        found.sort(key=lambda m: m.id)
        return entity, found

    # ------------------------------------------------------------------ 파일
    def _wanted(self, msg) -> bool:
        if msg is None or not getattr(msg, "media", None):
            return False
        # 링크 미리보기는 받을 파일이 아니다.
        if type(msg.media).__name__ in {"MessageMediaWebPage", "MessageMediaEmpty"}:
            return False
        file = getattr(msg, "file", None)
        if file is None or not getattr(file, "size", None):
            return False
        if self.media == "all":
            return True
        if getattr(msg, "video", None) or getattr(msg, "video_note", None):
            return True
        if getattr(msg, "gif", None):
            return True
        mime = getattr(file, "mime_type", "") or ""
        return mime.startswith("video/")

    def _dest_path(self, msg, entity) -> Path:
        folder = self.out_dir
        if self.per_chat_folder:
            folder = folder / chat_folder_name(entity)
        folder.mkdir(parents=True, exist_ok=True)

        file = msg.file
        name = (getattr(file, "name", None) or "").strip()
        if not name:
            caption = (getattr(msg, "message", "") or "").strip()
            first_line = caption.splitlines()[0] if caption else ""
            stem = sanitize(first_line, 60) if first_line else f"video_{msg.id}"
            name = f"{stem}{getattr(file, 'ext', None) or '.mp4'}"
        name = sanitize(name)

        date = msg.date.strftime("%Y%m%d") if getattr(msg, "date", None) else "00000000"
        stem, ext = os.path.splitext(f"{date}_{msg.id}_{name}")
        return folder / f"{stem[:150]}{ext}"

    @staticmethod
    def _unique(path: Path) -> Path:
        if not path.exists():
            return path
        stem, ext = path.stem, path.suffix
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem} ({index}){ext}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{stem}_{int(time.time())}{ext}")

    @staticmethod
    def _resume_offset(size: int, total: int) -> int:
        """이어받기를 시작할 지점(조각 경계에 맞춘다)."""
        start = (max(0, int(size)) // CHUNK_SIZE) * CHUNK_SIZE
        if total and start >= total:
            start = max(0, ((total - 1) // CHUNK_SIZE) * CHUNK_SIZE)
        return start

    async def _stream(self, msg, part: Path, total: int, key: str, label: str) -> None:
        """``.part`` 파일에 이어받기 방식으로 저장한다."""
        start = self._resume_offset(part.stat().st_size if part.exists() else 0, total)
        if start:
            self.reporter.log(f"이어받기: {label} — {human_size(start)} 지점부터")

        mode = "r+b" if part.exists() else "wb"
        with open(part, mode) as handle:
            handle.truncate(start)
            self.reporter.progress(key, label, start, total)

            # 1) 빠른 방식: 여러 연결로 동시에 받는다.
            if self.connections > 1 and total:
                try:
                    await download_parallel(
                        self._pool,
                        msg.media,
                        handle,
                        total=total,
                        start=start,
                        on_progress=lambda done: self.reporter.progress(
                            key, label, done, total
                        ),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                    return
                except ParallelUnavailable as exc:
                    # 구조적으로 불가능한 경우에만 기본 방식으로 되돌린다.
                    self.reporter.log(
                        f"빠른 다운로드를 쓸 수 없어 기본 방식으로 받습니다 ({exc})", "warn"
                    )
                    self.connections = 1
                    handle.flush()
                    start = self._resume_offset(os.fstat(handle.fileno()).st_size, total)

            # 2) 기본 방식: 연결 하나로 순서대로 받는다.
            handle.seek(start)
            done = start
            async for chunk in self.client.iter_download(
                msg.media, offset=start, chunk_size=CHUNK_SIZE
            ):
                handle.write(chunk)
                done += len(chunk)
                self.reporter.progress(key, label, done, total)
            handle.flush()
            os.fsync(handle.fileno())

    async def _download(self, msg, entity) -> None:
        key = f"{getattr(entity, 'id', '?')}:{msg.id}"
        total = int(getattr(msg.file, "size", 0) or 0)
        dest = self._dest_path(msg, entity)
        label = dest.name

        if dest.exists() and not self.overwrite:
            if total and dest.stat().st_size == total:
                self.stats.skipped += 1
                self.reporter.finished(key, label, dest, skipped=True)
                return
            dest = self._unique(dest)
            label = dest.name
        elif dest.exists() and self.overwrite:
            dest.unlink()
        if self.overwrite:
            # 다시 받으라고 했으므로 예전 조각(.part)도 버린다.
            stale = dest.with_name(dest.name + ".part")
            if stale.exists():
                stale.unlink()

        if dest in self._active_paths:  # 같은 실행 안에서의 경합 방지
            dest = self._unique(dest)
            label = dest.name
        self._active_paths.add(dest)

        part = dest.with_name(dest.name + ".part")
        attempt = 0
        try:
            while True:
                try:
                    await self._stream(msg, part, total, key, label)
                    break
                except errors.FloodWaitError as exc:
                    wait = int(getattr(exc, "seconds", 0)) + 2
                    if wait > 3600:
                        raise DownloadError(
                            f"텔레그램 제한이 너무 깁니다({wait}초). 나중에 다시 시도하세요."
                        ) from exc
                    self.reporter.log(
                        f"속도 제한 — {wait}초 대기 후 계속: {label}", "warn"
                    )
                    await asyncio.sleep(wait)
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, errno.EROFS, errno.EACCES):
                        raise DownloadError(f"파일을 저장할 수 없습니다: {exc}") from exc
                    attempt += 1
                    if attempt > self.max_retries:
                        raise
                    backoff = min(2**attempt, 30)
                    self.reporter.log(
                        f"오류({exc.__class__.__name__}) — {backoff}초 후 재시도 "
                        f"{attempt}/{self.max_retries}: {label}",
                        "warn",
                    )
                    await asyncio.sleep(backoff)
                except (errors.RPCError, asyncio.TimeoutError, ConnectionError) as exc:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise
                    backoff = min(2**attempt, 30)
                    self.reporter.log(
                        f"오류({exc.__class__.__name__}) — {backoff}초 후 재시도 "
                        f"{attempt}/{self.max_retries}: {label}",
                        "warn",
                    )
                    await asyncio.sleep(backoff)

            os.replace(part, dest)
            if self.after_save is not None:
                try:
                    self.after_save(dest)
                except Exception as exc:  # noqa: BLE001 - 후처리 실패는 치명적이지 않다
                    self.reporter.log(f"저장 후 처리 실패(무시): {exc}", "warn")
            self.stats.downloaded += 1
            self.stats.bytes += total or dest.stat().st_size
            self.reporter.finished(
                key, label, dest, info=media_info_from_message(msg)
            )
        except asyncio.CancelledError:
            self.reporter.log(f"중단됨(이어받기 가능): {label}", "warn")
            raise
        except Exception as exc:  # noqa: BLE001 - 링크 하나가 실패해도 계속 진행
            self.stats.failed += 1
            message = str(exc) or exc.__class__.__name__
            self.reporter.failed(key, label, message)
        finally:
            self._active_paths.discard(dest)

    async def aclose(self) -> None:
        """열어둔 연결을 정리한다."""
        await self._pool.close()

    # ------------------------------------------------------------------ 진입
    async def process(self, link: ParsedLink) -> None:
        """링크 하나를 처리한다. 개별 실패는 로그만 남기고 넘어간다."""
        self.reporter.log(f"▶ {link.describe()}")
        try:
            entity = await self._resolve(link)
            entity, messages = await self._collect(link, entity)
        except DownloadError as exc:
            self.stats.failed += 1
            self.reporter.failed(link.raw, link.describe(), str(exc))
            return
        except errors.ChannelPrivateError:
            self.stats.failed += 1
            self.reporter.failed(
                link.raw,
                link.describe(),
                "비공개 채널에 접근할 수 없습니다(가입 여부를 확인하세요).",
            )
            return
        except errors.RPCError as exc:
            self.stats.failed += 1
            self.reporter.failed(link.raw, link.describe(), f"텔레그램 오류: {exc}")
            return

        targets = [msg for msg in messages if self._wanted(msg)]
        if not targets:
            kind = "동영상" if self.media == "video" else "첨부 파일"
            self.reporter.log(
                f"{kind}이 없습니다: {link.describe()}"
                + ("  (--all 옵션으로 사진·파일도 받을 수 있습니다)" if self.media == "video" else ""),
                "warn",
            )
            return

        total_bytes = sum(int(getattr(msg.file, "size", 0) or 0) for msg in targets)
        self.reporter.log(
            f"{len(targets)}개 파일 · 총 {human_size(total_bytes)} 다운로드 시작"
        )

        async def run(msg):
            async with self._sem:
                await self._download(msg, entity)

        await asyncio.gather(*(run(msg) for msg in targets))


class LinkPump:
    """링크를 큐에 넣고 워커가 순서대로 처리한다(붙여넣는 즉시 시작)."""

    def __init__(self, downloader: Downloader, workers: Optional[int] = None) -> None:
        self.downloader = downloader
        self.queue: asyncio.Queue = asyncio.Queue()
        self._seen: set = set()
        count = workers if workers is not None else downloader.concurrency
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(max(1, int(count)))
        ]

    async def _worker(self) -> None:
        while True:
            link = await self.queue.get()
            try:
                if link is None:
                    return
                await self.downloader.process(link)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.downloader.stats.failed += 1
                self.downloader.reporter.failed(
                    getattr(link, "raw", "?"), getattr(link, "raw", "?"), str(exc)
                )
            finally:
                self.queue.task_done()

    def submit(self, links: Sequence[ParsedLink], skip_seen: bool = True) -> int:
        """새 링크만 큐에 넣고, 넣은 개수를 돌려준다."""
        added = 0
        for link in links:
            if skip_seen and link.key in self._seen:
                continue
            self._seen.add(link.key)
            self.queue.put_nowait(link)
            added += 1
        return added

    async def join(self) -> None:
        await self.queue.join()

    async def close(self) -> None:
        for _ in self._workers:
            self.queue.put_nowait(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
