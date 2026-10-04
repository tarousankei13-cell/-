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


# リンク先が使えるかどうか。使えないと分かったらボタンを出さない。
_link_alive: bool = True
_link_checked_at: float = 0.0


def receipt_view_url(store_id: str, receipt_number: str) -> str:
    """
    受け取り画面のURL。

    ⚠️ これは外部サイトへの飾りのリンク。BOTの動作には関わらない。
       店頭で必要な注文番号は、BOTが作るレシート画像に入っている。

    設定が空のとき、または直近の確認でリンク先が落ちていたときは
    空文字を返す。呼び出し側はその場合ボタンを出さないこと。
    """
    from core import settings

    template = settings.get("receipt_view_url", config.RECEIPT_VIEW_URL)
    if not template or not _link_alive:
        return ""
    try:
        return template.format(store_id=store_id, receipt_number=receipt_number)
    except (KeyError, IndexError, ValueError):
        log.warning("受け取り画面のURLの書式が正しくありません: %s", template)
        return ""


async def check_link_alive(force: bool = False) -> bool:
    """
    リンク先が生きているか確かめる。

    落ちているサイトへのボタンを利用者に見せないための確認。
    確認できなかった場合は「生きている」ものとして扱う
    （こちらの回線の問題でボタンを消してしまわないように）。
    """
    global _link_alive, _link_checked_at
    import time

    from core import settings
    from core.http import build_async_client

    template = settings.get("receipt_view_url", config.RECEIPT_VIEW_URL)
    if not template:
        _link_alive = False
        return False

    interval = int(config.RECEIPT_URL_CHECK_MINUTES) * 60
    if not force and time.time() - _link_checked_at < interval:
        return _link_alive

    url = template.format(store_id="13934", receipt_number="0000")
    try:
        async with build_async_client(timeout=10.0, service="web") as client:
            r = await client.head(url)
            if r.status_code >= 400:
                r = await client.get(url)
        alive = r.status_code < 500
    except Exception as e:
        log.info("受け取り画面のURLを確認できませんでした（ボタンは出します）: %s", e)
        _link_checked_at = time.time()
        return _link_alive

    if alive != _link_alive:
        log.info(
            "受け取り画面のリンクを%sにしました（%s）",
            "表示" if alive else "非表示", url,
        )
    _link_alive, _link_checked_at = alive, time.time()
    return alive


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
