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


def _ja(node: Any, key: str) -> str:
    """{"en": ..., "ja": ...} から日本語を取り出す。無ければ空。"""
    v = node.get(key)
    if isinstance(v, dict):
        return str(v.get("ja") or "").strip()
    return ""


def harvest_display(menu: dict) -> dict[str, Display]:
    """
    `groupMenu.products` から、見せるための情報を集める。

    実物のアプリが出しているのと同じ説明文・画像・注意書きを
    ここで拾う。注文の組み立てには使わない。

    ⚠️ `showLimitedTimeIcon` は全商品が true になっていて役に立たない。
       期間限定の判定には `timeLimitedOffer.valid` を使う。
    """
    out: dict[str, Display] = {}
    products = ((menu.get("groupMenu") or {}).get("products") or {})
    if not isinstance(products, dict):
        return out

    for code, raw in products.items():
        if not isinstance(raw, dict):
            continue
        image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
        offer = raw.get("timeLimitedOffer") if isinstance(raw.get("timeLimitedOffer"), dict) else {}
        durl = raw.get("detailUrl") if isinstance(raw.get("detailUrl"), dict) else {}

        tname = raw.get("tName") if isinstance(raw.get("tName"), dict) else {}
        out[str(code)] = Display(
            name_en=str(tname.get("en") or "").strip(),
            subtitle=_ja(raw, "tSubtitle"),
            description=_ja(raw, "tDescription"),
            # small と middle と large は同じURLのことが多い。
            # 大きいものから順に、あるものを使う。
            image_url=str(
                image.get("large") or image.get("middle") or image.get("small") or ""
            ),
            notes=_ja(raw, "tNotes"),
            precautions=_ja(raw, "tPrecautions"),
            kind=str(raw.get("kind") or ""),
            set_type=str(raw.get("setType") or ""),
            limited=bool(offer.get("valid")),
            limited_from=str(offer.get("startedAt") or ""),
            limited_to=str(offer.get("endedAt") or ""),
            detail_url=_ja(durl, "tUrl") if isinstance(durl.get("tUrl"), dict) else "",
            msc=bool(raw.get("isMscCertified")),
            rainforest=bool(raw.get("isRainforestAllianceCertified")),
        )
    return out


def harvest_extras(menu: dict, products: dict, ability: dict) -> dict[str, Extra]:
    """
    選択肢専用の商品を集める。

    `groupMenu` にあって `products` に無く、値段が 0 のものが該当する。
    実データでは 6xxx 番台（ソース・ドレッシング・おもちゃ）。
    """
    out: dict[str, Extra] = {}
    gm = ((menu.get("groupMenu") or {}).get("products") or {})
    if not isinstance(gm, dict):
        return out
    for code, raw in gm.items():
        code = str(code)
        if code in products or not isinstance(raw, dict):
            continue
        if str(raw.get("price", "")) not in ("0", ""):
            continue            # 値段が付いているものは単品で買える商品
        # ⚠️ 材料（99901032 ピクルス、99903001 氷など）は composition 側で
        #    扱うもので、選択枠の候補ではない。混ぜるとソースの枠に
        #    「氷」が並ぶ。
        if code.startswith("999"):
            continue
        name = ((raw.get("tName") or {}).get("ja") or "").strip()
        # ⚠️ 【CLR】で始まるものは社内向けの符号。利用者には出さない。
        if not name or name.startswith("【"):
            continue
        out[code] = Extra(
            code=code, name=name,
            kind=str(raw.get("kind") or ""),
            day_part=str(raw.get("dayPart") or ""),
            time_windows=_windows_of(ability, code),
        )
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
class Display:
    """
    実物のアプリと同じ情報を出すための、見せるためだけの値。

    注文の組み立てには一切使わない。`groupMenu.products` から拾う
    （`products` の側には名前すら入っていない / docs/08）。
    """
    name_en: str = ""           # 英語名（"Big Mac" などで検索できるように）
    subtitle: str = ""          # 商品名の補足
    description: str = ""       # 商品説明
    image_url: str = ""         # 商品画像
    notes: str = ""             # ※一部店舗及びデリバリーでは価格が異なります。
    precautions: str = ""       # 注意書き
    kind: str = ""              # KIND_BURGER / KIND_SIDE / KIND_DRINK / KIND_MCCAFE
    set_type: str = ""          # SET_TYPE_VALUE_SET / SET_TYPE_HAPPY_SET ...
    limited_from: str = ""      # 期間限定の開始（ISO8601）
    limited_to: str = ""        # 〃 終了（空なら未定）
    limited: bool = False       # 期間限定かどうか
    detail_url: str = ""        # 公式のアレルギー・栄養情報ページ
    msc: bool = False           # MSC認証（白身魚）
    rainforest: bool = False    # レインフォレスト・アライアンス認証（コーヒー）

    @property
    def has_any(self) -> bool:
        return bool(self.description or self.image_url or self.subtitle)

    def to_dict(self) -> dict:
        """空の値は捨てる。1店舗ぶんで数百件あるので、持ち物は軽くする。"""
        return {k: v for k, v in self.__dict__.items() if v}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Display":
        if not d:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


