"""生成 ShareBox 图标（PNG + ICO）。构建 exe 前运行一次即可。"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

TOP = (92, 124, 250)     # #5c7cfa
BOTTOM = (59, 91, 219)   # #3b5bdb


def gradient(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)
    return image


def build(size: int = 256) -> Image.Image:
    base = gradient(size).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.paste(base, (0, 0), mask)

    font_path = None
    for candidate in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                      "C:/Windows/Fonts/simhei.ttf"):
        if Path(candidate).exists():
            font_path = candidate
            break
    draw = ImageDraw.Draw(image)
    if font_path:
        font = ImageFont.truetype(font_path, int(size * 0.56))
        draw.text((size / 2, size / 2 + size * 0.02), "共", font=font, fill=(255, 255, 255, 255), anchor="mm")
    else:
        draw.rectangle([size * 0.3, size * 0.3, size * 0.7, size * 0.7], fill="white")
    return image


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    large = build(256)
    large.save(OUT / "sharebox.png")
    large.save(OUT / "sharebox.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"已生成：{OUT / 'sharebox.ico'}")
    print(f"已生成：{OUT / 'sharebox.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
