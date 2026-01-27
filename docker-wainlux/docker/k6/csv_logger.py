"""CSV logging utilities for protocol operations.

Provides CSVLogger class for tracking protocol operations with timing,
throughput, and retry metrics matching the legacy script format.
"""

from __future__ import annotations
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


class CSVLogger:
    """Logs protocol operations to CSV file with timing and throughput metrics.

    CSV Format:
        burn_start, timestamp, elapsed_s, phase, operation, duration_ms,
        bytes_transferred, cumulative_bytes, throughput_kbps, status_pct,
        state, response_type, retry_count, device_state

    Usage:
        logger = CSVLogger("path/to/log.csv")
        logger.log_operation(
            phase="connect",
            operation="HOME",
            duration_ms=145.2,
            bytes_transferred=4,
            response_type="ACK"
        )
        logger.close()
    """

    def __init__(self, csv_path: str):
        """Initialize CSV logger.

        Args:
            csv_path: Path to CSV file (will be created/overwritten)
        """
        self.csv_path = Path(csv_path)
        self.csv_file = open(self.csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)

        # Write header
        self.csv_writer.writerow(
            [
                "burn_start",
                "timestamp",
                "elapsed_s",
                "phase",
                "operation",
                "duration_ms",
                "bytes_transferred",
                "cumulative_bytes",
                "throughput_kbps",
                "status_pct",
                "state",
                "response_type",
                "retry_count",
                "device_state",
            ]
        )

        self.start_time = time.time()
        self.burn_start_str = datetime.fromtimestamp(self.start_time).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.cumulative_bytes = 0

    def log_operation(
        self,
        phase: str,
        operation: str,
        duration_ms: float,
        bytes_transferred: int = 0,
        status_pct: Optional[int] = None,
        state: str = "ACTIVE",
        response_type: str = "",
        retry_count: int = 0,
        device_state: str = "IDLE",
    ):
        """Log a protocol operation.

        Args:
            phase: Operation phase (connect, burn, wait, etc.)
            operation: Specific operation name (HOME, DATA, CONNECT, etc.)
            duration_ms: Operation duration in milliseconds
            bytes_transferred: Bytes sent/received in this operation
            status_pct: Burn progress percentage (0-100)
            state: Operation state (ACTIVE, COMPLETE, ERROR, etc.)
            response_type: Response type (ACK, HEARTBEAT, STATUS, etc.)
            retry_count: Number of retries for this operation
            device_state: Device state (IDLE, BUSY, BURNING, etc.)
        """
        self.cumulative_bytes += bytes_transferred
        throughput_kbps = (
            (bytes_transferred / 1024) / (duration_ms / 1000) if duration_ms > 0 else 0
        )
        elapsed_s = time.time() - self.start_time
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        self.csv_writer.writerow(
            [
                self.burn_start_str,
                now_str,
                f"{elapsed_s:.3f}",
                phase,
                operation,
                f"{duration_ms:.0f}",
                bytes_transferred,
                self.cumulative_bytes,
                f"{throughput_kbps:.2f}",
                status_pct if status_pct is not None else "",
                state,
                response_type,
                retry_count,
                device_state,
            ]
        )

        # Flush to ensure data is written
        self.csv_file.flush()

    def close(self):
        """Close CSV file."""
        if self.csv_file and not self.csv_file.closed:
            self.csv_file.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
