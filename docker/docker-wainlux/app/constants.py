"""Shared K6 hardware constants used across API, services, and UI payloads."""


class K6Constants:
    """Single source of truth for K6 geometry and default positioning."""

    # Hardware workspace limits (pixels)
    MAX_WIDTH_PX = 1600
    MAX_HEIGHT_PX = 1520

    # Geometry conversion
    RESOLUTION_MM_PER_PX = 0.05
    WORK_WIDTH_MM = MAX_WIDTH_PX * RESOLUTION_MM_PER_PX
    WORK_HEIGHT_MM = MAX_HEIGHT_PX * RESOLUTION_MM_PER_PX

    # Positioning offsets/defaults
    CENTER_X_OFFSET_PX = 67
    CENTER_Y_PX = MAX_HEIGHT_PX // 2
    DEFAULT_CENTER_X_PX = 800
    DEFAULT_CENTER_Y_PX = 800

    # Backward-compatible aliases used in existing modules
    BURN_WIDTH_PX = MAX_WIDTH_PX
    BURN_HEIGHT_PX = MAX_HEIGHT_PX
    BURN_WIDTH_MM = WORK_WIDTH_MM
    BURN_HEIGHT_MM = WORK_HEIGHT_MM
    RESOLUTION_MM_PX = RESOLUTION_MM_PER_PX
    CENTER_OFFSET_X = CENTER_X_OFFSET_PX
    DEFAULT_CENTER_X = DEFAULT_CENTER_X_PX
    DEFAULT_CENTER_Y = DEFAULT_CENTER_Y_PX
