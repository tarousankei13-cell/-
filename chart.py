"""日別推移のグラフ画像を描く (Pillow のみ・外部サービスへ送信しない)。

Discord の Embed では数字の羅列しか出せないため、運用者が「増えているのか
減っているのか」を一目で判断できるようにグラフ画像を添える。

方針:

* 描画は Pillow だけで行う。集計データを外部サービスへ送らない。
* Pillow が入っていない環境でも Bot 全体は動かす (``available()`` で判定)。
* 日本語フォントが見つからない場合はラベルを英字へ切り替える。
  豆腐 (□□□) になるより読めるものを出す。
* 描画は CPU を使う同期処理なので、呼び出し側は必ず別スレッドで実行する
  (:func:`render_daily_chart` は同期関数のまま提供し、``asyncio.to_thread``
  で呼ぶ)。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

import config
import utils

logger = logging.getLogger(config.LOGGER_BOT)

try:  # Pillow は任意依存として扱う
    from PIL import Image, ImageDraw, ImageFont

    _PIL_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - 環境差で import できないことがある
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
    _PIL_ERROR = f"{type(exc).__name__}: {exc}"


#: 日本語を描けるフォントの候補 (Debian/Ubuntu の一般的な配置)
_JP_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf",
    "/usr/share/fonts/truetype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
)
#: 日本語フォントが無い場合に使う欧文フォントの候補
_FALLBACK_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)

#: 配色 (Discord のダークテーマに合わせる)
_BG = (32, 34, 37)
_PANEL = (41, 43, 47)
_GRID = (60, 63, 68)
_AXIS = (120, 124, 130)
_TEXT = (220, 221, 222)
_SUBTEXT = (150, 153, 158)
_BAR = (88, 101, 242)        # 受取額
_BAR_TOP = (120, 133, 255)
_LINE = (87, 242, 135)       # 件数
_MARKER = (255, 255, 255)


@dataclass(slots=True)
class DailyPoint:
    """1日ぶんの集計。"""

    day: str          # "2026-09-27"
    amount: int       # その日の送金額
    count: int        # その日の件数
    credited: int = 0  # その日の付与額


def available() -> bool:
    """グラフを描ける環境かどうか。"""
    return Image is not None


def unavailable_reason() -> str:
    """描けない理由 (管理者向けの説明)。"""
    if available():
        return ""
    return (
        "グラフの描画には Pillow が必要です。"
        "`pip install -r requirements.txt` で導入してください。"
        + (f" (詳細: {_PIL_ERROR})" if _PIL_ERROR else "")
    )


_font_cache: dict[tuple[str, int], Any] = {}
_jp_font_path: str | None | bool = False  # False=未探索 / None=見つからない


def _find_font_path() -> str | None:
    """日本語を描けるフォントを探す (見つからなければ欧文フォント)。"""
    global _jp_font_path
    if _jp_font_path is not False:
        return _jp_font_path  # type: ignore[return-value]
    from pathlib import Path

    for candidate in _JP_FONT_CANDIDATES:
        if Path(candidate).exists():
            _jp_font_path = candidate
            logger.info("グラフ用の日本語フォントを使用します: %s", candidate)
            return candidate
    for candidate in _FALLBACK_FONT_CANDIDATES:
        if Path(candidate).exists():
            _jp_font_path = candidate
            logger.warning(
                "日本語フォントが見つかりません。グラフは英字ラベルで描画します。"
                "日本語で表示するには `sudo apt install fonts-noto-cjk` を実行して"
                "Bot を再起動してください (使用中: %s)", candidate
            )
            return candidate
    _jp_font_path = None
    logger.warning("使用できるフォントが見つかりません。既定のビットマップフォントで描画します")
    return None


def _has_japanese_font() -> bool:
    """日本語フォントが使えるか (ラベルの言語を切り替えるため)。"""
    path = _find_font_path()
    return bool(path and path in _JP_FONT_CANDIDATES)


def _font(size: int) -> Any:
    """指定サイズのフォントを返す (キャッシュする)。"""
    path = _find_font_path()
    key = (path or "", size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    try:
        font = (
            ImageFont.truetype(path, size) if path else ImageFont.load_default()
        )
    except Exception:  # noqa: BLE001 - 壊れたフォントでも落とさない
        logger.exception("フォントの読み込みに失敗しました: %s", path)
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _text_size(draw: Any, text: str, font: Any) -> tuple[int, int]:
    """文字列の描画サイズ (Pillow のバージョン差を吸収する)。"""
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top
    except Exception:  # noqa: BLE001
        return len(text) * 6, 12


def _nice_step(maximum: int, target_lines: int = 4) -> int:
    """目盛りの間隔を「切りのよい数」に丸める。"""
    if maximum <= 0:
        return 1
    rough = max(1, maximum // max(1, target_lines))
    magnitude = 1
    while magnitude * 10 <= rough:
        magnitude *= 10
    for multiplier in (1, 2, 5, 10):
        step = magnitude * multiplier
        if step >= rough:
            return step
    return magnitude * 10


def _compact_number(value: int) -> str:
    """目盛り用の短い数値表記 (12,000 → 12k)。"""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}億".rstrip("0").rstrip(".") + ""
    if value >= 10_000:
        text = f"{value / 10_000:.1f}"
        return text.rstrip("0").rstrip(".") + "万"
    if value >= 1_000:
        text = f"{value / 1_000:.1f}"
        return text.rstrip("0").rstrip(".") + "k"
    return str(value)


def _compact_number_ascii(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(value)


def build_daily_series(
    rows: Sequence[Any], *, days: int, end_day: str | None = None
) -> list[DailyPoint]:
    """DB の集計結果を「欠けた日も 0 で埋めた連続した並び」にする。

    グラフでは日付が飛ぶと傾きが嘘になるため、チャージが無かった日も
    0 の点として必ず入れる。

    Args:
        rows: ``day`` / ``amount`` / ``count`` / ``credited`` を持つ行の並び。
        days: 何日ぶん並べるか。
        end_day: 最終日 (``YYYY-MM-DD``)。省略すると今日 (JST)。
    """
    by_day: dict[str, DailyPoint] = {}
    for row in rows:
        day = str(row["day"])
        by_day[day] = DailyPoint(
            day=day,
            amount=int(row["amount"] or 0),
            count=int(row["count"] or 0),
            credited=int(row["credited"] or 0) if "credited" in row.keys() else 0,
        )
    last = (
        datetime.strptime(end_day, "%Y-%m-%d")
        if end_day else
        datetime.fromtimestamp(utils.now_ts(), utils.JST)
    )
    series: list[DailyPoint] = []
    for offset in range(days - 1, -1, -1):
        key = (last - timedelta(days=offset)).strftime("%Y-%m-%d")
        series.append(by_day.get(key, DailyPoint(day=key, amount=0, count=0)))
    return series


def render_daily_chart(
    series: Sequence[DailyPoint],
    *,
    title: str,
    subtitle: str = "",
    width: int = config.CHART_WIDTH,
    height: int = config.CHART_HEIGHT,
) -> bytes | None:
    """日別推移の棒グラフ + 件数の折れ線を PNG バイト列で返す。

    Pillow が無い場合や描画に失敗した場合は ``None`` を返す
    (呼び出し側は文字の統計だけを出す)。

    この関数は同期処理なので、必ず ``asyncio.to_thread`` 経由で呼ぶこと。
    """
    if not available():
        return None
    if not series:
        return None
    try:
        return _render(series, title=title, subtitle=subtitle, width=width, height=height)
    except Exception:  # noqa: BLE001 - グラフが出ないだけで統計は出す
        logger.exception("グラフの描画に失敗しました")
        return None


def _render(
    series: Sequence[DailyPoint], *, title: str, subtitle: str,
    width: int, height: int,
) -> bytes:
    jp = _has_japanese_font()
    fmt_num = _compact_number if jp else _compact_number_ascii
    label_amount = "送金額" if jp else "Amount"
    label_count = "件数" if jp else "Count"
    label_empty = "この期間のチャージはありません" if jp else "No charges in this period"

    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    font_title = _font(20)
    font_sub = _font(13)
    font_axis = _font(12)
    font_small = _font(11)

    # --- 見出し ---
    draw.text((24, 18), title, font=font_title, fill=_TEXT)
    if subtitle:
        draw.text((24, 46), subtitle, font=font_sub, fill=_SUBTEXT)

    # --- 描画領域 ---
    top = 78
    bottom = height - 54
    left = 78
    right = width - 76
    draw.rectangle([left - 10, top - 10, right + 10, bottom + 10], fill=_PANEL)

    max_amount = max((p.amount for p in series), default=0)
    max_count = max((p.count for p in series), default=0)
    if max_amount <= 0 and max_count <= 0:
        text_w, text_h = _text_size(draw, label_empty, font_sub)
        draw.text(
            ((width - text_w) // 2, (top + bottom - text_h) // 2),
            label_empty, font=font_sub, fill=_SUBTEXT,
        )
        return _to_png(image)

    step = _nice_step(max_amount)
    axis_max = max(step, ((max_amount + step - 1) // step) * step)

    # --- 横の目盛りと補助線 ---
    line = 0
    while True:
        value = step * line
        if value > axis_max:
            break
        y = bottom - int((value / axis_max) * (bottom - top))
        draw.line([(left, y), (right, y)], fill=_GRID, width=1)
        text = fmt_num(value)
        text_w, text_h = _text_size(draw, text, font_axis)
        draw.text((left - 12 - text_w, y - text_h // 2), text, font=font_axis, fill=_SUBTEXT)
        line += 1

    # --- 棒 (送金額) ---
    count = len(series)
    slot = (right - left) / count
    bar_width = max(3, int(slot * 0.62))
    centers: list[float] = []
    for index, point in enumerate(series):
        center = left + slot * (index + 0.5)
        centers.append(center)
        if point.amount <= 0:
            continue
        bar_height = int((point.amount / axis_max) * (bottom - top))
        x0 = int(center - bar_width / 2)
        x1 = x0 + bar_width
        y0 = bottom - bar_height
        draw.rectangle([x0, y0, x1, bottom], fill=_BAR)
        # 上端を少し明るくして立体感を出す (濃淡で読みやすくする)
        draw.rectangle([x0, y0, x1, min(bottom, y0 + 3)], fill=_BAR_TOP)

    # --- 折れ線 (件数) ---
    if max_count > 0:
        # 件数は送金額と桁が違うので、右側に専用の目盛りを出す。
        # 目盛りが無いと折れ線の高さが何件なのか読み取れない。
        count_step = _nice_step(max_count, target_lines=3)
        count_axis = max(count_step, ((max_count + count_step - 1) // count_step) * count_step)
        usable = (bottom - top) * 0.92
        tick = 0
        while True:
            value = count_step * tick
            if value > count_axis:
                break
            y = bottom - int((value / count_axis) * usable)
            text = str(value)
            _, text_h = _text_size(draw, text, font_axis)
            draw.text((right + 12, y - text_h // 2), text, font=font_axis, fill=_LINE)
            tick += 1
        points = [
            (centers[i], bottom - int((p.count / count_axis) * usable))
            for i, p in enumerate(series)
        ]
        line_width = 3 if len(points) <= 45 else 2
        if len(points) >= 2:
            draw.line(points, fill=_LINE, width=line_width, joint="curve")
        # 点が多いとマーカーで線が潰れるため、一定数を超えたら描かない
        if len(points) <= 32:
            for x, y in points:
                draw.ellipse(
                    [x - 4, y - 4, x + 4, y + 4], fill=_MARKER, outline=_LINE, width=2
                )

    # --- 軸 ---
    draw.line([(left, top), (left, bottom)], fill=_AXIS, width=1)
    draw.line([(left, bottom), (right, bottom)], fill=_AXIS, width=1)

    # --- 日付ラベル (混み合う場合は間引く) ---
    label_every = 1
    while count / label_every > 12:
        label_every += 1
    # 直前に描いたラベルの右端。重なりそうなラベルは描かない。
    last_right = float("-inf")
    for index, point in enumerate(series):
        if index % label_every and index != count - 1:
            continue
        text = point.day[5:]  # MM-DD
        text_w, _ = _text_size(draw, text, font_small)
        x = centers[index] - text_w / 2
        if x < last_right + 6:
            continue
        draw.text((x, bottom + 8), text, font=font_small, fill=_SUBTEXT)
        last_right = x + text_w

    # --- 凡例 ---
    legend_y = height - 26
    draw.rectangle([24, legend_y, 40, legend_y + 12], fill=_BAR)
    draw.text((46, legend_y - 1), label_amount, font=font_small, fill=_SUBTEXT)
    amount_w, _ = _text_size(draw, label_amount, font_small)
    line_x = 46 + amount_w + 20
    draw.line([(line_x, legend_y + 6), (line_x + 16, legend_y + 6)], fill=_LINE, width=3)
    draw.ellipse(
        [line_x + 5, legend_y + 1, line_x + 13, legend_y + 9],
        fill=_MARKER, outline=_LINE, width=2,
    )
    draw.text((line_x + 22, legend_y - 1), label_count, font=font_small, fill=_SUBTEXT)

    # --- 右下に最大値を添える (目盛りだけでは実数が分からないため) ---
    peak = f"max {utils.fmt_int(max_amount)} / {max_count}"
    peak_w, _ = _text_size(draw, peak, font_small)
    draw.text((right - peak_w, legend_y - 1), peak, font=font_small, fill=_SUBTEXT)
    return _to_png(image)


def _to_png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
