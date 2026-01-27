#!/usr/bin/env python3
"""Thin CLI to use the MVP `WainluxK6` driver.

This script is intentionally minimal — it should remain a tiny wrapper
that calls library functions and returns meaningful exit codes.
"""

import argparse
from .driver import WainluxK6


def main():
    parser = argparse.ArgumentParser(description="MVP k6 CLI")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("bounds", help="draw boundary box")
    p.add_argument("--mm", type=int, default=75)
    p.add_argument("--depth", type=int, default=5)

    p = sub.add_parser("engrave", help="engrave an image")
    p.add_argument("image")
    p.add_argument("--power", type=int, default=1000)
    p.add_argument("--depth", type=int, default=100)

    args = parser.parse_args()
    driver = WainluxK6()

    if args.cmd == "bounds":
        ok = driver.draw_bounds(boundary_mm=args.mm, depth=args.depth)
        return 0 if ok else 2

    if args.cmd == "engrave":
        out = driver.engrave(args.image, power=args.power, depth=args.depth)
        print(out["stdout"])
        if out["ok"]:
            return 0
        else:
            print(out["stderr"])
            return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
