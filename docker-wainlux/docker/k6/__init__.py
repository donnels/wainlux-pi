"""K6 laser protocol library - clean, testable implementation.

Pure-Python protocol implementation for Wainlux K6 laser engravers.
Uses transport abstraction. No subprocess calls. No legacy dependencies.
"""

from .driver import WainluxK6
from .transport import SerialTransport, MockTransport
from .csv_logger import CSVLogger

__all__ = ["WainluxK6", "SerialTransport", "MockTransport", "CSVLogger"]
__version__ = "0.2.0"
