"""Timestamp utilities for consistent file/log naming."""

from datetime import datetime, timezone


def file_timestamp() -> str:
    """Filesystem-safe timestamp with microsecond precision."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def iso_timestamp() -> str:
    """ISO-8601 timestamp for API/log payloads."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
