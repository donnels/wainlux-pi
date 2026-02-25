"""Shared validation utilities for Flask and MCP servers"""

from typing import Optional, Any


def safe_int(value: Any, field: str, min_value: Optional[int] = None, max_value: Optional[int] = None, default: Optional[int] = None) -> int:
    """Parse and validate integer with optional bounds.
    
    Args:
        value: Value to parse
        field: Field name for error messages
        min_value: Minimum allowed value (clamps if exceeded)
        max_value: Maximum allowed value (clamps if exceeded)
        default: Default value if None (raises if not provided)
        
    Returns:
        Validated integer
        
    Raises:
        ValueError: If value cannot be parsed and no default provided
    """
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    
    if min_value is not None and parsed < min_value:
        parsed = min_value
    if max_value is not None and parsed > max_value:
        parsed = max_value
    return parsed


def safe_float(value: Any, field: str, min_value: Optional[float] = None, max_value: Optional[float] = None, default: Optional[float] = None) -> float:
    """Parse and validate float with optional bounds.
    
    Args:
        value: Value to parse
        field: Field name for error messages
        min_value: Minimum allowed value (clamps if exceeded)
        max_value: Maximum allowed value (clamps if exceeded)
        default: Default value if None (raises if not provided)
        
    Returns:
        Validated float
        
    Raises:
        ValueError: If value cannot be parsed and no default provided
    """
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    
    if min_value is not None and parsed < min_value:
        parsed = min_value
    if max_value is not None and parsed > max_value:
        parsed = max_value
    return parsed


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse bool-like values from JSON/form payloads.
    
    Args:
        value: Value to parse (bool, int, str, etc.)
        default: Default if None
        
    Returns:
        Boolean value
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
