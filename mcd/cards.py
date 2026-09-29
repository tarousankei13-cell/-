"""受け取り番号カードの生成と、画像の重複検知。

カードは BOT 独自デザイン。店頭で必要なのは番号そのものなので、
番号の可読性を最優先にしている。short_code も併記して、
番号だけで引けなかったときの第2キーにする。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("bot.cards")

# 日本語が出せるフォントの候補。上から順に探す。
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-mincho.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
]

_font_path: Optional[str] = None
_warned = False


def configure_font(path: str = "") -> Optional[str]:
    """使用するフォントを決める。main.py の FONT_PATH から呼ぶ。"""
    global _font_path, _warned
    candidates = ([path] if path else []) + FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            _font_path = candidate
            log.info("カード生成に使用するフォント: %s", candidate)
            return candidate
    if not _warned:
        log.warning(
            "日本語フォントが見つかりません。番号は出ますが日本語が豆腐になります。"
            " main.py の FONT_PATH にフォントのパスを設定してください。"
        )
        _warned = True
    return None


def _font(size: int) -> ImageFont.FreeTypeFont:
    if _font_path:
        try:
            return ImageFont.truetype(_font_path, size)
        except OSError:
            pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _center(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill, width: int) -> int:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, y - top), text, font=font, fill=fill)
    return bottom - top


def render_pickup_card(
    receipt_number: str,
    store_name: str,
    store_id: str,
    pickup: str,
    when: str,
    short_code: str = "",
    brand: str = "",
) -> bytes:
    """番号カードを PNG バイト列で返す。"""
    W, H = 1000, 640
    BG = (250, 250, 249)
    INK = (23, 23, 23)
    MUTED = (115, 113, 108)
    LINE = (222, 220, 216)
    ACCENT = (32, 96, 78)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 外枠
    d.rounded_rectangle([12, 12, W - 12, H - 12], radius=28, outline=LINE, width=3)
    d.rounded_rectangle([12, 12, W - 12, 20], radius=0, fill=ACCENT)

    _center(d, 58, "ご注文を承りました", _font(34), INK, W)
    _center(d, 118, "注 文 番 号", _font(26), MUTED, W)

    number = (receipt_number or "----").strip()
    size = 200 if len(number) <= 4 else (170 if len(number) <= 6 else 130)
    _center(d, 168, number, _font(size), ACCENT, W)

    d.line([70, 420, W - 70, 420], fill=LINE, width=2)

    label_font = _font(24)
    value_font = _font(30)
    y = 452

    def row(label: str, value: str) -> None:
        nonlocal y
        if not value:
            return
        d.text((78, y + 6), label, font=label_font, fill=MUTED)
        d.text((260, y), value, font=value_font, fill=INK)
        y += 46

    shop = store_name or "-"
    if store_id:
        shop = f"{shop}  ({store_id})"
    row("店舗", shop)
    row("受取方法", pickup or "-")
    row("日時", when)
    if short_code:
        row("照合コード", short_code)

    if brand:
        foot = _font(20)
        left, top, right, bottom = d.textbbox((0, 0), brand, font=foot)
        d.text((W - 78 - (right - left), 62), brand, font=foot, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------- 画像の重複検知


def dhash(data: bytes, size: int = 8) -> str:
    """差分ハッシュ。リサイズや軽い加工では変わりにくい。"""
    with Image.open(io.BytesIO(data)) as img:
        small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
        px = list(small.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 999
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def find_similar(phash: str, known: list, threshold: int = 6):
    """既知のハッシュから近いものを返す。戻り値は (phash, user_id, 距離)。"""
    best = None
    for row in known:
        distance = hamming(phash, row["phash"])
        if distance <= threshold and (best is None or distance < best[2]):
            best = (row["phash"], int(row["user_id"]), distance)
    return best
