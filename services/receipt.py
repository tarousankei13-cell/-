"""
レシート画像の生成

添付画像3（マクドナルドの受け取り画面）をテンプレートに、
注文番号だけを実際の値へ差し替える。

計測値は docs/04 §6.1 を参照。
Playwright で描画する案もあったが、メモリを数百MB使うため
MWS（1GB）では現実的でない。Pillow なら約50msで済む。
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config

log = logging.getLogger("bot.receipt")

BASE_DIR = Path(__file__).parent.parent
TEMPLATE_PATH = BASE_DIR / config.RECEIPT_TEMPLATE
FONT_PATH = BASE_DIR / config.RECEIPT_FONT

_template: Image.Image | None = None


class ReceiptError(Exception):
    pass


def _load_template() -> Image.Image:
    """テンプレートは使い回す（毎回ディスクから読まない）。"""
    global _template
    if _template is None:
        if not TEMPLATE_PATH.exists():
            raise ReceiptError(f"テンプレート画像がありません: {TEMPLATE_PATH}")
        _template = Image.open(TEMPLATE_PATH).convert("RGB")
    return _template.copy()


def _fit_font(draw: ImageDraw.ImageDraw, text: str) -> ImageFont.FreeTypeFont:
    """桁数が変わっても収まるよう、はみ出す場合だけ縮める。"""
    size = config.RECEIPT_BASE_FONT_SIZE
    while size > 20:
        font = ImageFont.truetype(str(FONT_PATH), size=size)
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        if right - left <= config.RECEIPT_MAX_WIDTH:
            return font
        size -= 2
    return ImageFont.truetype(str(FONT_PATH), size=20)


def _render(receipt_number: str) -> bytes:
    img = _load_template()
    draw = ImageDraw.Draw(img)
    # 元の番号を消す（アンチエイリアスの縁が残らないよう広めに塗る）
    draw.rectangle(config.RECEIPT_CLEAR_BOX, fill=config.RECEIPT_BG_COLOR)
    text = receipt_number or "----"
    font = _fit_font(draw, text)
    draw.text(
        config.RECEIPT_CENTER, text,
        font=font, fill=config.RECEIPT_INK_COLOR, anchor="mm",
    )
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def render(receipt_number: str) -> io.BytesIO:
    """
    注文番号を差し替えたレシート画像を作る。

    Pillow は同期処理なので、別スレッドで動かしてBOTを止めない。
    """
    data = await asyncio.to_thread(_render, receipt_number)
    return io.BytesIO(data)


def receipt_view_url(store_id: str, receipt_number: str) -> str:
    """
    受け取り画面のURL。

    DMのボタンから開けるようにしておくと、利用者が本物の画面を
    自分で表示できる（docs/07 §3）。
    """
    return config.RECEIPT_VIEW_URL.format(
        store_id=store_id, receipt_number=receipt_number
    )


def self_check() -> str:
    """起動時に一度だけ、テンプレートとフォントが使えるか確かめる。"""
    if not TEMPLATE_PATH.exists():
        return f"テンプレート画像がありません: {TEMPLATE_PATH}"
    if not FONT_PATH.exists():
        return f"フォントがありません: {FONT_PATH}"
    try:
        _render("0000")
    except Exception as e:
        return f"レシート画像を生成できません: {e}"
    return ""
