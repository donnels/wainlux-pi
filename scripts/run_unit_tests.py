#!/usr/bin/env python3
"""Simple test runner that does not require pytest.

This is a minimal harness to validate core helpers in environments
where pytest isn't available (like on a Pi without dev deps).
"""

import sys
import os
from pathlib import Path

# Ensure repo root is on sys.path so 'scripts' package can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts.k6 import processing, logger
from PIL import Image


def test_processing():
    mm = 75
    px = processing.mm_to_px(mm)
    if not isinstance(px, int):
        raise AssertionError("mm_to_px did not return int")
    if abs(processing.px_to_mm(px) - mm) > 0.1:
        raise AssertionError("px_to_mm roundtrip off")

    if processing.mm_to_px(0) != 0:
        raise AssertionError("mm_to_px(0) != 0")
    if processing.px_to_mm(0) != 0.0:
        raise AssertionError("px_to_mm(0) != 0.0")


def run_logger_test(tmpdir):
    td = Path(tmpdir)
    img_path = td / "t.png"
    Image.new("RGB", (10, 10), "white").save(img_path)
    cfg, img_copy = logger.save_burn_config(str(td), str(img_path), power=123, depth=4)
    cfg_path = Path(cfg)
    img_path_copy = Path(img_copy)
    if not cfg_path.exists():
        raise AssertionError("Config file missing")
    if not img_path_copy.exists():
        raise AssertionError("Image copy missing")


def main():
    # processing tests
    try:
        test_processing()
        print("processing: ok")
    except AssertionError as e:
        print("processing: FAILED -", e)
        sys.exit(2)

    # logger tests
    import tempfile

    td = tempfile.mkdtemp(prefix="k6test_")
    try:
        run_logger_test(td)
        print("logger: ok")
    except AssertionError as e:
        print("logger: FAILED -", e)
        sys.exit(2)

    print("ALL TESTS OK")


if __name__ == "__main__":
    main()
