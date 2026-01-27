"""Minimal k6 package (MVP)

This package is intentionally small. It provides a thin, tested-able
wrapper around the existing `k6_burn_image.py` script while establishing
clean module boundaries for a future library rework.
"""

from .driver import WainluxK6

__all__ = ["WainluxK6"]
__version__ = "0.1.0"