KIND_LABEL = {
    "KIND_BURGER": "バーガー",
    "KIND_SIDE": "サイドメニュー",
    "KIND_DRINK": "ドリンク",
    "KIND_MCCAFE": "McCafé",
}

SET_TYPE_LABEL = {
    "SET_TYPE_VALUE_SET": "バリューセット",
    "SET_TYPE_HAPPY_SET": "ハッピーセット",
    "SET_TYPE_COMBINATION": "コンビ",
    "SET_TYPE_VALUE_LUNCH": "バリューランチ",
}


def _dayparts_fit(parent: str, child: str) -> bool:
    """セットの時間帯区分と、中身の時間帯区分が噛み合うか。

    ⚠️ `BREAKFAST_DAY_MENU` は朝にも昼にも出せる。空欄も同じ扱い
       （ナゲット5ピースは空欄だが、昼のセットで選べる）。
       **分からないものを外さない**こと。外すと候補が空になり、
       その商品が一切注文できなくなる。
    """
    a, b = (parent or "").upper(), (child or "").upper()
    if not a or not b:
        return True
    if a == b:
        return True
    # どちらかが「朝も昼も」なら通す
    if "BREAKFAST" in a and "DAY" in a:
        return True
    if "BREAKFAST" in b and "DAY" in b:
        return True
    # 朝だけ ↔ 昼だけ は噛み合わない
    return not (
        ("BREAKFAST" in a and "DAY" in b) or ("DAY" in a and "BREAKFAST" in b)
    )


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

    @property
    def need(self) -> int:
        """
        この枠に入れる個数。

        ⚠️ ほとんどの枠は1個だが、チキンマックナゲット15ピースの
           ソース枠（7251）は **3個必須**（min=max=default=3）。
           1個しか送らないとマクドナルドに断られる。
        """
        return max(self.default_quantity or 1, self.min_quantity or 0, 1)

    @property
    def multi(self) -> bool:
        """2個以上入れる枠か（ナゲット15ピースのソースなど）。"""
        return self.need > 1

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
    delivery_pre_price: int = 0        # セットの宅配の表示価格
    slots: list[Slot] = field(default_factory=list)
    # 注文できる時間帯。
    #   None … カタログに登録が無い（分からないので止めない）
    #   []   … 登録はあるが空＝**この店舗では扱っていない**
    #   [..] … その時間帯だけ注文できる
    time_windows: list[dict] | None = None
    size_group: str = ""      # サイズ違いをまとめる代表コード
    display: Display = field(default_factory=Display)   # 見せるためだけの情報

    def price_for(self, pickup_method: str) -> int:
        """
        受取方法に応じた価格。

        セット商品は prePrice（構成込みの表示価格）を使う。

        ⚠️ その受取方法の価格が入っていなければ、他の価格で代える。
           実データ（247商品）では全部そろっていたが、欠けたときに
           **¥0 と出すのが一番まずい**。ただより安いものは無い。
        """
        code = PRICE_CODE.get(pickup_method, "TAKEOUT")
        if self.product_class == "VALUE_MEAL":
            # ⚠️ 宅配はセットでも値段が違う（ハンバーガー ハッピーセットは
            #    店内/持ち帰り540円・宅配630円）。pre_price を一律で返すと
            #    宅配のときに安く請求してしまう。
            if code == "OTHER" and self.delivery_pre_price:
                return self.delivery_pre_price
            if self.pre_price:
                return self.pre_price
        by_code = {
            "EATIN": self.price_eatin,
            "TAKEOUT": self.price_takeout,
            "OTHER": self.price_other,
        }
        for key in (code, "TAKEOUT", "EATIN", "OTHER"):
            if by_code.get(key):
                return by_code[key]
        return self.pre_price or 0

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
class Extra:
    """
    選択肢専用の商品。

    ⚠️ `products` には載らず、`groupMenu` と `limitedAbility` にだけ
       現れる商品がある（6xxx番台）。ナゲットに付けるソース、
       サイドサラダのドレッシング、ハッピーセットのおもちゃなど。

       同じ名前で `products` にもある場合があるが**別物**。
         5502 バーベキューソース price=50  … 単品で買うソース
         6048 バーベキューソース price=0   … ナゲットに付ける無料のソース
       取り違えると、無料のはずのソースを50円で注文することになる。
    """
    code: str
    name: str
    kind: str = ""              # KIND_SIDE / KIND_NONE など
    day_part: str = ""
    time_windows: list[dict] | None = None

    @property
    def group(self) -> tuple[str, str]:
        """同じ枠に入る仲間かどうかの目印。"""
        return (self.kind, self.day_part)

    def is_orderable_at(self, minutes: int) -> bool:
        if self.time_windows is None:
            return True
        if not self.time_windows:
            return False
        return any(w["start"] <= minutes < w["end"] for w in self.time_windows)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "kind": self.kind,
            "day_part": self.day_part, "time_windows": self.time_windows,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Extra":
        return cls(
            code=str(d.get("code", "")), name=str(d.get("name", "")),
            kind=str(d.get("kind", "")), day_part=str(d.get("day_part", "")),
            time_windows=d.get("time_windows"),
        )


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
    # products に載っていない商品コード → 名前（groupMenu 由来）。
    # 選択枠の参照を名前で読み替えるのに使う。
    aliases: dict[str, str] = field(default_factory=dict)
    # 選択肢専用の商品（ソース・ドレッシング・おもちゃ）
    extras: dict[str, Extra] = field(default_factory=dict)
    # 従業員向けの食事（エンプロイミール）。利用者には売らない。
    staff_only: frozenset[str] = frozenset()
    _slot_ref_cache: dict[str, str] | None = field(default=None, repr=False)

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

    # 選択枠の候補を絞る価格帯。参照商品の値段の何倍までを同じ枠の
    # 仲間とみなすか。
    # ⚠️ 実データで全枠を確認して決めた値（docs/05 §5.10）。
    #    ナゲットのソース枠（参照50円）は3種のソースだけが残り、
    #    セットのサイド枠（参照370円）からは50円のソースだけが外れ、
    #    ドリンク枠（参照310円）は140円のコーヒーを含め24件すべて残る。
    PRICE_BAND = (0.3, 3.0)

    def slot_reference(self, slot: Slot) -> str:
        """
        その枠に入る商品の代表。見つからなければ空文字。

        ⚠️ 3段階で探す。カタログがそのままでは引けない形をしている。
          1. 枠が持つ既定・参照商品
          2. **同じ枠コードを使う他の商品**の参照
             （ナゲット5ピースの枠7251には参照が無いが、
               15ピースの同じ枠には 6048 が入っている）
          3. 参照が選択肢専用の商品（6xxx）ならそのまま使う
          4. それでも分からない枠は、手がかりの表から引く
             （ハッピーセットのドリンク枠は全商品で参照が空。
               services/mcd/slot_hints.py を参照）

        ⚠️ 同じ名前で products にもある商品に読み替えてはいけない。
             5502 バーベキューソース price=50 … 単品で買うソース
             6048 バーベキューソース price=0  … ナゲットに付ける無料のソース
           別物なので、取り違えると無料のソースを50円で注文してしまう。
        """
        base = slot.default_product or slot.reference_product
        if not base:
            base = self._slot_refs().get(slot.code, "")
        if not base:
            # カタログのどこにも参照が無い枠（ハッピーセットのドリンク）
            from services.mcd import slot_hints
            base = slot_hints.reference_for(slot.code)
        if not base:
            return ""
        if base in self.products or base in self.extras:
            return base
        return ""

    def _slot_refs(self) -> dict[str, str]:
        """枠コード → 参照商品。全商品を走査して1度だけ作る。"""
        if self._slot_ref_cache is None:
            found: dict[str, str] = {}
            for prod in self.products.values():
                for sl in prod.slots_of("choices"):
                    ref = sl.default_product or sl.reference_product
                    if ref and sl.code not in found:
                        found[sl.code] = ref
            self._slot_ref_cache = found
        return self._slot_ref_cache

    def _display_name(self, code: str) -> str:
        """products に無い商品の名前（groupMenu 由来）。"""
        return self.aliases.get(str(code), "")

    def bridge_for(self, slot: Slot) -> str:
        """
        枠と商品の間に入る中間ノード。無ければ空文字。

        ⚠️ ドリンク枠には中間ノードが要る。実物の注文コードで確認した。

            通常セット   9997918 → 9997914 → 3120（コカ・コーラM）
            朝マック     9997925 → 9997922 → 3170（スプライトM）
            サイド枠     9987010 → 5010（ハッシュポテト）   ※中間なし

        ⚠️ **値に規則は無いので、推測で埋めないこと。**
           一度「同じカテゴリの枠は同じ中間ノードを使う」とみなして
           9997925 に 9997914 を当てたが、実物は 9997922 だった。
           知らない枠は、実物の注文コードを貼ってもらって覚える
           （slot_bridge.learn_from_order）。
        """
        from services.mcd import slot_bridge

        return slot_bridge.bridge_for(slot.code)

    def bridge_gaps(self) -> list[tuple[str, list["Product"]]]:
        """中間ノードが分かっていない枠と、それが止めている商品。

        ⚠️ カタログは「参照しているが定義していないコード」を持つ。
           枠コードは choices に出てくるが products には載っておらず、
           その枠に必要な中間ノードはカタログのどこにも書かれていない。
           書かれていないので**計算では出せない**。実際に通った注文
           コードを貼ってもらう以外に手が無い（docs/04）。

        ⚠️ 「中間が要る枠」と「中間が要らない枠」は見分けられない。
           サイド枠は中間なしで実際に通っている。だからここに出るのは
           「中間が要るかどうかも分からない枠」であって、全部が
           壊れているとは限らない。**疑わしい枠の一覧**として読む。

        返すのは (枠のコード, その枠を持つ商品) の一覧。多い順。
        """
        from services.mcd import slot_bridge

        gaps: dict[str, list[Product]] = {}
        for product in self.products.values():
            for slot in product.slots_of("choices"):
                # 中間あり／中間不要と分かっている枠は、調べる必要が無い
                if slot_bridge.status_of(slot.code) != "unknown":
                    continue
                gaps.setdefault(slot.code, []).append(product)
        return sorted(gaps.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    def products_using_slots(self, slot_codes: Iterable[str]) -> list["Product"]:
        """その枠を持つ商品。覚えた直後に「何が直ったか」を見せるため。"""
        want = {str(c) for c in slot_codes}
        out = []
        for product in self.products.values():
            if any(s.code in want for s in product.slots_of("choices")):
                out.append(product)
        return sorted(out, key=lambda p: p.code)

    def _pool_for(self, base: str) -> list[str]:
        """
        候補の母集団。

        ⚠️ サイズ違いしか載っていないカテゴリがある。
           ポテトSは単体ではどのカテゴリにも入っていないが、
           同じサイズ群のポテトMはサイドメニューに入っている。
           自分で引けなければ、サイズ違いの仲間から引く。
        """
        col = self.collection_of(base)
        if col:
            return list(col.product_codes)
        for q in self.size_variants(base):
            col = self.collection_of(q.code)
            if col:
                return list(col.product_codes)
        return [q.code for q in self.size_variants(base)] or [base]

    def _extra_candidates(
        self, slot: Slot, base: str, parent: Product | None
    ) -> list[Extra] | None:
        """
        選択肢専用の商品から候補を出す。該当しなければ None。

        同じ (kind, dayPart) のものを「同じ枠の仲間」とみなす。
        実データで、ソース3種・おもちゃ5種がそれぞれ1グループになる。
        """
        if base and base in self.extras:
            g = self.extras[base].group
            return sorted(
                (e for e in self.extras.values() if e.group == g),
                key=lambda e: e.code,
            )
        if base or parent is None:
            return None

        # ---- 参照がどこにも無い枠 ----
        # ⚠️ 同じ商品の**他の枠**がすでに使っているグループは外す。
        #    ハッピーセットにはおもちゃの枠とは別に参照の無い枠があり、
        #    そのままだとおもちゃが両方の枠に出てしまう。
        taken = set()
        for other in parent.slots_of("choices"):
            if other.code == slot.code:
                continue
            ref = self.slot_reference(other)
            if ref and ref in self.extras:
                taken.add(self.extras[ref].group)
        # ⚠️ シャカチキの味、サイドサラダのドレッシングがこれ。
        #    親商品との結び付きを順に探す。
        claimed = {
            e.code for e in self.extras.values()
            for q in self.products.values()
            if q.name and q.name in e.name and q.code != parent.code
        }
        # ⚠️ **他の枠がはっきり参照しているグループは候補にしない。**
        #    おもちゃは枠9997008が 6118 を参照しているし、ナゲットの
        #    ソースは枠7251が 6048 を参照している。つまり「行き先の
        #    決まっているグループ」であって、参照の無い枠の中身ではない。
        #    これを外さないと、マカロンのボックスセットやエンプロイミールの
        #    枠に **おもちゃ・えほん・プラレール** が並んでしまい、
        #    選んで注文してもマクドナルドに断られる。
        spoken = {
            self.extras[ref].group
            for ref in self._slot_refs().values()
            if ref in self.extras
        }
        free = [
            e for e in self.extras.values()
            if e.code not in claimed
            and e.group not in taken
            and e.group not in spoken
        ]

        # ① 名前に親商品の名前が入っている（「シャカチキ チェダーチーズ味…」）
        named = [
            e for e in self.extras.values()
            if parent.name and parent.name in e.name and e.group not in taken
        ]
        if named:
            return sorted(named, key=lambda e: e.code)

        # ② 残ったものが **ひとつのグループに絞れたときだけ** 使う。
        #    絞れないなら「何の枠か分からない」ので候補を出さない。
        #    分からないまま並べると、選べないものを選ばせてしまう。
        rest = [e for e in free if e.kind == parent.display.kind]
        if not rest:
            return None
        if len({e.group for e in rest}) > 1:
            return None                     # 複数のグループ → 判断できない
        return sorted(rest, key=lambda e: e.code)

    def choice_candidates(
        self, slot: Slot, minutes: int | None = None,
        parent: Product | None = None,
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
        base = self.slot_reference(slot)

        # ---- 選択肢専用の商品（ソース・ドレッシング・おもちゃ）----
        picks = self._extra_candidates(slot, base, parent)
        if picks is not None:
            return [
                e for e in picks
                if minutes is None or e.is_orderable_at(minutes)
            ]

        if not base:
            return []

        codes = self._pool_for(base)
        # ⚠️ 母集団には、その枠に入れられないものが混ざる。
        #    サイドメニューには50円のソース（ナゲットの付属）が入っており、
        #    セットのサイドとしては選べない。
        #    参照商品とかけ離れた値段のものは、同じ枠の仲間ではない。
        lo, hi = self.PRICE_BAND
        ref_price = self.products[base].price_takeout or 0
        if ref_price > 0:
            codes = [
                c for c in codes
                if (q := self.products.get(str(c))) is None
                or lo * ref_price <= (q.price_takeout or 0) <= hi * ref_price
            ]

        from services.mcd import slot_rules

        out = []
        for code in codes:
            p = self.products.get(str(code))
            if p is None:
                continue
            if minutes is not None and not p.is_orderable_at(minutes):
                continue
            # ⚠️ **セットをセットの中身にできない。**
            #    母集団は「参照商品と同じカテゴリ全部」なので、
            #    サイドメニューの枠に ポテナゲ大（VALUE_MEAL・¥600）や
            #    食べくらべポテナゲ特大（¥990）まで並んでいた。
            #    実機（チーズチーズ倍月見 セット）で確かめたところ、
            #    選べるのは **ポテト / サイドサラダ / ナゲット5ピース /
            #    えだまめコーン の4つだけ**で、セットは1つも出ない。
            if p.product_class == "VALUE_MEAL":
                continue
            # ⚠️ 朝の商品を昼のセットに入れられない（逆も同じ）。
            #    ハッシュポテト（BREAKFAST_MENU・¥190）が
            #    昼のセットの候補に出ていたが、実機では出ない。
            if parent is not None and not _dayparts_fit(parent.day_part, p.day_part):
                continue
            # 一度マクドナルドに断られた組み合わせは、もう出さない
            if not slot_rules.allowed(self.store_id, slot.code, p.code):
                continue
            out.append(p)

        # 代表の商品（referenceProduct）の扱い。
        #
        # ⚠️ **断られた記録があるなら戻さない。**
        #    代表だからといって必ず選べるとは限らない。実際、朝マックの
        #    セット（9030）のドリンク枠 9997925 は代表が「コカ・コーラ M」
        #    だが、マクドナルドは
        #        9030 > 9997925 > 3120 product not found
        #    と断ってくる。以前はここで無条件に押し戻していたため、
        #    学習しても候補の**先頭**に出続け、選んだ人が必ず失敗していた。
        #
        # ⚠️ ただし**候補が空になるのはもっと悪い**。
        #    0件だと枠を埋められず、その商品を一切注文できなくなる。
        #    全部断られてしまった場合だけは、代表を戻して望みをつなぐ。
        base_p = self.products.get(str(base))
        if base_p is not None and base not in [p.code for p in out]:
            time_ok = minutes is None or base_p.is_orderable_at(minutes)
            if time_ok and slot_rules.allowed(self.store_id, slot.code, base):
                out.insert(0, base_p)
            elif not out and time_ok:
                # 最後の手段。断られた記録があっても、空よりはまし。
                log.info(
                    "枠 %s は候補が全部断られています。"
                    "代表商品（%s）だけ残します。", slot.code, base,
                )
                out.insert(0, base_p)

        # 確かなものを先に並べる。
        # ⚠️ カタログに候補一覧が無い以上、確実に選べると分かっているのは
        #    参照商品とそのサイズ違いだけ。利用者が上から選ぶほど
        #    通りやすくなるようにしておく。
        # ⚠️ 断られた記録があるものを「確実」に入れない。
        #    入れると、通らないと分かっているものが先頭に並ぶ。
        safe = {
            c for c in ({str(base)} | {q.code for q in self.size_variants(base)})
            if slot_rules.allowed(self.store_id, slot.code, c)
        }
        # 手がかりの表で「確実に選べる」と分かっているものは、さらに先へ。
        # ハッピーセットのドリンクは枠の中身がカタログに無いため、
        # 上から選ぶほど通りやすい並びにしておく。
        from services.mcd import slot_hints
        prefer = slot_hints.preferred(slot.code)
        rank = {code: i for i, code in enumerate(prefer)}

        # ⚠️ ドリンクの枠は20種類を超えることがある。名前順に並べると
        #    コーラ・スプライト・ファンタが離れ、選ぶのに何度も
        #    上下に動かすことになる（一度に5〜6件しか見えない）。
        #    同じ仲間が隣り合うように、種類でまとめてから並べる。
        from services.mcd import drinks

        grouped = drinks.is_drink_slot(out)

        def sort_key(q):
            # ⚠️ 並べる順の優先度を間違えないこと。
            #    ① 手がかりの表（実物で通ると分かっているもの）
            #    ② 種類（炭酸・ジュース…）でまとめる
            #    ③ 代表商品とそのサイズ違い
            #    ④ 名前
            #    ①を②より後ろにすると、ハッピーセットのように
            #    カタログに候補が載っていない枠で、通らないものが
            #    先頭に来てしまう。**通りやすさが見やすさより先**。
            hint = rank.get(q.code, len(rank))
            group = drinks.group_of(q.name)[0] if grouped else 0
            return (hint, group, q.code not in safe, q.name)

        out.sort(key=sort_key)
        return out

    def nested_choices(self, product: Product) -> list[tuple[str, Slot]]:
        """
        構成品が持っている選択枠。(構成品のコード, 枠) の一覧。

        ⚠️ ポテナゲのように、セットの**構成品**（ナゲット）が
           さらに選択枠（ソース）を持っていることがある。
           実データで7商品。これを送らないと、必須の枠が空のまま
           注文することになり、マクドナルドに断られる。

           「食べくらべポテナゲ特大が注文できない」の原因がこれ。
        """
        out: list[tuple[str, Slot]] = []
        for comp in product.slots_of("composition"):
            inner = self.products.get(str(comp.code))
            if inner is None:
                continue
            for slot in inner.slots_of("choices"):
                if slot.min_quantity >= 1:
                    out.append((str(comp.code), slot))
        return out

    def unfillable_slots(self, product: Product, minutes: int | None = None) -> list[Slot]:
        """
        埋めようがない選択枠。

        ⚠️ ハッピーセットのように、参照商品がカタログの products に
           載っていない枠や、参照が空の枠がある（実データで9商品）。
           候補を1つも出せないのに必須なので、そのまま注文を送ると
           マクドナルドから「お取り扱いがありません」で断られる。
           利用者には理由が分からないので、**最初から出さない**。
        """
        out = []
        slots = list(product.slots_of("choices"))
        slots += [sl for _, sl in self.nested_choices(product)]
        for slot in slots:
            if slot.min_quantity < 1:
                continue                    # 入れなくてよい枠
            if slot.default_product:
                continue                    # 既定があるので埋まる
            if not self.choice_candidates(slot, minutes, parent=product):
                out.append(slot)
        return out

    def orderable(self, product: Product, minutes: int | None = None) -> bool:
        """注文として成立させられるか。時間帯と、枠を埋められるかの両方。"""
        # ⚠️ エンプロイミール（従業員向けの食事）は利用者には売らない。
        #    値段が従業員価格で、一般の注文として送っても断られる。
        if product.code in self.staff_only:
            return False
        if minutes is not None and not product.is_orderable_at(minutes):
            return False
        return not self.unfillable_slots(product, minutes)

    def visible_products(self, collection_id: str, minutes: int | None = None) -> list[Product]:
        col = next((c for c in self.collections if c.id == collection_id), None)
        if not col:
            return []
        out = []
        for code in col.product_codes:
            p = self.products.get(str(code))
            # ⚠️ 時間帯だけでなく「枠を埋められるか」も見る。
            #    埋められないセットを出すと、選び終えてから断られる。
            if p and self.orderable(p, minutes):
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
    display = harvest_display(menu)

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
            delivery_pre_price=_price_of(raw, "deliveryPrePrice"),
            slots=_parse_slots(raw, names),
            time_windows=_windows_of(ability, code),
            size_group=size_groups.get(code, ""),
            display=display.get(code, Display()),
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
        # products に無い商品の名前も持っておく（枠の参照を引き直すため）
        aliases={c: n for c, n in names.items() if c not in products},
        extras=harvest_extras(menu, products, ability),
        staff_only=frozenset(
            str(x) for x in
            ((menu.get("employeeMealCollection") or {}).get("productCodes") or [])
        ),
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
