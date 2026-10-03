"""
その店舗が「いま注文できるか」の判定

注文できない店舗を選ばせてしまうと、商品を選び終えてから
「注文できません」と言われることになる。それでは遅いので、
**店舗を選んだ時点で**理由を説明して止める。

判定の材料（すべて日本時間）:

| 材料 | 意味 |
|---|---|
| `store.mopEnabled` | そもそもモバイルオーダーに対応しているか |
| `store.foeStatus` | 店舗の稼働状態。NORMAL 以外は一時休業など |
| `store.openingHours.businessHours[日付]` | 店舗の営業時間 |
| `store.deliveryMethod.<方法>.isSupported` | その受取方法に対応しているか |
| `store.deliveryMethod.<方法>.businessHours` | 受取方法ごとの営業時間 |
| `mopDaypartAbilityLists[日付].daypartAbilities[].checkoutable` | **注文を受け付けている時間帯** |

最後の `checkoutable` が本命。朝マックとレギュラーの切り替え時など、
店は開いていても注文を受け付けない時間がある。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import config
from services.mcd.protocol import PICKUP_LABEL, STORE_DELIVERY_KEY

log = logging.getLogger("bot.availability")

# 時間帯の名前を日本語に
DAYPART_LABEL = {
    "DAYPART_BREAKFAST": "朝マック",
    "DAYPART_REGULAR": "レギュラーメニュー",
    "DAYPART_BREAKFAST_REGULAR": "終日メニュー",
    "DAYPART_HIRU_MAC": "ヒルマック",
    "DAYPART_YORU_MAC": "夜マック",
}

# 判定結果の種類
OK = "OK"
NOT_MOP = "NOT_MOP"              # モバイルオーダー非対応の店舗
SUSPENDED = "SUSPENDED"          # 一時休業など
CLOSED = "CLOSED"                # 営業時間外
OUTSIDE = "OUTSIDE"              # 営業中だが受付停止の時間帯
NO_METHOD = "NO_METHOD"          # 対応している受取方法が無い
METHOD_CLOSED = "METHOD_CLOSED"  # その受取方法の受付時間外
UNKNOWN = "UNKNOWN"              # 判断材料が無い


def fmt(minutes: int | None) -> str:
    """分を時刻表記に。1440を超える値は翌日として扱う。"""
    if minutes is None:
        return "—"
    m = int(minutes)
    day = ""
    if m >= 1440:
        m -= 1440
        day = "翌"
    return f"{day}{m // 60}:{m % 60:02d}"


@dataclass
class Availability:
    """判定の結果。利用者に出す文言もここで持つ。"""
    orderable: bool
    code: str = OK
    title: str = ""
    reason: str = ""
    next_at: int | None = None        # 次に受け付ける時刻（分）
    closes_at: int | None = None      # いまの受付が終わる時刻（分）
    windows: list[dict] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)   # いま使える受取方法

    @property
    def hint(self) -> str:
        """次に注文できる時刻の案内。分からなければ空。"""
        if self.orderable or self.next_at is None:
            return ""
        return f"次に注文できるのは **{fmt(self.next_at)}** からです。"

    def window_text(self) -> str:
        """受付時間の一覧。「6:00〜23:50」の形。"""
        if not self.windows:
            return "—"
        return " / ".join(f"{fmt(w['start'])}〜{fmt(w['end'])}" for w in self.windows)


# ------------------------------------------------------------
#  時間帯の計算
# ------------------------------------------------------------

def _norm(windows: list[dict]) -> list[dict]:
    """開始が早い順に並べ、おかしい値を捨てる。"""
    out = []
    for w in windows or []:
        try:
            start, end = int(w["start"]), int(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            out.append({"start": start, "end": end})
    return sorted(out, key=lambda w: w["start"])


def _merge(windows: list[dict]) -> list[dict]:
    """重なっている時間帯をつなげる（6:00-10:20 と 10:20-23:50 → 6:00-23:50）。"""
    merged: list[dict] = []
    for w in _norm(windows):
        if merged and w["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], w["end"])
        else:
            merged.append(dict(w))
    return merged


def _contains(minutes: int, windows: list[dict]) -> dict | None:
    """いまがどの時間帯に入っているか。日をまたぐ指定にも対応する。"""
    for w in windows:
        if w["start"] <= minutes < w["end"]:
            return w
        # end が 1440 を超える＝翌日にまたがる指定。
        # 午前0〜6時は minutes が小さいので、1日ぶん足して比べる。
        if w["end"] > 1440 and w["start"] <= minutes + 1440 < w["end"]:
            return w
    return None


def _next_start(minutes: int, windows: list[dict]) -> int | None:
    """次に始まる時間帯の開始時刻。今日の残りに無ければ翌日の最初。"""
    later = [w["start"] for w in windows if w["start"] > minutes]
    if later:
        return min(later)
    if windows:
        return windows[0]["start"] + 1440   # 翌日
    return None


def checkout_windows(raw: dict, date_key: str) -> list[dict]:
    """
    その日に注文を受け付けている時間帯（全メニューの合算）。

    日付のキーが見つからない場合は空を返す。
    別の日の時間帯を代わりに使うと、誤った案内になるため。
    """
    lists = raw.get("mopDaypartAbilityLists") or {}
    day = lists.get(date_key)
    if not day:
        return []
    windows: list[dict] = []
    for ability in day.get("daypartAbilities") or []:
        windows += ability.get("checkoutable") or []
    return _merge(windows)


def daypart_now(raw: dict, date_key: str, minutes: int) -> str:
    """いまの時間帯の名前（朝マック・夜マックなど）。分からなければ空。"""
    lists = raw.get("mopDaypartAbilityLists") or {}
    day = lists.get(date_key)
    if not day:
        return ""
    # 範囲の狭いものを優先する（終日メニューより朝マックを出したい）
    best, best_width = "", 10**9
    for ability in day.get("daypartAbilities") or []:
        name = str(ability.get("daypart") or "")
        if name in ("DAYPART_BREAKFAST_REGULAR",):
            continue
        hit = _contains(minutes, _merge(ability.get("checkoutable") or []))
        if hit:
            width = hit["end"] - hit["start"]
            if width < best_width:
                best, best_width = name, width
    return DAYPART_LABEL.get(best, "")


# カテゴリ名と時間帯の対応。
# 「朝マック」のカテゴリを夜に出しても、中身はほとんど注文できない。
COLLECTION_DAYPART = {
    "朝マック": "DAYPART_BREAKFAST",
    "ヒルマック": "DAYPART_HIRU_MAC",
    "ひるまック": "DAYPART_HIRU_MAC",
    "夜マック": "DAYPART_YORU_MAC",
    "よるマック": "DAYPART_YORU_MAC",
}


def active_dayparts(raw: dict, date_key: str, minutes: int) -> set[str]:
    """いま注文を受け付けている時間帯の名前。"""
    lists = raw.get("mopDaypartAbilityLists") or {}
    day = lists.get(date_key)
    if not day:
        return set()
    out = set()
    for ability in day.get("daypartAbilities") or []:
        windows = _merge(ability.get("checkoutable") or [])
        if windows and _contains(minutes, windows):
            out.add(str(ability.get("daypart") or ""))
    return out


def collection_available(name: str, active: set[str]) -> bool:
    """
    そのカテゴリを出してよいか。

    時間帯に結びついたカテゴリ（朝マック・夜マックなど）は、
    その時間帯が終わっていれば出さない。
    時間帯が分からない場合は出す（止めるほどの確証が無いため）。
    """
    daypart = COLLECTION_DAYPART.get((name or "").strip())
    if daypart is None:
        return True          # 時間帯に結びついていないカテゴリ
    if not active:
        return True          # 時間帯が分からない
    return daypart in active


def _method_windows(store: dict, method: str, date_key: str) -> list[dict]:
    """受取方法ごとの営業時間。"""
    key = STORE_DELIVERY_KEY.get(method)
    if not key:
        return []
    node = (store.get("deliveryMethod") or {}).get(key) or {}
    hours = (node.get("businessHours") or {}).get("businessHours") or {}
    w = hours.get(date_key)
    return _merge([w]) if w else []


def method_hours(raw: dict) -> dict:
    """
    受取方法ごとの営業時間を取り出す。

    戻り値: {"eatIn": {"2026-09-30": {"start": 360, "end": 1410}}, ...}
    DBに保存して、次回以降は通信せずに判定できるようにする。
    """
    store = raw.get("store") or raw
    dm = store.get("deliveryMethod") or {}
    out: dict[str, dict] = {}
    for method, key in STORE_DELIVERY_KEY.items():
        node = dm.get(key) or {}
        if not node.get("isSupported"):
            continue
        hours = (node.get("businessHours") or {}).get("businessHours") or {}
        clean = {}
        for date, w in hours.items():
            try:
                clean[date] = {"start": int(w["start"]), "end": int(w["end"])}
            except (KeyError, TypeError, ValueError):
                continue
        if clean:
            out[method] = clean
    return out


# ------------------------------------------------------------
#  判定
# ------------------------------------------------------------

def check(
    raw: dict,
    *,
    minutes: int | None = None,
    date_key: str | None = None,
    pickup_method: str | None = None,
) -> Availability:
    """
    いま注文できるかを判定する。

    raw は data.cat から取った店舗のJSON全体（store キーを含む）。
    minutes / date_key を省いたときは日本時間の現在を使う。
    pickup_method を渡すと、その受取方法まで見て判定する。
    """
    now = config.now_jst()
    if minutes is None:
        minutes = now.hour * 60 + now.minute
    if date_key is None:
        date_key = now.strftime("%Y-%m-%d")

    store = raw.get("store") or raw

    # ① そもそもモバイルオーダーに対応しているか
    if not store.get("mopEnabled", True):
        return Availability(
            False, NOT_MOP,
            title="この店舗はモバイルオーダーに対応していません",
            reason=(
                "お店の設備や立地の都合で、アプリからの注文を受け付けていません。\n"
                "別の店舗をお選びください。"
            ),
        )

    # ② 一時休業などでないか
    status = str(store.get("foeStatus") or "NORMAL").upper()
    if status not in ("NORMAL", ""):
        return Availability(
            False, SUSPENDED,
            title="この店舗は現在ご利用いただけません",
            reason=(
                "改装や設備点検などで、一時的に注文を停止しています。\n"
                "別の店舗をお選びください。"
            ),
        )

    # ③ 対応している受取方法があるか
    supported = [
        m for m, key in STORE_DELIVERY_KEY.items()
        if ((store.get("deliveryMethod") or {}).get(key) or {}).get("isSupported")
    ]
    # 配達はこのBOTでは扱わない
    supported = [m for m in supported if m != "addressDelivery"]
    if not supported:
        return Availability(
            False, NO_METHOD,
            title="この店舗では受け取れません",
            reason="対応している受け取り方法がありません。別の店舗をお選びください。",
        )

    # ④ 注文を受け付けている時間帯か（本命）
    windows = checkout_windows(raw, date_key)
    if not windows:
        # 時間帯の情報が無い。営業時間で代用する。
        opening = (store.get("openingHours") or {}).get("businessHours") or {}
        windows = _merge([opening[date_key]]) if date_key in opening else []
    if not windows:
        # 判断材料が無い。止めるほどの確証は無いので通す。
        log.info("店舗 %s の受付時間が分かりません（%s）", store.get("id"), date_key)
        return Availability(
            True, UNKNOWN,
            title="",
            reason="",
            methods=supported,
        )

    hit = _contains(minutes, windows)
    if hit is None:
        opening = (store.get("openingHours") or {}).get("businessHours") or {}
        open_windows = _merge([opening[date_key]]) if date_key in opening else []
        closed = open_windows and _contains(minutes, open_windows) is None
        nxt = _next_start(minutes, windows)
        return Availability(
            False, CLOSED if closed else OUTSIDE,
            title=(
                "ただいま営業時間外です" if closed
                else "ただいまの時間は注文を受け付けていません"
            ),
            reason=(
                "この店舗は現在閉まっています。"
                if closed else
                "お店は開いていますが、メニューの切り替えなどで\n"
                "アプリからの注文を受け付けていない時間帯です。"
            ),
            next_at=nxt,
            windows=windows,
            methods=supported,
        )

    # ⑤ 指定された受取方法が使えるか
    if pickup_method:
        if pickup_method not in supported:
            return Availability(
                False, NO_METHOD,
                title=f"この店舗は「{PICKUP_LABEL.get(pickup_method, pickup_method)}」に対応していません",
                reason="別の受け取り方法をお選びください。",
                methods=supported,
                windows=windows,
                closes_at=hit["end"],
            )
        mw = _method_windows(store, pickup_method, date_key)
        if mw and _contains(minutes, mw) is None:
            return Availability(
                False, METHOD_CLOSED,
                title=f"ただいま「{PICKUP_LABEL.get(pickup_method, pickup_method)}」は受け付けていません",
                reason=(
                    "この受け取り方法の対応時間は "
                    + " / ".join(f"{fmt(w['start'])}〜{fmt(w['end'])}" for w in mw)
                    + " です。"
                ),
                next_at=_next_start(minutes, mw),
                windows=mw,
                methods=supported,
            )

    # いま使える受取方法だけに絞る
    usable = [
        m for m in supported
        if not _method_windows(store, m, date_key)
        or _contains(minutes, _method_windows(store, m, date_key))
    ]
    return Availability(
        True, OK,
        title=daypart_now(raw, date_key, minutes),
        next_at=None,
        closes_at=hit["end"],
        windows=windows,
        methods=usable or supported,
    )


# ------------------------------------------------------------
#  保存済みの情報から判定する（通信しない）
# ------------------------------------------------------------

async def check_store(
    store_id: str,
    *,
    minutes: int | None = None,
    date_key: str | None = None,
    pickup_method: str | None = None,
) -> Availability:
    """
    DBに保存済みの情報だけで判定する。

    店舗を選んだ直後に呼ぶので、ここで通信はしない。
    店舗情報は resolve_store が取得済み・定期同期で最新に保たれている。

    保存が無い店舗（まだ一度も使っていない）は判断できないので、
    止めずに通す。注文の直前にもう一度確かめられる。
    """
    from db.models import StoreCache, StoreDaypart
    from db.session import session_scope
    from sqlalchemy import select

    now = config.now_jst()
    if minutes is None:
        minutes = now.hour * 60 + now.minute
    if date_key is None:
        date_key = now.strftime("%Y-%m-%d")

    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
        if row is None:
            return Availability(True, UNKNOWN)
        dayparts = (
            await s.execute(
                select(StoreDaypart).where(
                    StoreDaypart.store_id == store_id, StoreDaypart.date == date_key
                )
            )
        ).scalars().all()
        methods = json.loads(row.delivery_methods or "{}")
        hours = json.loads(row.method_hours or "{}")
        parts = [
            {"daypart": d.daypart, "checkoutable": json.loads(d.checkoutable or "[]")}
            for d in dayparts
        ]
        mop_enabled = bool(row.mop_enabled)
        foe_status = row.foe_status or "NORMAL"

    # 保存済みの形から、raw と同じ形を組み立てて共通の判定にかける
    raw = {
        "store": {
            "id": store_id,
            "mopEnabled": mop_enabled,
            "foeStatus": foe_status,
            "deliveryMethod": {
                STORE_DELIVERY_KEY[m]: {
                    "isSupported": bool(ok),
                    "businessHours": {
                        "businessHours": hours.get(m, {})
                    },
                }
                for m, ok in methods.items()
                if m in STORE_DELIVERY_KEY
            },
        },
        "mopDaypartAbilityLists": {
            date_key: {
                "daypartAbilities": [
                    {"daypart": p["daypart"], "checkoutable": p["checkoutable"]}
                    for p in parts
                ]
            }
        } if parts else {},
    }
    return check(raw, minutes=minutes, date_key=date_key, pickup_method=pickup_method)


async def active_dayparts_for(
    store_id: str, *, minutes: int | None = None, date_key: str | None = None
) -> set[str]:
    """
    保存済みの情報から、いま受け付けている時間帯の名前を返す。

    通信はしない。店舗を選んだ直後に呼ぶため。
    """
    from db.models import StoreDaypart
    from db.session import session_scope
    from sqlalchemy import select

    now = config.now_jst()
    if minutes is None:
        minutes = now.hour * 60 + now.minute
    if date_key is None:
        date_key = now.strftime("%Y-%m-%d")

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(StoreDaypart).where(
                    StoreDaypart.store_id == str(store_id),
                    StoreDaypart.date == date_key,
                )
            )
        ).scalars().all()
        parts = [(d.daypart, json.loads(d.checkoutable or "[]")) for d in rows]

    out = set()
    for name, windows in parts:
        merged = _merge(windows)
        if merged and _contains(minutes, merged):
            out.add(str(name))
    return out
