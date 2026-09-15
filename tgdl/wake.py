"""컴퓨터를 원격으로 **켜기** — Wake-on-LAN(매직 패킷).

꺼진(또는 절전 중인) 컴퓨터는 프로그램을 돌릴 수 없으므로 텔레그램 메시지를
직접 받을 수 없다. 대신 랜카드가 전원을 조금 먹으며 깨어 있어서, 같은 공유기
안에서 특별한 UDP 묶음(매직 패킷)을 받으면 본체를 깨운다.

그래서 집에 **항상 켜져 있는 작은 기기**(라즈베리파이 · 안 쓰는 옛 안드로이드폰
+ Termux · NAS)가 하나 필요하다. 그 기기에서 `python -m tgdl waker` 를 돌려
두면, 밖에서 텔레그램으로 `/깨우기` 만 보내면 집 컴퓨터가 켜진다.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from typing import Callable, List, Optional, Sequence

#: 매직 패킷을 받는 흔한 포트. 메인보드마다 다르므로 둘 다 보낸다.
DEFAULT_PORTS = (9, 7)

#: 같은 패킷을 몇 번 보낼지(하나가 유실돼도 깨어나도록)
DEFAULT_REPEAT = 3

_HEX = re.compile(r"^[0-9a-f]{12}$")


class WakeError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


def normalize_mac(text: str) -> str:
    """``AA:BB:CC:DD:EE:FF`` 같은 여러 표기를 12자리 소문자로 통일한다."""
    raw = (text or "").strip().lower()
    cleaned = re.sub(r"[^0-9a-f]", "", raw)
    if not _HEX.match(cleaned):
        raise WakeError(
            f"랜카드 주소(MAC)를 이해할 수 없습니다: {text!r}\n"
            "윈도우 명령 프롬프트에서 `ipconfig /all` 을 실행해 유선 랜(이더넷)의 "
            "'물리적 주소'를 확인하세요. 예: A1-B2-C3-D4-E5-F6"
        )
    return cleaned


def pretty_mac(mac: str) -> str:
    value = normalize_mac(mac)
    return ":".join(value[index : index + 2] for index in range(0, 12, 2)).upper()


def magic_packet(mac: str) -> bytes:
    """0xFF 여섯 개 뒤에 랜카드 주소를 열여섯 번 반복한 102바이트."""
    payload = bytes.fromhex(normalize_mac(mac))
    return b"\xff" * 6 + payload * 16


def broadcast_targets(broadcast: str = "") -> List[str]:
    """매직 패킷을 보낼 주소 목록.

    공유기에 따라 ``255.255.255.255`` 를 그냥 버리는 경우가 있어서, 내 주소에서
    계산한 같은 망의 브로드캐스트 주소(예: ``192.168.0.255``)도 함께 쓴다.
    """
    targets: List[str] = []
    if broadcast:
        targets.append(broadcast.strip())
    else:
        targets.append("255.255.255.255")
        local = _local_ip()
        if local and local.count(".") == 3 and not local.startswith("127."):
            targets.append(local.rsplit(".", 1)[0] + ".255")
    seen = set()
    return [item for item in targets if item and not (item in seen or seen.add(item))]


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # 실제로 보내지는 않는다
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def send_magic(
    mac: str,
    broadcast: str = "",
    ports: Sequence[int] = DEFAULT_PORTS,
    repeat: int = DEFAULT_REPEAT,
    sender: Optional[Callable[[bytes, str, int], None]] = None,
) -> int:
    """매직 패킷을 보낸다. 보낸 횟수를 돌려준다.

    ``sender`` 를 넘기면 실제 통신 없이 시험할 수 있다.
    """
    packet = magic_packet(mac)
    targets = broadcast_targets(broadcast)
    ports = [int(port) for port in (ports or DEFAULT_PORTS) if int(port) > 0]
    repeat = max(1, min(10, int(repeat or 1)))

    if sender is not None:
        count = 0
        for _ in range(repeat):
            for address in targets:
                for port in ports:
                    sender(packet, address, port)
                    count += 1
        return count

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sent = 0
    errors: List[str] = []
    try:
        for attempt in range(repeat):
            for address in targets:
                for port in ports:
                    try:
                        sock.sendto(packet, (address, port))
                        sent += 1
                    except OSError as exc:
                        errors.append(f"{address}:{port} — {exc}")
            if attempt + 1 < repeat:
                time.sleep(0.2)
    finally:
        sock.close()
    if not sent:
        raise WakeError("매직 패킷을 보내지 못했습니다: " + " / ".join(errors[:3]))
    return sent


# ------------------------------------------------------------------ 깨어났는지
def is_awake(host: str, port: int = 0, timeout: float = 2.0) -> bool:
    """그 컴퓨터가 지금 응답하는지.

    포트를 지정하면 그 포트에 붙어 보고(가장 확실하다), 없으면 ping 을 쓴다.
    윈도우는 기본 방화벽이 ping 을 막는 경우가 있어 포트 지정을 권한다.
    """
    host = (host or "").strip()
    if not host:
        return False
    if port:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False
    return _ping(host, timeout)


def _ping(host: str, timeout: float) -> bool:
    import os

    if os.name == "nt":  # pragma: no cover - 윈도우에서만
        argv = ["ping", "-n", "1", "-w", str(int(max(1.0, timeout) * 1000)), host]
    else:
        argv = ["ping", "-c", "1", "-W", str(int(max(1.0, timeout))), host]
    try:
        done = subprocess.run(
            argv,
            timeout=max(3.0, timeout + 2),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def wait_awake(
    host: str,
    port: int = 0,
    timeout: float = 90.0,
    interval: float = 3.0,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    check: Optional[Callable[[], bool]] = None,
) -> bool:
    """켜질 때까지 기다린다. 확인할 주소가 없으면 기다리지 않고 True."""
    if not host:
        return True
    probe = check or (lambda: is_awake(host, port))
    deadline = clock() + max(0.0, timeout)
    while True:
        if probe():
            return True
        if clock() >= deadline:
            return False
        sleeper(max(0.5, interval))
