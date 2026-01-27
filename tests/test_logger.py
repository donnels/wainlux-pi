import json
from pathlib import Path
from scripts.k6 import logger
from PIL import Image


def test_save_burn_config(tmp_path):
    # create a small test image
    img_path = tmp_path / "test.png"
    Image.new("RGB", (10, 10), "white").save(img_path)

    cfg_file, img_file = logger.save_burn_config(
        str(tmp_path), str(img_path), power=500, depth=5
    )

    cfg_path = Path(cfg_file)
    img_copy = Path(img_file)

    assert cfg_path.exists()
    assert img_copy.exists()

    with open(cfg_path, "r") as f:
        cfg = json.load(f)

    assert cfg["power"] == 500
    assert cfg["depth"] == 5
    assert "image" in cfg
    assert Path(cfg["image"]).exists() or img_copy.exists()
