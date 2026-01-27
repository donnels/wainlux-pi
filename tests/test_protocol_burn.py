"""Tests for burn_payload with chunking and retry logic."""

import pytest
from scripts.k6.transport import MockTransport
from scripts.k6 import protocol


def test_burn_payload_single_line():
    """Test burning a single line under DATA_CHUNK size."""
    transport = MockTransport()
    # Each DATA packet expects ACK
    transport.queue_response(bytes([protocol.ACK]))

    payload_lines = [b"X" * 100]  # Single line, 100 bytes

    chunks = protocol.burn_payload(transport, payload_lines)

    assert chunks == 1
    assert len(transport.writes) == 1
    # Verify DATA packet structure
    pkt = transport.writes[0]
    assert pkt[0] == 0x22  # DATA opcode


def test_burn_payload_chunking():
    """Test that large lines are split into DATA_CHUNK pieces."""
    transport = MockTransport()
    # Need 2 ACKs for 2 chunks
    transport.queue_response(bytes([protocol.ACK]))
    transport.queue_response(bytes([protocol.ACK]))

    # Create line that needs 2 chunks (DATA_CHUNK = 1900)
    payload_lines = [b"X" * 3000]

    chunks = protocol.burn_payload(transport, payload_lines)

    assert chunks == 2
    assert len(transport.writes) == 2


def test_burn_payload_multiple_lines():
    """Test burning multiple lines."""
    transport = MockTransport()
    # 3 lines, each gets 1 ACK
    for _ in range(3):
        transport.queue_response(bytes([protocol.ACK]))

    payload_lines = [b"A" * 100, b"B" * 100, b"C" * 100]

    chunks = protocol.burn_payload(transport, payload_lines)

    assert chunks == 3
    assert len(transport.writes) == 3


def test_burn_payload_retry_success():
    """Test retry logic succeeds after initial timeout."""
    transport = MockTransport()

    payload_lines = [b"X" * 100]

    # Mock timeout on first read, success on retry
    original_read = transport.read
    read_count = [0]

    def mock_read(n):
        read_count[0] += 1
        # First 20 reads timeout, then return ACK
        if read_count[0] <= 20:
            return b""
        # After timeout, queue an ACK for retry
        if read_count[0] == 21:
            transport.queue_response(bytes([protocol.ACK]))
        return original_read(n)

    transport.read = mock_read

    chunks = protocol.burn_payload(transport, payload_lines, max_retries=3)

    # Should succeed after retry
    assert chunks == 1


def test_burn_payload_max_retries_exceeded():
    """Test that max retries raises K6DeviceError."""
    transport = MockTransport()
    # Never provide ACK - all attempts timeout

    payload_lines = [b"X" * 100]

    with pytest.raises(protocol.K6DeviceError) as exc_info:
        protocol.burn_payload(transport, payload_lines, max_retries=2)

    assert "failed after 2 attempts" in str(exc_info.value)
    assert "line 0" in str(exc_info.value)


def test_burn_payload_empty():
    """Test burning empty payload (edge case)."""
    transport = MockTransport()

    payload_lines = []

    chunks = protocol.burn_payload(transport, payload_lines)

    assert chunks == 0
    assert len(transport.writes) == 0


def test_burn_payload_exponential_backoff():
    """Test that retry delays increase (timing test)."""
    import time

    transport = MockTransport()

    payload_lines = [b"X" * 100]

    # Force 2 timeouts then success
    original_read = transport.read
    read_count = [0]
    retry_start_times = []

    def mock_read(n):
        read_count[0] += 1
        # First timeout: reads 1-20
        # Second timeout: reads 21-40
        # Success: read 41+
        if read_count[0] == 1:
            retry_start_times.append(time.time())
        elif read_count[0] == 21:
            retry_start_times.append(time.time())
        elif read_count[0] == 41:
            retry_start_times.append(time.time())
            transport.queue_response(bytes([protocol.ACK]))

        if read_count[0] <= 40:
            return b""
        return original_read(n)

    transport.read = mock_read

    chunks = protocol.burn_payload(transport, payload_lines, max_retries=3)

    assert chunks == 1
    # Verify we had retries with backoff
    assert len(retry_start_times) == 3
    # Second retry should be ~0.1s after first
    # Third retry should be ~0.2s after second
    delay1 = retry_start_times[1] - retry_start_times[0]
    delay2 = retry_start_times[2] - retry_start_times[1]
    assert delay1 >= 0.08  # 0.1s backoff with some tolerance
    assert delay2 >= 0.18  # 0.2s backoff with some tolerance
