"""깨우기(Wake-on-LAN)와 전원 명령 테스트. `python tests/test_wake.py`"""

from __future__ import annotations

import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgdl import power  # noqa: E402
from tgdl.power import Action, PowerError, plan  # noqa: E402
from tgdl.wake import (  # noqa: E402
    DEFAULT_PORTS,
    WakeError,
    broadcast_targets,
    is_awake,
    magic_packet,
    normalize_mac,
    pretty_mac,
    send_magic,
    wait_awake,
)


# ------------------------------------------------------------------ 주소 다루기
def test_normalize_mac_accepts_common_shapes():
    for text in (
        "A1:B2:C3:D4:E5:F6",
        "a1-b2-c3-d4-e5-f6",
        "a1b2c3d4e5f6",
        "A1B2.C3D4.E5F6",
        " a1:b2:c3:d4:e5:f6 ",
    ):
        assert normalize_mac(text) == "a1b2c3d4e5f6", text


def test_normalize_mac_rejects_nonsense():
    for text in ("", "a1:b2:c3", "zzzzzzzzzzzz", "a1b2c3d4e5f6f7"):
        try:
            normalize_mac(text)
        except WakeError as exc:
            assert "ipconfig" in str(exc), "확인 방법을 알려 줘야 한다"
        else:
            raise AssertionError(f"잘못된 주소를 받아들였다: {text!r}")


def test_pretty_mac():
    assert pretty_mac("a1b2c3d4e5f6") == "A1:B2:C3:D4:E5:F6"


# ------------------------------------------------------------------ 매직 패킷
def test_magic_packet_shape():
    packet = magic_packet("a1:b2:c3:d4:e5:f6")
    assert len(packet) == 102, "6바이트 머리 + 6바이트 주소 × 16"
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("a1b2c3d4e5f6") * 16


def test_send_magic_repeats_to_every_target():
    sent: list = []
    count = send_magic(
        "a1b2c3d4e5f6",
        broadcast="192.168.0.255",
        repeat=2,
        sender=lambda packet, address, port: sent.append((address, port)),
    )
    assert count == len(sent) == 2 * len(DEFAULT_PORTS)
    assert {address for address, _port in sent} == {"192.168.0.255"}
    assert {port for _address, port in sent} == set(DEFAULT_PORTS)


def test_broadcast_targets_include_global_broadcast():
    targets = broadcast_targets()
    assert "255.255.255.255" in targets
    assert len(targets) == len(set(targets)), "같은 주소를 두 번 보내지 않는다"


def test_broadcast_targets_respect_setting():
    assert broadcast_targets("10.0.0.255") == ["10.0.0.255"]


def test_send_magic_over_real_socket():
    """실제로 UDP 소켓에 쓸 수 있는지(브로드캐스트 권한 포함) 확인한다."""
    assert send_magic("a1b2c3d4e5f6", broadcast="127.0.0.1", ports=(9,), repeat=1) == 1


# ------------------------------------------------------------------ 켜졌는지 확인
def test_is_awake_with_open_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    try:
        assert is_awake("127.0.0.1", port)
    finally:
        server.close()


def test_is_awake_with_closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert not is_awake("127.0.0.1", port, timeout=1.0)


def test_is_awake_without_host():
    assert not is_awake("")


def test_wait_awake_stops_as_soon_as_it_answers():
    tries = {"count": 0}

    def probe():
        tries["count"] += 1
        return tries["count"] >= 3

    clock = {"now": 0.0}
    assert wait_awake(
        "192.168.0.10",
        timeout=60,
        interval=1,
        check=probe,
        sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        clock=lambda: clock["now"],
    )
    assert tries["count"] == 3


def test_wait_awake_gives_up_after_timeout():
    clock = {"now": 0.0}
    assert not wait_awake(
        "192.168.0.10",
        timeout=10,
        interval=3,
        check=lambda: False,
        sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        clock=lambda: clock["now"],
    )


def test_wait_awake_without_host_does_not_block():
    assert wait_awake("", check=lambda: False)


# ------------------------------------------------------------------ 전원 명령
def test_plan_knows_every_action_on_this_platform():
    for key in ("sleep", "shutdown", "reboot", "lock"):
        action = plan(key)
        assert action.argv, key
        assert action.label, key
    assert plan("lock").danger is False, "화면 잠금은 확인이 필요 없다"
    assert plan("shutdown").danger is True, "종료는 확인을 받아야 한다"


def test_plan_rejects_unknown_action():
    try:
        plan("자폭")
    except (PowerError, KeyError):
        pass
    else:
        raise AssertionError("모르는 전원 동작을 받아들였다")


def test_windows_commands_are_correct():
    """윈도우에서 어떤 명령이 나가는지 표를 직접 확인한다."""
    table = power._TABLE["windows"]
    assert table["shutdown"][1] == ["shutdown", "/s", "/t", "0"]
    assert table["reboot"][1] == ["shutdown", "/r", "/t", "0"]
    assert "SetSuspendState" in " ".join(table["sleep"][1])
    assert "LockWorkStation" in " ".join(table["lock"][1])


def test_run_uses_injected_runner():
    calls: list = []
    power.run(Action("sleep", "절전", ["아무거나"]), runner=calls.append)
    assert calls == [["아무거나"]]


def test_run_reports_failure_clearly():
    action = Action("shutdown", "종료", ["이런-명령은-없다-12345"])
    try:
        power.run(action)
    except PowerError as exc:
        assert "종료" in str(exc)
    else:
        raise AssertionError("없는 명령이 성공했다고 보고했다")


def test_uptime_is_sane():
    value = power.uptime_seconds()
    assert value >= 0.0


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
