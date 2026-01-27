"""Small image math helpers for K6 (MVP)

Keep pure functions here for easy testing and reuse.
"""

MM_PER_PX = 0.05  # K6 native resolution (mm per pixel)
PX_PER_MM = 1.0 / MM_PER_PX


def mm_to_px(mm: float) -> int:
    """Convert millimetres to pixels (rounded int)."""
    return int(round(mm * PX_PER_MM))


def px_to_mm(px: int) -> float:
    """Convert pixels to millimetres."""
    return px * MM_PER_PX
