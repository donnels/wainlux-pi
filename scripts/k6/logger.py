"""Simple logger utilities (MVP).

This module provides tiny helper functions to save a JSON config
and copy the image. It intentionally avoids complex CSV logic; the
goal is a minimal, testable surface to extend later.
"""

import json
from datetime import datetime
from pathlib import Path
from PIL import Image


def save_burn_config(output_dir: str, img_path: str, power: int, depth: int):
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    cfg_name = p / f"stat-{ts}.json"
    img_name = p / f"stat-{ts}.png"

    config = {
        "timestamp": datetime.now().isoformat(),
        "image": str(img_name),
        "power": power,
        "depth": depth,
    }

    with open(cfg_name, "w") as f:
        json.dump(config, f, indent=2)

    # copy image
    try:
        im = Image.open(img_path)
        im.save(img_name)
    except Exception:
        # fail silently for MVP; tests should cover failures
        pass

    return str(cfg_name), str(img_name)
