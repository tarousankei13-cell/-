"""注文完了画像の生成.

テンプレート画像の中央にある注文番号を、実際の注文番号に差し替える。
Pillow(PIL) が無い場合やテンプレートが無い場合は無効化される。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bot.image")

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:  # pragma: no cover
    _PIL_OK = False

ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATE_PATH = ASSETS_DIR / "order_complete_template.png"

# ── テンプレート実測値（370x665 の注文完了画面） ──
_NUM_CENTER_X = 255       # 注文番号の中心X
_NUM_CENTER_Y = 134       # 注文番号の中心Y
_NUM_TARGET_H = 48        # 注文番号の高さ(px)
_NUM_COLOR = (45, 45, 45)  # 文字色 #2D2D2D
_BG_COLOR = (247, 247, 247)  # 背景色 #F7F7F7
_COVER_BOX = (185, 105, 328, 164)  # 元の番号を塗りつぶす矩形

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def is_available() -> bool:
    return _PIL_OK and TEMPLATE_PATH.exists()


def _find_font_path() -> Optional[str]:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _fitted_font(text: str, target_h: int):
    path = _find_font_path()
    if not path:
        return ImageFont.load_default()
    base = 100
    font = ImageFont.truetype(path, base)
    bbox = font.getbbox(text)
    height = bbox[3] - bbox[1]
    if height <= 0:
        height = base
    size = max(8, int(base * target_h / height))
    return ImageFont.truetype(path, size)


def render_order_complete_sync(number: str) -> bytes:
    """注文番号を差し込んだ完了画像を PNG バイト列で返す。"""
    number = (str(number).strip() or "----")[:8]
    im = Image.open(TEMPLATE_PATH).convert("RGB")
    draw = ImageDraw.Draw(im)

    # 元の番号を背景色で消す
    draw.rectangle(_COVER_BOX, fill=_BG_COLOR)

    font = _fitted_font(number, _NUM_TARGET_H)
    bbox = draw.textbbox((0, 0), number, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = _NUM_CENTER_X - tw // 2 - bbox[0]
    y = _NUM_CENTER_Y - th // 2 - bbox[1]
    draw.text((x, y), number, font=font, fill=_NUM_COLOR)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


async def render_order_complete(number: str) -> bytes:
    return await asyncio.to_thread(render_order_complete_sync, number)
