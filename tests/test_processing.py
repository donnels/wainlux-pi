import pytest
from scripts.k6 import processing


def test_mm_px_roundtrip():
    mm = 75
    px = processing.mm_to_px(mm)
    assert isinstance(px, int)
    assert processing.px_to_mm(px) == pytest.approx(mm, rel=1e-3)


def test_zero():
    assert processing.mm_to_px(0) == 0
    assert processing.px_to_mm(0) == 0.0
