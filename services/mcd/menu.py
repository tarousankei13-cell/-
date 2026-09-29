"""
メニューカタログの解析

取得元: {catRootUrl}/{storeId}/menu.json （認証不要・約1MB）
構造の根拠は docs/08。

このモジュールが解決すること:
  - 商品名（products には名前が無いので、他セクションから再帰的に集める）
  - 受取方法ごとの価格（EATIN / TAKEOUT / OTHER で異なる）
  - 時間帯による販売可否（朝マック / ヒルマック / 夜マック）
  - セットの構成（固定構成・選択枠・追加トッピング）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

log = logging.getLogger("bot.menu")

# 受取方法 → menu.json の priceCode
PRICE_CODE = {
    "eatIn": "EATIN",
    "tableDelivery": "EATIN",
    "takeOut": "TAKEOUT",
    "curbsidePickUp": "TAKEOUT",
    "driveThru": "TAKEOUT",
    "addressDelivery": "OTHER",
}


# ============================================================
#  商品名の収集
# ============================================================

def harvest_names(node: Any, out: dict[str, str] | None = None) -> dict[str, str]:
    """
    menu.json 全体を再帰的に走査して 商品コード → 日本語名 を集める。

    products には名前が入っていないが、sizeVariants / collections /
    relatedSets などに tName があり、これで全商品をカバーできる。
    """
    if out is None:
        out = {}
    if isinstance(node, dict):
        code, tname = node.get("productCode"), node.get("tName")
        if code and isinstance(tname, dict) and tname.get("ja"):
            out.setdefault(str(code), tname["ja"])
        for v in node.values():
            harvest_names(v, out)
    elif isinstance(node, list):
        for v in node:
            harvest_names(v, out)
    return out


def build_size_names(menu: dict) -> dict[str, str]:
    """
    サイズ違いの商品名を「親の名前 + サイズ」に直す。

    sizeVariants では 2020 の名前が単に "M" になっているため、
    そのままだと「M」とだけ表示されてしまう。
    """
    out: dict[str, str] = {}
    for info in (menu.get("sizeVariants") or {}).values():
        if not isinstance(info, dict):
            continue
        parent = ((info.get("tName") or {}).get("ja") or "").strip()
        if not parent:
            continue
        for size in info.get("sizes") or []:
            code = str(size.get("productCode") or "")
            label = ((size.get("tName") or {}).get("ja") or "").strip()
            if code:
                out[code] = f"{parent} {label}".strip() if label else parent
    return out


# ============================================================
#  データ構造
# ============================================================

@dataclass
class Slot:
    """セットの構成要素（固定構成 / 選択枠 / 追加トッピング）。"""
    kind: str                 # composition / choices / canAdds
    code: str
    default_product: str = ""
    reference_product: str = ""
    min_quantity: int = 0
    max_quantity: int = 1
    extra_price: int = 0      # この枠を選んだときの加算額
    cost_inclusive: bool = True

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        return cls(**d)


@dataclass
class Product:
    code: str
    name: str
    product_class: str = "PRODUCT"     # PRODUCT / VALUE_MEAL
    day_part: str = ""
    price_eatin: int = 0
    price_takeout: int = 0
    price_other: int = 0
    pre_price: int = 0                 # セットの表示価格
    slots: list[Slot] = field(default_factory=list)
    time_windows: list[dict] = field(default_factory=list)  # [{start,end}] 分単位

    def price_for(self, pickup_method: str) -> int:
        """
        受取方法に応じた価格。

        セット商品は prePrice（構成込みの表示価格）を使う。
        """
        if self.product_class == "VALUE_MEAL" and self.pre_price:
            return self.pre_price
        code = PRICE_CODE.get(pickup_method, "TAKEOUT")
        return {
            "EATIN": self.price_eatin,
            "TAKEOUT": self.price_takeout,
            "OTHER": self.price_other,
        }.get(code, self.price_takeout)

    def is_orderable_at(self, minutes: int) -> bool:
        """その時刻に注文できるか。時間帯の定義が無ければ常に可。"""
        if not self.time_windows:
            return True
        return any(w["start"] <= minutes < w["end"] for w in self.time_windows)

    def slots_of(self, kind: str) -> list[Slot]:
        return [s for s in self.slots if s.kind == kind]

    def structure_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.slots], ensure_ascii=False)


@dataclass
class Collection:
    id: str
    name: str
    product_codes: list[str]
    sort_order: int = 0


@dataclass
class ParsedMenu:
    store_id: str
    products: dict[str, Product]
    collections: list[Collection]

    def visible_products(self, collection_id: str, minutes: int | None = None) -> list[Product]:
        col = next((c for c in self.collections if c.id == collection_id), None)
        if not col:
            return []
        out = []
        for code in col.product_codes:
            p = self.products.get(str(code))
            if p and (minutes is None or p.is_orderable_at(minutes)):
                out.append(p)
        return out


# ============================================================
#  解析
# ============================================================

def _price_of(entry: dict, key: str) -> int:
    v = entry.get(key)
    if isinstance(v, dict):
        return int(v.get("price") or 0) if v.get("valid", True) else 0
    return 0


def _price_list(entry: dict) -> dict[str, int]:
    out = {}
    for p in entry.get("priceList") or []:
        code = p.get("priceCode")
        if code:
            out[code] = int(p.get("price") or 0)
    return out


def _parse_slots(raw: dict) -> list[Slot]:
    slots: list[Slot] = []
    for kind in ("composition", "choices", "canAdds"):
        for c in raw.get(kind) or []:
            if not isinstance(c, dict):
                continue
            slots.append(
                Slot(
                    kind=kind,
                    code=str(c.get("productCode") or ""),
                    default_product=str(c.get("defaultProduct") or ""),
                    reference_product=str(c.get("referenceProduct") or ""),
                    min_quantity=int(c.get("minQuantity") or 0),
                    max_quantity=int(c.get("maxQuantity") or 1),
                    extra_price=_price_of(c, "prePrice"),
                    cost_inclusive=bool(c.get("costInclusive", True)),
                )
            )
    return slots


def _time_windows(menu: dict, code: str) -> list[dict]:
    """limitedAbility から、その商品の注文可能な時間帯を取り出す。"""
    limited = menu.get("limitedAbility") or {}
    for day in limited.values():
        ability = (day or {}).get("ability") or {}
        entry = ability.get(code)
        if entry:
            return [
                {"start": int(w["start"]), "end": int(w["end"])}
                for w in (entry.get("checkoutable") or [])
                if "start" in w and "end" in w
            ]
    return []


def parse_menu(store_id: str, menu: dict) -> ParsedMenu:
    names = harvest_names(menu)
    names.update(build_size_names(menu))   # サイズ違いは親名＋サイズで上書き

    products: dict[str, Product] = {}
    for code, raw in (menu.get("products") or {}).items():
        if not isinstance(raw, dict):
            continue
        code = str(code)
        pl = _price_list(raw)
        products[code] = Product(
            code=code,
            name=names.get(code, f"商品 {code}"),
            product_class=str(raw.get("productClass") or "PRODUCT"),
            day_part=str(raw.get("dayPart") or ""),
            price_eatin=pl.get("EATIN", 0),
            price_takeout=pl.get("TAKEOUT", 0),
            price_other=pl.get("OTHER", 0),
            pre_price=_price_of(raw, "prePrice"),
            slots=_parse_slots(raw),
            time_windows=_time_windows(menu, code),
        )

    collections: list[Collection] = []
    for i, c in enumerate(menu.get("collections") or []):
        if not isinstance(c, dict):
            continue
        collections.append(
            Collection(
                id=str(c.get("id") or c.get("collectionId") or i),
                name=((c.get("tName") or {}).get("ja") or "その他"),
                product_codes=[str(x) for x in (c.get("productCodes") or [])],
                sort_order=i,
            )
        )

    log.info("メニューを解析しました: 店舗%s 商品%d件 カテゴリ%d件",
             store_id, len(products), len(collections))
    return ParsedMenu(store_id=store_id, products=products, collections=collections)


# ============================================================
#  店舗の時間帯
# ============================================================

@dataclass
class Daypart:
    name: str
    visible: list[dict]
    checkoutable: list[dict]

    def active_at(self, minutes: int) -> bool:
        return any(w["start"] <= minutes < w["end"] for w in self.checkoutable)


def parse_dayparts(store: dict, date_key: str | None = None) -> list[Daypart]:
    """mopDaypartAbilityLists から時間帯を取り出す。"""
    lists = store.get("mopDaypartAbilityLists") or {}
    if not lists:
        return []
    key = date_key if date_key in lists else sorted(lists)[0]
    out = []
    for a in (lists.get(key) or {}).get("daypartAbilities") or []:
        out.append(
            Daypart(
                name=str(a.get("daypart") or ""),
                visible=[{"start": int(w["start"]), "end": int(w["end"])}
                         for w in (a.get("visible") or [])],
                checkoutable=[{"start": int(w["start"]), "end": int(w["end"])}
                              for w in (a.get("checkoutable") or [])],
            )
        )
    return out


def supported_pickup_methods(store: dict) -> dict[str, bool]:
    """店舗が対応している受取方法。対応外は選択肢に出さない。"""
    from services.mcd.protocol import STORE_DELIVERY_KEY

    dm = (store.get("store") or {}).get("deliveryMethod") or {}
    return {
        method: bool((dm.get(key) or {}).get("isSupported"))
        for method, key in STORE_DELIVERY_KEY.items()
    }


def minutes_of(dt: datetime) -> int:
    """0時からの経過分。menu.json の時間帯はこの単位。"""
    return dt.hour * 60 + dt.minute


# ============================================================
#  差分（新商品・終売・価格改定の通知用）
# ============================================================

@dataclass
class MenuDiff:
    added: list[tuple[str, str]] = field(default_factory=list)          # (code, name)
    removed: list[tuple[str, str]] = field(default_factory=list)
    price_changed: list[tuple[str, str, int, int]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.price_changed)


def diff_menus(old: Iterable[tuple[str, str, int]], new: ParsedMenu) -> MenuDiff:
    """
    old: DBにある (product_code, name, price_takeout) の一覧
    """
    old_map = {c: (n, p) for c, n, p in old}
    d = MenuDiff()
    for code, prod in new.products.items():
        if code not in old_map:
            d.added.append((code, prod.name))
        else:
            _, old_price = old_map[code]
            if old_price != prod.price_takeout:
                d.price_changed.append((code, prod.name, old_price, prod.price_takeout))
    for code, (name, _) in old_map.items():
        if code not in new.products:
            d.removed.append((code, name))
    return d
