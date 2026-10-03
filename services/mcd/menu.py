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
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Iterable

import config

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
    # 具材名。ピクルスや氷などの符号は products に載っていないため、
    # 解析時にここへ入れて保存する（あとから引けるように）。
    name: str = ""
    default_product: str = ""
    reference_product: str = ""
    min_quantity: int = 0
    max_quantity: int = 1
    default_quantity: int = 1
    extra_price: int = 0      # この枠を選んだときの加算額
    cost_inclusive: bool = True

    @property
    def removable(self) -> bool:
        """外せる具材か（ピクルス抜き・氷抜きなど）。"""
        return self.min_quantity < self.default_quantity

    @property
    def increasable(self) -> bool:
        """増やせる具材か（チーズ追加など）。"""
        return self.max_quantity > self.default_quantity

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        # 列が増える前に保存した内容も読めるようにする
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


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
    # 注文できる時間帯。
    #   None … カタログに登録が無い（分からないので止めない）
    #   []   … 登録はあるが空＝**この店舗では扱っていない**
    #   [..] … その時間帯だけ注文できる
    time_windows: list[dict] | None = None
    size_group: str = ""      # サイズ違いをまとめる代表コード

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
        """
        その時刻に注文できるか。

        ⚠️ **空の時間帯を「制限なし」と読んではいけない。**
           カタログには `checkoutable: []` の商品がある。これは
           「見えるが注文はできない」＝この店舗では扱っていない、という意味。
           実例: ひるまックを扱わない店舗では、ひるまック商品がこの形になる。
           制限なしと誤読すると、注文できない商品をいつでも表示してしまう。

           登録そのものが無い（None）ときだけ「分からないので止めない」。
        """
        if self.time_windows is None:
            return True                      # 分からない → 止めない
        if not self.time_windows:
            return False                     # 登録はあるが空 → 扱っていない
        return any(w["start"] <= minutes < w["end"] for w in self.time_windows)

    @property
    def never_orderable(self) -> bool:
        """この店舗では扱っていない商品か。"""
        return self.time_windows is not None and not self.time_windows

    def slots_of(self, kind: str) -> list[Slot]:
        return [s for s in self.slots if s.kind == kind]

    def customizations(self) -> list[Slot]:
        """
        利用者がいじれる具材。

        `composition` のうち、減らせる（抜ける）か増やせるものだけを返す。
        バンズやパティのように数量が固定のものは対象外。

        ⚠️ 名前が分からないもの（内部用の符号）は返さない。
           「99901001 を抜く」と出しても利用者は判断できないため。
           名前の解決は呼び出し側で行う（ここはコードだけを持つ）。
        """
        return [
            s for s in self.slots
            if s.kind == "composition"
            and (s.removable or s.increasable)
            and s.name                      # 名前が分からないものは出さない
        ]

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

    size_groups: dict[str, str] = field(default_factory=dict)   # code → 代表コード

    def collection_of(self, code: str) -> "Collection | None":
        """
        その商品が属するカテゴリ。

        複数に属することがある（ハッシュポテトは「サイドメニュー」と
        「朝マック」の両方）。選択枠の候補に使うので、**一番小さい**
        ＝一番具体的なカテゴリを返す。
        """
        hits = [c for c in self.collections if str(code) in c.product_codes]
        return min(hits, key=lambda c: len(c.product_codes)) if hits else None

    def size_variants(self, code: str) -> list[Product]:
        """同じ商品のサイズ違い（S/M/L）。"""
        group = self.size_groups.get(str(code))
        if not group:
            return []
        out = [
            p for c, p in self.products.items()
            if self.size_groups.get(c) == group
        ]
        return sorted(out, key=lambda p: p.code)

    def choice_candidates(
        self, slot: Slot, minutes: int | None = None
    ) -> list[Product]:
        """
        セットの選択枠（ドリンク・サイド）に入れられる商品。

        ⚠️ カタログには枠の中身が直接書かれていない。
           枠は `9997925` のような符号を持つが、これはどこにも定義が無い。
           代わりに `referenceProduct`（例: コカ・コーラ(M)）が
           示されているので、**その商品が属するカテゴリ全体**を候補にする。
           これで「ドリンクはコーラしか選べない」状態を解消できる。

        さらに、その時刻に取り扱いのない商品は候補から外す。
        朝の時間にマックフライポテトを選ばせると、注文時に
        マクドナルド側から「ただいまのお時間は取り扱いがありません」と
        弾かれてしまうため。
        """
        base = slot.default_product or slot.reference_product
        if not base:
            return []

        col = self.collection_of(base)
        codes = list(col.product_codes) if col else []
        if not codes:
            # カテゴリが分からないときは、せめてサイズ違いを出す
            codes = [p.code for p in self.size_variants(base)] or [base]

        out = []
        for code in codes:
            p = self.products.get(str(code))
            if p is None:
                continue
            if minutes is not None and not p.is_orderable_at(minutes):
                continue
            out.append(p)

        # 既定の商品は、時間帯の判定に関わらず必ず残す
        # （マクドナルド側が既定として指定しているものなので）
        if base not in [p.code for p in out]:
            base_p = self.products.get(str(base))
            if base_p is not None and (
                minutes is None or base_p.is_orderable_at(minutes)
            ):
                out.insert(0, base_p)
        return out

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


