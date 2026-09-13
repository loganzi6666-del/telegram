"""텔레그램 링크 파서.

지원하는 형태
  https://t.me/channel/123                공개 채널/그룹의 메시지
  https://t.me/channel/123-130            메시지 범위 (구분자: - .. ~)
  https://t.me/channel/123,125,130        여러 메시지
  https://t.me/c/1234567890/123           비공개 채널의 메시지
  https://t.me/c/1234567890/45/123        비공개 포럼(토픽) 그룹의 메시지
  https://t.me/channel/123?comment=456    채널 글에 달린 댓글(토론 그룹) 메시지
  https://t.me/+AbCdEfGhIj                초대 링크 (가입 후 전체 동영상 탐색)
  https://t.me/joinchat/AbCdEfGhIj        구형 초대 링크
  https://t.me/channel                    메시지 번호 없음 → 채널 전체에서 동영상 탐색
  tg://resolve?domain=channel&post=123    텔레그램 내부 스킴
  tg://privatepost?channel=123456&post=7  텔레그램 내부 스킴(비공개)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

#: t.me 계열 호스트
T_ME_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}

#: 아무 텍스트에서 링크만 뽑아낼 때 쓰는 패턴
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"'()\[\]]+"
    r"|tg://[^\s<>\"'()\[\]]+",
    re.IGNORECASE,
)

_RANGE_RE = re.compile(r"^(\d+)\s*(?:-|\.\.|~)\s*(\d+)$")
# 실제 존재 여부는 텔레그램이 판단하므로 형식만 느슨하게 확인한다.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")

#: 링크 하나로 한 번에 지정할 수 있는 메시지 개수 상한 (오타로 수만 개를 거는 실수 방지)
MAX_RANGE = 3000


class LinkError(ValueError):
    """링크를 해석할 수 없을 때."""


@dataclass
class ParsedLink:
    """해석된 링크 하나."""

    raw: str
    kind: str  # "public" | "private" | "invite"
    username: Optional[str] = None
    channel_id: Optional[int] = None
    invite_hash: Optional[str] = None
    message_ids: List[int] = field(default_factory=list)
    topic_id: Optional[int] = None
    comment_id: Optional[int] = None

    @property
    def key(self) -> tuple:
        """중복 링크 판별용 키."""
        return (
            self.kind,
            self.username,
            self.channel_id,
            self.invite_hash,
            tuple(self.message_ids),
            self.topic_id,
            self.comment_id,
        )

    @property
    def chat_label(self) -> str:
        if self.username:
            return f"@{self.username}"
        if self.channel_id:
            return f"비공개채널 {self.channel_id}"
        if self.invite_hash:
            return f"초대링크 {self.invite_hash[:8]}…"
        return "?"

    def describe(self) -> str:
        if self.comment_id:
            return f"{self.chat_label} / 댓글 {self.comment_id}"
        ids = self.message_ids
        if not ids:
            return f"{self.chat_label} / 전체 탐색"
        if len(ids) == 1:
            return f"{self.chat_label} / 메시지 {ids[0]}"
        return f"{self.chat_label} / 메시지 {ids[0]}~{ids[-1]} ({len(ids)}개)"


def _parse_id_token(token: str) -> List[int]:
    """"123", "100-120", "1,2,3" 형태를 메시지 번호 목록으로."""
    ids: List[int] = []
    for part in re.split(r"[,+]", token):
        part = part.strip()
        if not part:
            continue
        matched = _RANGE_RE.match(part)
        if matched:
            start, end = int(matched.group(1)), int(matched.group(2))
            if start > end:
                start, end = end, start
            if end - start + 1 > MAX_RANGE:
                raise LinkError(
                    f"메시지 범위가 너무 넓습니다({end - start + 1}개). "
                    f"최대 {MAX_RANGE}개까지 지정할 수 있습니다."
                )
            ids.extend(range(start, end + 1))
        elif part.isdigit():
            ids.append(int(part))
        else:
            raise LinkError(f"메시지 번호를 이해할 수 없습니다: {part!r}")

    seen = set()
    unique: List[int] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _apply_tail(link: ParsedLink, tail: Sequence[str]) -> None:
    """경로의 남은 조각을 (토픽 번호, 메시지 번호)로 해석."""
    tail = [part for part in tail if part]
    if not tail:
        return
    if len(tail) >= 2 and tail[0].isdigit():
        # 포럼 토픽: /<topic>/<msg>
        link.topic_id = int(tail[0])
        link.message_ids = _parse_id_token(tail[1])
    else:
        link.message_ids = _parse_id_token(tail[0])


def _first(query: dict, name: str) -> Optional[str]:
    values = query.get(name) or []
    return values[0] if values else None


def _apply_query(link: ParsedLink, query: dict) -> None:
    thread = _first(query, "thread")
    if thread and thread.isdigit():
        link.topic_id = int(thread)
    comment = _first(query, "comment")
    if comment and comment.isdigit():
        link.comment_id = int(comment)
    post = _first(query, "post")
    if post and not link.message_ids:
        link.message_ids = _parse_id_token(post)


def _parse_tg_scheme(text: str) -> ParsedLink:
    parsed = urlparse(text)
    action = (parsed.netloc or parsed.path.lstrip("/")).split("?")[0].lower()
    query = parse_qs(parsed.query)

    if action == "resolve":
        domain = _first(query, "domain")
        if not domain:
            raise LinkError(f"도메인이 없는 tg:// 링크입니다: {text}")
        link = ParsedLink(raw=text, kind="public", username=domain)
    elif action in {"privatepost", "privatechannel"}:
        channel = _first(query, "channel")
        if not channel or not channel.isdigit():
            raise LinkError(f"채널 번호가 없는 tg:// 링크입니다: {text}")
        link = ParsedLink(raw=text, kind="private", channel_id=int(channel))
    elif action == "join":
        invite = _first(query, "invite")
        if not invite:
            raise LinkError(f"초대 해시가 없는 tg:// 링크입니다: {text}")
        return ParsedLink(raw=text, kind="invite", invite_hash=invite)
    else:
        raise LinkError(f"지원하지 않는 tg:// 링크입니다: {text}")

    _apply_query(link, query)
    return link


def parse_link(raw: str) -> ParsedLink:
    """문자열 하나를 :class:`ParsedLink` 로 변환한다."""
    text = (raw or "").strip().strip("<>").strip("\"'").rstrip(".,;")
    if not text:
        raise LinkError("빈 링크입니다.")

    if text.lower().startswith("tg://"):
        return _parse_tg_scheme(text)

    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text.lstrip("/")

    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host not in T_ME_HOSTS:
        raise LinkError(f"텔레그램 링크가 아닙니다: {raw.strip()}")

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower() == "s":
        # t.me/s/channel/123 (웹 미리보기 링크)
        parts = parts[1:]
    if not parts:
        raise LinkError(f"채널 정보가 없는 링크입니다: {raw.strip()}")

    query = parse_qs(parsed.query)
    head = parts[0]

    if head.lower() == "c":
        if len(parts) < 2 or not parts[1].isdigit():
            raise LinkError(f"비공개 채널 번호를 찾을 수 없습니다: {raw.strip()}")
        link = ParsedLink(raw=raw.strip(), kind="private", channel_id=int(parts[1]))
        _apply_tail(link, parts[2:])
    elif head.lower() == "joinchat":
        if len(parts) < 2 or not parts[1]:
            raise LinkError(f"초대 해시를 찾을 수 없습니다: {raw.strip()}")
        return ParsedLink(raw=raw.strip(), kind="invite", invite_hash=parts[1])
    elif head.startswith("+"):
        invite_hash = head[1:]
        if not invite_hash:
            raise LinkError(f"초대 해시를 찾을 수 없습니다: {raw.strip()}")
        return ParsedLink(raw=raw.strip(), kind="invite", invite_hash=invite_hash)
    else:
        if not _USERNAME_RE.match(head):
            raise LinkError(f"사용자명을 이해할 수 없습니다: {head!r}")
        link = ParsedLink(raw=raw.strip(), kind="public", username=head)
        _apply_tail(link, parts[1:])

    _apply_query(link, query)
    return link


def extract_link_strings(text: str) -> List[str]:
    """긴 텍스트에서 텔레그램 링크 문자열만 뽑아낸다."""
    found: List[str] = []
    for match in LINK_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(".,;'\")]}")
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def extract_links(text: str) -> List[ParsedLink]:
    """텍스트에서 해석 가능한 링크만 모아 중복을 제거해 돌려준다."""
    links: List[ParsedLink] = []
    keys = set()
    for candidate in extract_link_strings(text):
        try:
            link = parse_link(candidate)
        except LinkError:
            continue
        if link.key in keys:
            continue
        keys.add(link.key)
        links.append(link)
    return links
