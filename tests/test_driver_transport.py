import pytest
from scripts.k6.driver import WainluxK6
from scripts.k6.transport import MockTransport
from scripts.k6 import protocol


def test_connect_transport_success():
    t = MockTransport()
    # Queue VERSION reply (3 bytes), then ACKs for CONNECTs and HOME
    t.queue_response(b"\x04\x01\x06")
    t.queue_response(bytes([protocol.ACK]))
    t.queue_response(bytes([protocol.ACK]))
    t.queue_response(bytes([protocol.ACK]))

    drv = WainluxK6()
    assert drv.connect_transport(t) is True


def test_connect_transport_home_timeout():
    t = MockTransport()
    # VERSION ok, CONNECTs OK, HOME no response
    t.queue_response(b"\x04\x01\x06")
    t.queue_response(bytes([protocol.ACK]))
    t.queue_response(bytes([protocol.ACK]))
    # no HOME response queued

    drv = WainluxK6()
    with pytest.raises(protocol.K6TimeoutError):
        drv.connect_transport(t)


def test_connect_transport_connect_no_ack():
    t = MockTransport()
    # VERSION ok, CONNECT #1 returns nothing (simulate device issue)
    t.queue_response(b"\x04\x01\x06")
    # No ACK for CONNECT #1 - empty response triggers timeout

    drv = WainluxK6()
    # Empty response triggers timeout before ACK check
    with pytest.raises(protocol.K6TimeoutError):
        drv.connect_transport(t)