def _parse_slots(raw: dict, names: dict[str, str] | None = None) -> list[Slot]:
    names = names or {}
    slots: list[Slot] = []
    for kind in ("composition", "choices", "canAdds"):
        for c in raw.get(kind) or []:
            if not isinstance(c, dict):
                continue
            code = str(c.get("productCode") or "")
            slots.append(
                Slot(
                    kind=kind,
                    code=code,
                    name=names.get(code, ""),
                    default_product=str(c.get("defaultProduct") or ""),
                    reference_product=str(c.get("referenceProduct") or ""),
                    min_quantity=int(c.get("minQuantity") or 0),
                    max_quantity=int(c.get("maxQuantity") or 1),
                    default_quantity=int(
                        c.get("defaultQuantity")
                        if c.get("defaultQuantity") is not None else 1
                    ),
                    extra_price=_price_of(c, "prePrice"),
                    cost_inclusive=bool(c.get("costInclusive", True)),
                )
            )
    return slots


def _limited_ability(menu: dict, date_key: str | None = None) -> dict:
    """
    limitedAbility から、その日の ability を取り出す。

    ⚠️ このデータには `0008-09-30` のような**壊れた日付キー**が
       混ざっており、しかもJSONの先頭に来る。
       素直に最初の要素を使うと、その日の本当の時間帯を取り逃す。
       実在する日付（2000年以降）だけを見て、今日のものを選ぶ。
    """
    limited = menu.get("limitedAbility") or {}
    valid = {k: v for k, v in limited.items() if k[:4].isdigit() and int(k[:4]) >= 2000}
    if not valid:
        return {}
    key = date_key or config.today_jst()
    day = valid.get(key)
    if day is None:
        day = valid[sorted(valid)[0]]
    return (day or {}).get("ability") or {}


def _windows_of(ability: dict, code: str) -> list[dict] | None:
    """
    注文できる時間帯。

    登録が無ければ None（分からない）、
    登録はあるが空なら [] （扱っていない）を返す。
    この2つを混同すると、注文できない商品を表示してしまう。
    """
    entry = ability.get(code)
    if entry is None:
        return None
    return [
        {"start": int(w["start"]), "end": int(w["end"])}
        for w in (entry.get("checkoutable") or [])
        if "start" in w and "end" in w
    ]


def parse_size_groups(menu: dict) -> dict[str, str]:
    """
    サイズ違いの対応表を作る。

    sizeVariants は「コカ・コーラ → S:3110 / M:3120 / L:3150」のような形。
    どのサイズからも同じ代表コードに辿れるようにしておく。
    """
    out: dict[str, str] = {}
    for key, v in (menu.get("sizeVariants") or {}).items():
        if not isinstance(v, dict):
            continue
        group = str(v.get("productCode") or key)
        for size in v.get("sizes") or []:
            code = str((size or {}).get("productCode") or "")
            if code:
                out[code] = group
        out.setdefault(str(key), group)
    return out


def parse_menu(store_id: str, menu: dict) -> ParsedMenu:
    names = harvest_names(menu)
    names.update(build_size_names(menu))   # サイズ違いは親名＋サイズで上書き

    ability = _limited_ability(menu)
    size_groups = parse_size_groups(menu)

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
            slots=_parse_slots(raw, names),
            time_windows=_windows_of(ability, code),
            size_group=size_groups.get(code, ""),
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
    return ParsedMenu(
        store_id=store_id, products=products, collections=collections,
        size_groups=size_groups,
    )


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


def customization_note(menu: "ParsedMenu", item) -> str:
    """
    「（ピクルス抜き）」のような補足を作る。調整が無ければ空。

    具材は products に載っていないため、商品の構成（Slot）に
    持たせておいた名前を使う。
    """
    product = menu.products.get(str(getattr(item, "product_code", "")))
    if product is None:
        return ""
    by_code = {s.code: s for s in product.slots_of("composition")}
    removed, increased = [], []
    for comp in getattr(item, "components", None) or []:
        slot = by_code.get(str(getattr(comp, "product_code", "")))
        if slot is None or not slot.name:
            continue
        qty = int(getattr(comp, "quantity", slot.default_quantity))
        if qty < slot.default_quantity:
            removed.append(slot.name)
        elif qty > slot.default_quantity:
            increased.append(f"{slot.name}×{qty}")
    parts = []
    if removed:
        parts.append("・".join(removed) + "抜き")
    if increased:
        parts.append("・".join(increased))
    return f"（{' / '.join(parts)}）" if parts else ""


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
