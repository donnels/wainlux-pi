from scripts.k6 import protocol
from scripts.k6.transport import MockTransport


def test_checksum_known():
    # simple known case: empty payload packet (length 4) -> checksum should be computed
    pkt = bytearray(4)
    pkt[0] = 0x22
    pkt[1] = 0x00
    pkt[2] = 0x04
    pkt[3] = 0x00
    chk = protocol.checksum(pkt)
    assert isinstance(chk, int)


def test_build_data_packet_and_checksum():
    payload = b"\x00\x01\x02"
    pkt = protocol.build_data_packet(payload)
    assert pkt[0] == 0x22
    # length is payload + 4
    assert pkt[1] == 0x00
    assert pkt[2] == len(payload) + 4
    # verify checksum matches function
    assert pkt[-1] == protocol.checksum(pkt)


def test_send_cmd_ack():
    t = MockTransport()
    t.queue_response(bytes([protocol.ACK]))

    rx = protocol.send_cmd(t, "TEST", b"\x0a\x00\x04\x00", timeout=0.5, expect_ack=True)
    assert protocol.ACK.to_bytes(1, "big") in rx


def test_send_cmd_heartbeat():
    t = MockTransport()
    t.queue_response(protocol.HEARTBEAT)

    rx = protocol.send_cmd(
        t, "TEST", b"\x23\x00\x26" + b"\x00" * 35, timeout=0.5, expect_ack=False
    )
    assert protocol.HEARTBEAT in rx


def test_parse_response_frames():
    hb, ack, typ = protocol.parse_response_frames(
        protocol.HEARTBEAT + bytes([protocol.ACK])
    )
    assert hb == 1
    assert ack == 1
    assert typ == "HEARTBEAT+ACK"
