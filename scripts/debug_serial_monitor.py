#!/usr/bin/env python3
"""
Simple serial port monitor for K6 device.
Displays all incoming bytes in real-time.
"""

import serial
import sys
import time
from datetime import datetime


def timestamp():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

    print(f"Opening {port} at 115200 baud...")
    print("Press Ctrl+C to exit\n")

    try:
        ser = serial.Serial(port, 115200, timeout=1.0)

        buf = bytearray()
        last_rx = time.time()
        byte_count = 0

        while True:
            b = ser.read(1)
            if b:
                now = time.time()
                idle_ms = (now - last_rx) * 1000
                last_rx = now
                byte_count += 1

                buf.extend(b)
                if len(buf) > 16:
                    buf = buf[-16:]

                # Display byte in hex
                print(f"[{timestamp()}] +{idle_ms:6.0f}ms: {b[0]:02x}  ", end="")

                # Check for status frame: FF FF 00 XX
                if (
                    len(buf) >= 4
                    and buf[-4:][0] == 0xFF
                    and buf[-4:][1] == 0xFF
                    and buf[-4:][2] == 0x00
                ):
                    pct = buf[-4:][3]
                    print(f"← STATUS {pct}%")
                # Check for heartbeat: FF FF FF FE
                elif len(buf) >= 4 and buf[-4:] == b"\xff\xff\xff\xfe":
                    print("← HEARTBEAT")
                # Check for ACK
                elif b[0] == 0x09:
                    print("← ACK")
                else:
                    print(f"  (buffer: {buf[-8:].hex()})")

                sys.stdout.flush()
            else:
                # Timeout - show idle time
                idle_s = time.time() - last_rx
                if idle_s > 5:
                    print(
                        f"[{timestamp()}] Idle for {idle_s:.1f}s (total bytes: {byte_count})"
                    )
                    sys.stdout.flush()
                    last_rx = time.time()  # Reset to avoid spam
                time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        if "ser" in locals():
            ser.close()


if __name__ == "__main__":
    main()
