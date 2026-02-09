"""Temporary file management utilities"""

from pathlib import Path
from contextlib import contextmanager
from .timestamps import file_timestamp


@contextmanager
def temp_image_file(data_dir: Path, prefix: str = "temp", suffix: str = ".png"):
    """Context manager for temporary image files with automatic cleanup.
    
    Args:
        data_dir: Directory to create temp file in
        prefix: Filename prefix
        suffix: File extension (default .png)
        
    Yields:
        Path object for temp file
        
    Example:
        with temp_image_file(DATA_DIR, "upload") as path:
            img.save(path)
            process_image(path)
        # File automatically deleted on exit
    """
    timestamp = file_timestamp()
    temp_path = data_dir / f"{prefix}_{timestamp}{suffix}"
    
    try:
        yield temp_path
    finally:
        temp_path.unlink(missing_ok=True)


@contextmanager
def multiple_temp_files(data_dir: Path, count: int, prefix: str = "temp", suffix: str = ".png"):
    """Context manager for multiple temporary files with automatic cleanup.
    
    Args:
        data_dir: Directory to create temp files in
        count: Number of temp files to create
        prefix: Filename prefix
        suffix: File extension
        
    Yields:
        List of Path objects
        
    Example:
        with multiple_temp_files(DATA_DIR, 3, "job") as [path1, path2, path3]:
            # Use temp files
            pass
        # All files automatically deleted
    """
    timestamp = file_timestamp()
    temp_paths = [
        data_dir / f"{prefix}_{timestamp}_{i}{suffix}"
        for i in range(count)
    ]
    
    try:
        yield temp_paths
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)
