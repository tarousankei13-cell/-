"""
マクドナルド モバイルオーダーの protobuf 層

HATTIMCD を参考にしているが、解析で判明した不具合を修正した独自実装。
スキーマの根拠は docs/07 §4（公式フロントエンドの protobuf 定義）。

HATTIMCD から直した点:
  1. 受取方法の判定が誤っていた（field 2 は takeOut であって デリバリー ではない）
  2. StoreOrder が受取方法を常に空で送っていた（hex の内容が無視される）
  3. 8.2.2 を shortOrderCode としていたが、実際は productCode
  4. 複数商品の注文で金額と商品が壊れる（上書きされる）

CreateOrderInput（= hex の中身）:
    1  storeId              string
    2  deliveryMethod       DeliveryMethod
    3  createPaymentMethod  CreatePaymentMethod
    7  createDeliveryMethod CreateDeliveryMethod  ← 受取方法
    8  order                { 2: OrderItem[] }
    12 userPosIdToken       string

OrderItem:
    1  (存在フラグ)  varint
    2  productCode   string
    3  quantity      varint
    4  amount        varint   ※トップレベルのみ
    5  component     OrderItem[]  （再帰）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import config

# ============================================================
#  低レベル protobuf
# ============================================================


def varint_encode(n: int) -> bytes:
    if n < 0:
        raise ValueError("負の数は varint にできません")
    buf = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        buf.append(bits | 0x80 if n else bits)
        if not n:
            return bytes(buf)


def varint_decode(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    start = pos
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, pos
        if shift > 63:
            raise ProtocolError(f"varint が長すぎます (位置 {start})")
    raise ProtocolError(f"varint が途中で終わっています (位置 {start})")


def pb_str(field_no: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return varint_encode((field_no << 3) | 2) + varint_encode(len(b)) + b


def pb_msg(field_no: int, data: bytes) -> bytes:
    return varint_encode((field_no << 3) | 2) + varint_encode(len(data)) + data


def pb_int(field_no: int, n: int) -> bytes:
    return varint_encode((field_no << 3) | 0) + varint_encode(n)


class ProtocolError(Exception):
    pass


def proto_parse(data: bytes, *, strict: bool = True) -> dict[int, list[Any]]:
    """
    protobuf をフィールド番号 → 値のリストに分解する。

    strict=True では、長さが合わない・末尾が欠けている場合に例外を出す。
    壊れた hex をそのまま決済に流さないための最初の防波堤。
    """
    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        tag, pos = varint_decode(data, pos)
        fn, wt = tag >> 3, tag & 0x7
        if fn == 0:
            raise ProtocolError(f"フィールド番号0は不正です (位置 {pos})")
        if wt == 0:
            v, pos = varint_decode(data, pos)
            fields.setdefault(fn, []).append(v)
        elif wt == 2:
            length, pos = varint_decode(data, pos)
            if pos + length > len(data):
                raise ProtocolError(
                    f"フィールド {fn} の長さ {length} に対し、残りが "
                    f"{len(data) - pos} バイトしかありません"
                )
            fields.setdefault(fn, []).append(data[pos:pos + length])
            pos += length
        elif wt == 5:
            if pos + 4 > len(data):
                raise ProtocolError(f"フィールド {fn}: 32bit値が欠けています")
            fields.setdefault(fn, []).append(data[pos:pos + 4])
            pos += 4
        elif wt == 1:
            if pos + 8 > len(data):
                raise ProtocolError(f"フィールド {fn}: 64bit値が欠けています")
            fields.setdefault(fn, []).append(data[pos:pos + 8])
            pos += 8
        else:
            if strict:
                raise ProtocolError(f"未対応のワイヤ型 {wt} (フィールド {fn})")
            break
    return fields


def _as_str(v: Any) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.hex()
    return str(v)


# ============================================================
#  受取方法（docs/07 §4.2 / CreateDeliveryMethod の oneof）
# ============================================================

PICKUP_FIELD = {
    "eatIn": 1,            # 店内（カウンター受取）
    "takeOut": 2,          # テイクアウト
    "tableDelivery": 3,    # 店内（席まで）
    "curbsidePickUp": 4,   # 駐車場で受け取る
    "driveThru": 5,        # ドライブスルー
    "addressDelivery": 6,  # デリバリー
}
FIELD_PICKUP = {v: k for k, v in PICKUP_FIELD.items()}

# 受取方法の表示名。実物のモバイルオーダーと同じ言い回しにしてある。
# ⚠️ 2か所に書くと必ずずれるので、config.PICKUP_METHODS から引く。
PICKUP_LABEL = {k: v["label"] for k, v in config.PICKUP_METHODS.items()}

# 店舗JSONの deliveryMethod キーとの対応（docs/08 §4）
STORE_DELIVERY_KEY = {
    "eatIn": "eatIn",
    "takeOut": "takeOut",
    "tableDelivery": "tableDelivery",
    "curbsidePickUp": "curbsidePickUp",
    "driveThru": "driveThru",
    "addressDelivery": "mcDelivery",
}


def build_pickup_payload(method: str, *, number: int | None = None) -> bytes:
    """
    createDeliveryMethod（field 7）の中身を組み立てる。

    eatIn / takeOut は空メッセージ。
    tableDelivery / curbsidePickUp はテーブル番号・駐車場番号が要る。
    """
    if method not in PICKUP_FIELD:
        raise ProtocolError(f"未知の受取方法です: {method}")
    fn = PICKUP_FIELD[method]
    if method in ("tableDelivery", "curbsidePickUp"):
        if number is None:
            raise ProtocolError(f"{PICKUP_LABEL[method]} には番号の指定が必要です")
        return pb_msg(fn, pb_int(1, number))
    if method == "addressDelivery":
        raise ProtocolError("デリバリーは住所の指定が必要なため、このBOTでは扱えません")
    return pb_msg(fn, b"")


def detect_pickup_method(top: dict[int, list[Any]]) -> str | None:
    """
    hex から受取方法を読み取る。

    ⚠️ HATTIMCD は field 2 を「デリバリー」と誤判定していた（正しくは takeOut）。
    """
    raw_list = top.get(7, [])
    if not raw_list or not isinstance(raw_list[0], bytes) or not raw_list[0]:
        return None  # 未指定（「テイクアウト」ではない）
    inner = proto_parse(raw_list[0])
    for fn, name in FIELD_PICKUP.items():
        if fn in inner:
            return name
    return None


# ============================================================
#  注文内容
# ============================================================


@dataclass
class OrderItem:
    """注文の1品。セットの構成品も同じ構造で入れ子になる。"""
    product_code: str
    quantity: int = 1
    amount: int = 0
    has_flag: bool = True
    components: list["OrderItem"] = field(default_factory=list)

    def walk(self):
        yield self
        for c in self.components:
            yield from c.walk()

    def to_dict(self) -> dict:
        return {
            "product_code": self.product_code,
            "quantity": self.quantity,
            "amount": self.amount,
            "has_flag": self.has_flag,
            "components": [c.to_dict() for c in self.components],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OrderItem":
        return cls(
            product_code=d["product_code"],
            quantity=d.get("quantity", 1),
            amount=d.get("amount", 0),
            has_flag=d.get("has_flag", True),
            components=[cls.from_dict(c) for c in d.get("components", [])],
        )


@dataclass
class DecodedOrder:
    store_id: str
    pickup_method: str | None
    items: list[OrderItem]          # ★複数商品に対応（HATTIMCDは1品しか扱えない）
    redirect_urls: list[str] = field(default_factory=list)
    raw_hex: str = ""

    @property
    def total_amount(self) -> int:
        return sum(i.amount for i in self.items)

    @property
    def product_codes(self) -> list[str]:
        return [p.product_code for i in self.items for p in i.walk() if p.product_code]

    @property
    def pickup_label(self) -> str:
        return PICKUP_LABEL.get(self.pickup_method or "", "未指定")


def _parse_item(raw: bytes, *, top_level: bool) -> OrderItem:
    f = proto_parse(raw)
    code = ""
    for v in f.get(2, []):
        if isinstance(v, bytes):
            code = _as_str(v)
    quantity = int(f.get(3, [1])[0]) if 3 in f else 1
    amount = int(f.get(4, [0])[0]) if 4 in f else 0
    return OrderItem(
        product_code=code,
        quantity=quantity,
        amount=amount,
        has_flag=1 in f,
        components=[
            _parse_item(r, top_level=False) for r in f.get(5, []) if isinstance(r, bytes)
        ],
    )


def decode_hex(hex_str: str) -> DecodedOrder:
    """hex をデコードする。壊れていれば ProtocolError。"""
    cleaned = "".join(hex_str.split()).lower()
    if not cleaned:
        raise ProtocolError("注文コードが空です")
    if len(cleaned) % 2:
        raise ProtocolError("注文コードの文字数が奇数です（途中で切れています）")
    try:
        data = bytes.fromhex(cleaned)
    except ValueError as e:
        raise ProtocolError("注文コードに16進数でない文字が含まれています") from e

    top = proto_parse(data)

    store_id = ""
    for v in top.get(1, []):
        if isinstance(v, bytes):
            store_id = _as_str(v)

    # リダイレクトURL（createPaymentMethod の中に入っていることがある）
    urls: list[str] = []
    for raw in top.get(3, []) + top.get(2, []):
        if not isinstance(raw, bytes) or not raw:
            continue
        try:
            sub = proto_parse(raw)
        except ProtocolError:
            continue
        for vals in sub.values():
            for v in vals:
                if isinstance(v, bytes):
                    try:
                        inner = proto_parse(v)
                    except ProtocolError:
                        continue
                    for ivals in inner.values():
                        for iv in ivals:
                            if isinstance(iv, bytes) and iv.startswith(b"http"):
                                urls.append(_as_str(iv))

    # ★ 8 → 2 が繰り返しの商品リスト（HATTIMCDは最後の1件で上書きしていた）
    items: list[OrderItem] = []
    for f8 in top.get(8, []):
        if not isinstance(f8, bytes):
            continue
        for f2 in proto_parse(f8).get(2, []):
            if isinstance(f2, bytes):
                items.append(_parse_item(f2, top_level=True))

    return DecodedOrder(
        store_id=store_id,
        pickup_method=detect_pickup_method(top),
        items=items,
        redirect_urls=urls,
        raw_hex=cleaned,
    )


# ============================================================
#  組み立て
# ============================================================


def build_item(item: OrderItem, *, top_level: bool = False) -> bytes:
    """
    注文の1品をバイト列にする。

    ⚠️ field 1 のフラグには規則がある。実物の注文コードを調べると、

        9180        最上位の商品      フラグなし
          9987009   選択枠            フラグあり
            2020    実際の商品        フラグなし
          9997918   選択枠            フラグあり
            9997914 中間ノード        フラグあり
              3120  実際の商品        フラグなし

    つまり「**子を持っていて、かつ最上位でない**」ものだけに付く。
    組み立てる側が付け忘れると相手が受け付けないため、
    持っている値を信じず、構造から決める。
    """
    b = b""
    if item.components and not top_level:
        b += pb_int(1, 1)
    b += pb_str(2, item.product_code)
    b += pb_int(3, item.quantity)
    if top_level and item.amount:
        b += pb_int(4, item.amount)
    for c in item.components:
        b += pb_msg(5, build_item(c))
    return b


def build_hex(
    store_id: str,
    items: list[OrderItem],
    pickup_method: str,
    *,
    pickup_number: int | None = None,
    redirect_url: str | None = None,
) -> str:
    """
    注文内容から hex を組み立てる。

    mcdon.asia を使わずに BOT 側で注文コードを作るための関数。
    """
    if not store_id:
        raise ProtocolError("店舗IDが指定されていません")
    if not items:
        raise ProtocolError("商品が1つも入っていません")

    body = pb_str(1, store_id)
    if redirect_url:
        body += pb_msg(3, pb_msg(8, pb_str(1, redirect_url) + pb_str(3, redirect_url)))
    body += pb_msg(7, build_pickup_payload(pickup_method, number=pickup_number))
    order = b"".join(pb_msg(2, build_item(i, top_level=True)) for i in items)
    body += pb_msg(8, order)
    return body.hex()


def build_store_order_body(
    decoded: DecodedOrder,
    *,
    pos_paseto: str,
    card_id: str = "",
    pickup_method: str | None = None,
    pickup_number: int | None = None,
) -> bytes:
    """
    StoreOrder へ送る本体を組み立てる。

    ⚠️ HATTIMCD は field 7 を常に空で送るため、受取方法が一切伝わらなかった。
       ここでは呼び出し側が指定した受取方法を必ず載せる。
    """
    method = pickup_method or decoded.pickup_method or "takeOut"

    b = pb_str(1, decoded.store_id)
    # field 2: deliveryMethod（レスポンス側と同じ形。takeOut を既定にする）
    b += pb_msg(2, pb_msg(PICKUP_FIELD.get(method, 2), b""))
    # field 3: createPaymentMethod（登録済みカード）
    if card_id:
        b += pb_msg(3, pb_msg(1, pb_msg(2, pb_str(1, card_id))))
    else:
        b += pb_msg(3, pb_msg(1, b""))
    # field 7: createDeliveryMethod ★ここが本命
    b += pb_msg(7, build_pickup_payload(method, number=pickup_number))
    # field 8: 商品（複数対応）
    order = b"".join(pb_msg(2, build_item(i, top_level=True)) for i in decoded.items)
    b += pb_msg(8, order)
    if pos_paseto:
        b += pb_str(12, pos_paseto)
    return b


def build_authorise_body(order_token: str) -> bytes:
    return pb_str(1, order_token) + pb_msg(2, pb_msg(2, b""))


def build_get_paid_body(order_token: str) -> bytes:
    return pb_str(1, order_token)


# ============================================================
#  注文レスポンス（docs/07 §4.3 / mcdord.Order）
# ============================================================


@dataclass
class OrderResponse:
    order_code: str = ""
    short_order_code: str = ""
    status: int = 0
    store_id: str = ""
    total_amount: int = 0
    display_order_number: str = ""   # ★注文番号（例 7161）
    order_token: str = ""
    raw_hex: str = ""


def parse_order_response(data: bytes) -> OrderResponse:
    top = proto_parse(data, strict=False)
    raw = next((v for v in top.get(2, []) if isinstance(v, bytes)), None)
    if raw is None:
        raw = next((v for v in top.get(1, []) if isinstance(v, bytes)), None)
    if raw is None:
        return OrderResponse(raw_hex=data.hex())

    try:
        f = proto_parse(raw, strict=False)
    except ProtocolError:
        return OrderResponse(raw_hex=data.hex())

    def s(n: int) -> str:
        return _as_str(f[n][0]) if n in f and f[n] else ""

    def i(n: int) -> int:
        return int(f[n][0]) if n in f and f[n] and isinstance(f[n][0], int) else 0

    return OrderResponse(
        order_code=s(1),
        short_order_code=s(2),
        status=i(3),
        store_id=s(4),
        total_amount=i(7),
        display_order_number=s(9),   # docs/07 §4.3
        order_token=s(10),
        raw_hex=data.hex(),
    )
