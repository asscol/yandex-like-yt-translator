#!/usr/bin/env python3
"""Generate the extension's icon set.

Renders a circular orange/red gradient badge with a microphone glyph and the
Russian "Я" letter overlaid. Output sizes match Chrome MV3's default icons
manifest entries: 16, 32, 48, 128 px.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "icons"
SIZES = (16, 32, 48, 128)

GRADIENT_TOP = (255, 87, 34)
GRADIENT_BOTTOM = (211, 47, 47)
TEXT_COLOR = (255, 255, 255)


def find_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def render_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle with vertical gradient.
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(GRADIENT_TOP[0] * (1 - t) + GRADIENT_BOTTOM[0] * t)
        g = int(GRADIENT_TOP[1] * (1 - t) + GRADIENT_BOTTOM[1] * t)
        b = int(GRADIENT_TOP[2] * (1 - t) + GRADIENT_BOTTOM[2] * t)
        grad.putpixel((0, y), (r, g, b))
    grad = grad.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    img.paste(grad, (0, 0), mask)

    # Letter "Я" centered.
    font_size = int(size * 0.62)
    font = find_font(font_size)
    text = "Я"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1] - max(1, size // 32)
    draw.text((x, y), text, font=font, fill=TEXT_COLOR)

    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s in SIZES:
        img = render_icon(s)
        out = OUT_DIR / f"icon{s}.png"
        img.save(out, "PNG")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
