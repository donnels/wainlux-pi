#!/usr/bin/env python3
"""Probe K6 DATA (0x22) behavior via /api/lab/raw/send.

Runs isolated protocol sequences against a connected device and reports
ACK/error behavior for different DATA payload patterns.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


def checksum_bytes(data_without_checksum: bytes) -> int:
    """Two's complement checksum (8-bit)."""
    return (-sum(data_without_checksum)) & 0xFF


def put_u16_be(buf: bytearray, idx: int, value: int) -> None:
    buf[idx] = (value >> 8) & 0xFF
    buf[idx + 1] = value & 0xFF


def put_u32_be(buf: bytearray, idx: int, value: int) -> None:
    buf[idx] = (value >> 24) & 0xFF
    buf[idx + 1] = (value >> 16) & 0xFF
    buf[idx + 2] = (value >> 8) & 0xFF
    buf[idx + 3] = value & 0xFF


def build_job_header_raster(
    width: int,
    height: int,
    payload_size: int,
    power: int = 300,
    depth: int = 8,
    center_x: int | None = None,
    center_y: int | None = None,
) -> bytes:
    """Build 38-byte JOB_HEADER (0x23) packet."""
    if center_x is None:
        center_x = (width // 2) + 67
    if center_y is None:
        center_y = 760

    hdr = bytearray(38)
    hdr[0] = 0x23
    hdr[1] = 0x00
    hdr[2] = 38

    packet_count = (payload_size // 4094) + 1
    put_u16_be(hdr, 3, packet_count)
    hdr[5] = 0x01

    put_u16_be(hdr, 6, width)
    put_u16_be(hdr, 8, height)
    put_u16_be(hdr, 10, 33)
    put_u16_be(hdr, 12, power)
    put_u16_be(hdr, 14, depth)
    put_u16_be(hdr, 16, 0)  # vector_w
    put_u16_be(hdr, 18, 0)  # vector_h
    put_u32_be(hdr, 20, payload_size)
    put_u16_be(hdr, 24, 0)  # vector_power
    put_u16_be(hdr, 26, 0)  # vector_depth
    put_u32_be(hdr, 28, 0)  # point_count
    put_u16_be(hdr, 32, center_x)
    put_u16_be(hdr, 34, center_y)
    hdr[36] = 1
    hdr[37] = 0x00
    return bytes(hdr)


def build_data_packet(payload: bytes) -> bytes:
    """Build DATA (0x22) packet."""
    packet_len = len(payload) + 4
    pkt = bytearray(packet_len)
    pkt[0] = 0x22
    pkt[1] = (packet_len >> 8) & 0xFF
    pkt[2] = packet_len & 0xFF
    pkt[3 : 3 + len(payload)] = payload
    pkt[-1] = checksum_bytes(bytes(pkt[:-1]))
    return bytes(pkt)


@dataclass
class PatternCase:
    name: str
    payload: bytes


class RawClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def send_packet(self, packet: bytes, timeout_s: float = 2.0) -> dict:
        body = {
            "packet_hex": packet.hex(" "),
            "timeout": timeout_s,
            "read_size": 64,
            "flush_input": True,
            "connect_if_needed": True,
            "save_logs": False,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/lab/raw/send",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))


def make_patterns() -> list[PatternCase]:
    patterns: list[PatternCase] = []

    patterns.append(PatternCase("blank_8", bytes([0x00] * 8)))
    patterns.append(PatternCase("single_pixel_start_8", bytes([0x80] + [0x00] * 7)))
    patterns.append(PatternCase("single_pixel_end_8", bytes([0x01] + [0x00] * 7)))
    patterns.append(PatternCase("alternating_aa55_8", bytes([0xAA, 0x55] * 4)))
    patterns.append(PatternCase("full_black_8", bytes([0xFF] * 8)))

    patterns.append(PatternCase("blank_72", bytes([0x00] * 72)))
    patterns.append(PatternCase("single_pixel_start_72", bytes([0x80] + [0x00] * 71)))
    patterns.append(PatternCase("full_black_72", bytes([0xFF] * 72)))

    edge_line = bytearray([0x00] * 200)
    edge_line[0] = 0x80
    edge_line[-1] = 0x01
    patterns.append(PatternCase("edge_pixels_200", bytes(edge_line)))
    patterns.append(PatternCase("blank_200", bytes([0x00] * 200)))
    patterns.append(PatternCase("full_black_200", bytes([0xFF] * 200)))

    patterns.append(PatternCase("oversize_1901", bytes([0xFF] * 1901)))
    return patterns


def setup_sequence(client: RawClient, payload_len: int, power: int, depth: int) -> None:
    """Run STOP, FRAMING, JOB_HEADER, CONNECT x2 for one isolated test case."""
    width = max(8, payload_len * 8)
    height = 1
    header = build_job_header_raster(
        width=width,
        height=height,
        payload_size=payload_len,
        power=power,
        depth=depth,
    )

    setup_packets = [
        ("STOP", bytes([0x16, 0x00, 0x04, 0x00]), 1.5),
        ("FRAMING", bytes([0x21, 0x00, 0x04, 0x00]), 1.5),
        ("JOB_HEADER", header, 3.0),
        ("CONNECT #1", bytes([0x0A, 0x00, 0x04, 0x00]), 1.5),
        ("CONNECT #2", bytes([0x0A, 0x00, 0x04, 0x00]), 1.5),
    ]

    for name, pkt, timeout_s in setup_packets:
        resp = client.send_packet(pkt, timeout_s=timeout_s)
        # Best effort setup check; keep it strict for deterministic runs.
        if name in {"STOP", "FRAMING", "CONNECT #1", "CONNECT #2"} and resp.get("ack_count", 0) == 0:
            raise RuntimeError(f"{name} setup step missing ACK: {resp}")


def print_result(prefix: str, payload_len: int, resp: dict) -> None:
    ack = resp.get("ack_count", 0)
    rx = resp.get("rx_hex", "")
    rtype = resp.get("response_type", "")
    success = resp.get("success", False)
    print(
        f"{prefix:24} len={payload_len:4d} ack={ack} "
        f"rx={rx or '-':<12} type={rtype:<10} success={success}"
    )


def run_cases(base_url: str, power: int, depth: int, pause_s: float) -> int:
    client = RawClient(base_url)
    patterns = make_patterns()

    print(f"Base URL: {base_url}")
    print(f"Power/Depth: {power}/{depth}")
    print()
    print("=== Isolated Single-Packet Cases ===")
    failures = 0

    for case in patterns:
        try:
            setup_sequence(client, len(case.payload), power, depth)
            resp = client.send_packet(build_data_packet(case.payload), timeout_s=2.0)
            print_result(case.name, len(case.payload), resp)
            if not resp.get("success", False):
                failures += 1
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            failures += 1
            print(f"{case.name:24} ERROR {exc}")
        time.sleep(pause_s)

    print()
    print("=== Sequential Case (valid -> blank -> valid) ===")
    try:
        payload = bytes([0xFF] + [0x00] * 7)
        blank = bytes([0x00] * 8)
        setup_sequence(client, len(payload), power, depth)
        r1 = client.send_packet(build_data_packet(payload), timeout_s=2.0)
        r2 = client.send_packet(build_data_packet(blank), timeout_s=2.0)
        r3 = client.send_packet(build_data_packet(payload), timeout_s=2.0)
        print_result("seq_valid_1", len(payload), r1)
        print_result("seq_blank", len(blank), r2)
        print_result("seq_valid_2", len(payload), r3)
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        failures += 1
        print(f"sequential_case            ERROR {exc}")

    print()
    print(f"Done. Non-success cases: {failures}")
    return 0 if failures == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe K6 DATA packet behavior via /api/lab/raw/send"
    )
    parser.add_argument(
        "--base-url",
        default="http://piz-k6:8080",
        help="Flask API base URL (default: http://piz-k6:8080)",
    )
    parser.add_argument("--power", type=int, default=300, help="Raster power (default: 300)")
    parser.add_argument("--depth", type=int, default=8, help="Raster depth (default: 8)")
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=150,
        help="Pause between isolated cases in milliseconds (default: 150)",
    )
    args = parser.parse_args()

    return run_cases(
        base_url=args.base_url,
        power=max(0, min(1000, args.power)),
        depth=max(1, min(255, args.depth)),
        pause_s=max(0.0, args.pause_ms / 1000.0),
    )


if __name__ == "__main__":
    sys.exit(main())
