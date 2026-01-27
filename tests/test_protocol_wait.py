from scripts.k6 import protocol
from scripts.k6.transport import MockTransport
import pytest


def test_wait_for_completion_success():
    t = MockTransport()
    # simulate progress 0% then 50% then 100%
    t.queue_response(b"\xff\xff\x00\x00\xff\xff\x00\x32\xff\xff\x00\x64")

    r = protocol.wait_for_completion(t, max_wait_s=2.0, idle_s=1.0)
    assert r["status"] == "complete"
    assert r["total_time"] >= 0


def test_wait_for_completion_idle():
    t = MockTransport()
    # no responses queued -> should idle out
    r = protocol.wait_for_completion(t, max_wait_s=5.0, idle_s=0.01)
    assert r["status"] == "idle_timeout"


def test_wait_for_completion_max_timeout():
    t = MockTransport()
    # ensure max_wait smaller than idle -> raises
    with pytest.raises(protocol.K6TimeoutError):
        protocol.wait_for_completion(t, max_wait_s=0.01, idle_s=5.0)


def test_send_cmd_checked_no_response():
    t = MockTransport()
    with pytest.raises(protocol.K6TimeoutError):
        protocol.send_cmd_checked(
            t, "TEST", b"\x0a\x00\x04\x00", timeout=0.01, expect_ack=True
        )


def test_send_cmd_checked_heartbeat_ok():
    t = MockTransport()
    t.queue_response(protocol.HEARTBEAT)
    rx = protocol.send_cmd_checked(
        t, "TEST", b"\x23\x00\x26" + b"\x00" * 35, timeout=0.5, expect_ack=False
    )
    assert protocol.HEARTBEAT in rx
