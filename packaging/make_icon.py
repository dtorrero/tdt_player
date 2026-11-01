#!/usr/bin/env python3
"""Draw ``tdt_player/tdtplayer.png``, the application icon.

A dark rounded tile with a light screen and a red play mark: it reads as
"television" and as "play" at 16 px in a menu, and needs neither fonts nor
artwork. The same PNG is used for the KDE menu entry (install.sh copies it into
the hicolor theme) and for the window icon (gui.apply_window_icon loads it from
next to the module, since Tk reads PNG natively).

Run it from anywhere::

    python3 packaging/make_icon.py                 # writes tdt_player/tdtplayer.png
    python3 packaging/make_icon.py --size 256 --out /tmp/icon.png

Pillow is required to draw the icon (``pip install pillow``); the application
itself does not need it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_SIZE = 512
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "tdt_player" / "tdtplayer.png"

#: The window background of the app's own dark variants, and the two accents.
TILE = (33, 43, 54)
SCREEN = (238, 242, 247)
ACCENT = (229, 57, 53)

RADIUS_RATIO = 0.22  # rounded square, like the other desktop icons


def draw(size: int):
    """Return the icon as an RGBA image of ``size`` x ``size`` pixels."""
    from PIL import Image, ImageDraw

    scale = 4  # draw big, shrink with LANCZOS: smooth corners at every size
    pixels = size * scale
    image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
    pencil = ImageDraw.Draw(image)

    pencil.rounded_rectangle(
        (0, 0, pixels - 1, pixels - 1),
        radius=int(pixels * RADIUS_RATIO),
        fill=TILE,
    )

    left = int(pixels * 0.15)
    right = pixels - left
    top = int(pixels * 0.24)
    bottom = int(pixels * 0.72)
    pencil.rounded_rectangle(
        (left, top, right, bottom),
        radius=int(pixels * 0.06),
        fill=SCREEN,
    )

    # The play mark, slightly right of centre so it looks centred inside the
    # screen's outline (optical centring, like a play sign on a TV bezel).
    centre_x = left + (right - left) * 0.54
    centre_y = (top + bottom) / 2
    half_width = (right - left) * 0.15
    half_height = (bottom - top) * 0.26
    pencil.polygon(
        [
            (centre_x - half_width, centre_y - half_height),
            (centre_x - half_width, centre_y + half_height),
            (centre_x + half_width, centre_y),
        ],
        fill=ACCENT,
    )

    # A small stand under the screen: two strokes plus a foot, which is what
    # makes the shape read as a television rather than a tablet.
    stand_top = bottom
    stand_bottom = int(pixels * 0.80)
    pencil.rectangle(
        (int(pixels * 0.46), stand_top, int(pixels * 0.54), stand_bottom),
        fill=SCREEN,
    )
    pencil.rounded_rectangle(
        (
            int(pixels * 0.34),
            stand_bottom,
            int(pixels * 0.66),
            int(pixels * 0.84),
        ),
        radius=int(pixels * 0.02),
        fill=SCREEN,
    )

    return image.resize((size, size), Image.LANCZOS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draw the tdt_player icon.")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, help="square size in pixels")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="where to write the PNG")
    args = parser.parse_args(argv)

    if args.size < 16:
        parser.error("--size must be at least 16")

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is required to draw the icon: pip install pillow", file=sys.stderr)
        return 1

    image = draw(args.size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out, optimize=True)
    print(f"wrote {args.out} ({args.size}x{args.size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
