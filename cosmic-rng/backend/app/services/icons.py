"""Server-side PNG rendering of item icons (used for Discord notifications).

The web client renders rich animated SVG icons from the same visual
descriptor; this is a faithful static approximation for places that cannot
render SVG (Discord embeds).
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256


def _hex(c: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = (c or "#ffffff").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha
    except ValueError:
        return 255, 255, 255, alpha


def _star(cx: float, cy: float, r1: float, r2: float, n: int = 5, rot: float = -math.pi / 2) -> list[tuple[float, float]]:
    pts = []
    for i in range(n * 2):
        r = r1 if i % 2 == 0 else r2
        a = rot + i * math.pi / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _poly(cx: float, cy: float, r: float, n: int, rot: float = -math.pi / 2, sx: float = 1.0, sy: float = 1.0) -> list[tuple[float, float]]:
    return [(cx + r * sx * math.cos(rot + i * 2 * math.pi / n), cy + r * sy * math.sin(rot + i * 2 * math.pi / n)) for i in range(n)]


def _shape(draw: ImageDraw.ImageDraw, shape: str, c1: tuple[int, ...], c2: tuple[int, ...], c3: tuple[int, ...]) -> None:
    cx = cy = SIZE / 2
    r = SIZE * 0.3
    if shape in ("star",):
        draw.polygon(_star(cx, cy, r * 1.15, r * 0.48), fill=c1, outline=c3)
    elif shape in ("crystal", "gem"):
        draw.polygon(_poly(cx, cy, r * 1.1, 6, sx=0.62), fill=c1, outline=c3)
        draw.polygon(_poly(cx, cy, r * 0.6, 6, sx=0.62), fill=c2)
    elif shape in ("shard",):
        draw.polygon([(cx - r * 0.5, cy + r), (cx, cy - r * 1.15), (cx + r * 0.62, cy + r * 0.55)], fill=c1, outline=c3)
    elif shape in ("diamond", "prism"):
        n = 4 if shape == "diamond" else 3
        draw.polygon(_poly(cx, cy, r * 1.15, n), fill=c1, outline=c3)
        draw.polygon(_poly(cx, cy + (r * 0.12 if n == 3 else 0), r * 0.55, n), fill=c2)
    elif shape in ("cube", "core"):
        draw.polygon(_poly(cx, cy, r * 1.05, 6, rot=-math.pi / 6), fill=c2, outline=c3)
        draw.polygon([(cx, cy), (cx - r * 0.9, cy - r * 0.52), (cx, cy - r * 1.05), (cx + r * 0.9, cy - r * 0.52)], fill=c1)
    elif shape in ("crown",):
        pts = [(cx - r, cy + r * 0.6), (cx - r, cy - r * 0.5), (cx - r * 0.5, cy), (cx, cy - r * 0.9),
               (cx + r * 0.5, cy), (cx + r, cy - r * 0.5), (cx + r, cy + r * 0.6)]
        draw.polygon(pts, fill=c1, outline=c3)
        draw.rectangle((cx - r, cy + r * 0.45, cx + r, cy + r * 0.7), fill=c2)
    elif shape in ("ring", "compass", "anchor"):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=c1, width=int(r * 0.32))
        draw.ellipse((cx - r * 0.25, cy - r * 0.25, cx + r * 0.25, cy + r * 0.25), fill=c3)
    elif shape in ("hourglass",):
        draw.polygon([(cx - r * 0.7, cy - r), (cx + r * 0.7, cy - r), (cx, cy)], fill=c1)
        draw.polygon([(cx - r * 0.7, cy + r), (cx + r * 0.7, cy + r), (cx, cy)], fill=c2)
    elif shape in ("bolt",):
        pts = [(cx + r * 0.2, cy - r * 1.1), (cx - r * 0.55, cy + r * 0.1), (cx - r * 0.05, cy + r * 0.1),
               (cx - r * 0.25, cy + r * 1.1), (cx + r * 0.6, cy - r * 0.2), (cx + r * 0.08, cy - r * 0.2)]
        draw.polygon(pts, fill=c1, outline=c3)
    elif shape in ("tear", "flame"):
        pts = [(cx, cy - r * 1.15)] + [(cx + r * 0.75 * math.cos(a), cy + r * 0.3 + r * 0.75 * math.sin(a))
                                       for a in [i * math.pi / 12 for i in range(-1, 14)]]
        draw.polygon(pts, fill=c1, outline=c3)
    elif shape in ("gate", "bell"):
        draw.rectangle((cx - r * 0.85, cy - r * 0.2, cx + r * 0.85, cy + r), fill=c1)
        draw.ellipse((cx - r * 0.85, cy - r, cx + r * 0.85, cy + r * 0.6), fill=c1)
        draw.ellipse((cx - r * 0.45, cy - r * 0.55, cx + r * 0.45, cy + r * 0.9), fill=c2)
    elif shape in ("rune", "sigil"):
        draw.polygon(_poly(cx, cy, r * 1.1, 3), outline=c1, width=6)
        draw.polygon(_poly(cx, cy, r * 1.1, 3, rot=math.pi / 2), outline=c2, width=6)
        draw.ellipse((cx - r * 0.3, cy - r * 0.3, cx + r * 0.3, cy + r * 0.3), fill=c3)
    elif shape in ("blackhole",):
        draw.ellipse((cx - r * 1.2, cy - r * 0.35, cx + r * 1.2, cy + r * 0.35), outline=c2, width=8)
        draw.ellipse((cx - r * 0.6, cy - r * 0.6, cx + r * 0.6, cy + r * 0.6), fill=(0, 0, 0, 255), outline=c3, width=3)
    elif shape in ("planet", "moon"):
        draw.ellipse((cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8), fill=c1)
        if shape == "planet":
            draw.ellipse((cx - r * 1.3, cy - r * 0.3, cx + r * 1.3, cy + r * 0.3), outline=c2, width=6)
        else:
            draw.ellipse((cx - r * 0.3, cy - r * 0.95, cx + r * 1.0, cy + r * 0.5), fill=(0, 0, 0, 0))
    else:  # orb and every remaining organic shape
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c1, outline=c3)
        draw.ellipse((cx - r * 0.55, cy - r * 0.7, cx + r * 0.05, cy - r * 0.1), fill=c3[:3] + (140,))


@lru_cache(maxsize=512)
def _render_cached(shape: str, c1: str, c2: str, c3: str, glow: str, ring: str, ring2: str) -> bytes:
    img = Image.new("RGBA", (SIZE, SIZE), (6, 8, 22, 255))
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(bg)
    for i in range(10, 0, -1):
        rr = SIZE * 0.05 * i
        d.ellipse((SIZE / 2 - rr, SIZE / 2 - rr, SIZE / 2 + rr, SIZE / 2 + rr), fill=_hex(glow, int(10 + (10 - i) * 6)))
    bg = bg.filter(ImageFilter.GaussianBlur(12))
    img = Image.alpha_composite(img, bg)
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _shape(ImageDraw.Draw(layer), shape, _hex(c1), _hex(c2), _hex(c3))
    glow_layer = layer.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(img, glow_layer)
    img = Image.alpha_composite(img, layer)
    d2 = ImageDraw.Draw(img)
    d2.ellipse((8, 8, SIZE - 8, SIZE - 8), outline=_hex(ring, 220), width=5)
    d2.ellipse((16, 16, SIZE - 16, SIZE - 16), outline=_hex(ring2, 120), width=2)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_icon(visual: dict[str, Any], rarity_color: str = "#ffffff", rarity_color2: str = "#888888") -> bytes:
    colors = list(visual.get("colors") or ["#c0c8e0", "#7080a8", "#ffffff"])
    while len(colors) < 3:
        colors.append(colors[-1])
    return _render_cached(str(visual.get("shape", "orb")), colors[0], colors[1], colors[2], str(visual.get("glow") or colors[0]),
                          rarity_color, rarity_color2)
