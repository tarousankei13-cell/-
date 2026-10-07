"""protobuf層の検証 — 実際のHEX（南砂町店・月見バーガーセット）で確認する"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.mcd.protocol import (
    decode_hex, build_hex, build_store_order_body, build_item,
    OrderItem, ProtocolError, PICKUP_LABEL, proto_parse,
)

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

# 利用者からいただいた実HEX（欠落2バイトを復元したもの）
REAL = ("0a0531333933341a4842460a2168747470733a2f2f6d63646f6e2e617369612f6d6f702f3133"
        "3933342f617574681a2168747470733a2f2f6d63646f6e2e617369612f6d6f702f31333933342f"
        "617574683a020a00424e124c120439313830180120a0062a17080112073939383730303918012a"
        "0812043230323018012a2608011207393939373931381801" + "2a1708011207393939373931341801"
        + "2a08120433313230" + "1801")

print("\n[1] 実HEXのデコード")
d = decode_hex(REAL)
check("店舗ID 13934", d.store_id == "13934", d.store_id)
check("受取方法 eatIn（店内カウンター受取）", d.pickup_method == "eatIn", d.pickup_method)
check("ラベル表示", d.pickup_label == "店内でお召し上がり", d.pickup_label)
check("商品1点", len(d.items) == 1, len(d.items))
check("商品コード 9180", d.items[0].product_code == "9180", d.items[0].product_code)
check("合計 800円", d.total_amount == 800, d.total_amount)
codes = d.product_codes
check("構成品を全部たどれる", set(codes) >= {"9180","9987009","2020","9997918","9997914","3120"}, codes)
check("リダイレクトURLを抽出", any("mcdon.asia" in u for u in d.redirect_urls), d.redirect_urls)

print("\n[2] ラウンドトリップ（組み立て→分解で同じになるか）")
rebuilt = build_hex(d.store_id, d.items, d.pickup_method)
d2 = decode_hex(rebuilt)
check("店舗ID一致", d2.store_id == d.store_id)
check("受取方法一致", d2.pickup_method == d.pickup_method)
check("金額一致", d2.total_amount == d.total_amount)
check("商品構成が完全一致", [i.to_dict() for i in d2.items] == [i.to_dict() for i in d.items])

print("\n[3] 受取方法の判定（HATTIMCDのバグ修正確認）")
for m, label in PICKUP_LABEL.items():
    if m in ("tableDelivery", "curbsidePickUp", "addressDelivery"): continue
    h = build_hex("10528", d.items, m)
    check(f"{m} → {label}", decode_hex(h).pickup_method == m, decode_hex(h).pickup_method)
h = build_hex("10528", d.items, "tableDelivery", pickup_number=12)
check("tableDelivery（席まで・番号付き）", decode_hex(h).pickup_method == "tableDelivery")
try:
    build_hex("10528", d.items, "tableDelivery"); check("番号なしは拒否", False)
except ProtocolError: check("番号なしは拒否", True)

print("\n[4] ★複数商品（HATTIMCDが壊れるケース）")
multi = [
    OrderItem("9180", amount=800, components=[OrderItem("9987009", components=[OrderItem("2020")])]),
    OrderItem("2110", amount=140),
    OrderItem("3110", amount=160),
]
h = build_hex("13934", multi, "takeOut")
dm = decode_hex(h)
check("3品とも保持される", len(dm.items) == 3, len(dm.items))
check("商品コードが順序どおり", [i.product_code for i in dm.items] == ["9180","2110","3110"])
check("合計 1100円（各商品の合算）", dm.total_amount == 1100, dm.total_amount)
check("1品目の構成品が残る", dm.items[0].components[0].components[0].product_code == "2020")

print("\n[5] 壊れたHEXを弾く")
for bad, why in [
    ("", "空"), ("0a05313339", "途中で切れている"), ("zzzz", "16進数でない"),
    ("0a0531333933", "長さプレフィックスと実体が不一致"), ("0a053133393334a", "文字数が奇数"),
]:
    try:
        decode_hex(bad); check(f"{why} を拒否", False)
    except ProtocolError: check(f"{why} を拒否", True)

print("\n[6] StoreOrder本体に受取方法が載るか（HATTIMCD最大のバグ）")
body = build_store_order_body(d, pos_paseto="v2.local.dummy", card_id="card123")
top = proto_parse(body)
check("field 1 に店舗ID", top[1][0].decode() == "13934")
check("field 7 が空でない", bool(top[7][0]), top[7][0].hex() if top.get(7) else None)
check("field 7 に eatIn(=1)", 1 in proto_parse(top[7][0]))
check("field 12 に posトークン", top[12][0].decode() == "v2.local.dummy")
body_t = build_store_order_body(d, pos_paseto="x", pickup_method="takeOut")
check("受取方法を上書きできる（takeOut=2）", 2 in proto_parse(proto_parse(body_t)[7][0]))

print("\n[7] ★複数の商品が本当に相手へ届くか（相手の読み方で確認）")
# ⚠️ これが無かったせいで、複数品の注文が最後の1品しか届かないバグを
#    長く見逃した。decode_hex は寛容に読むので、自分で作ったコードを
#    自分で読み返すと3品に見えてしまう。相手の読み方で確かめること。
#
#    公式の定義（マクドナルドのアプリから取得・docs/04）：
#        CreateOrderInput { 8: repeated OrderItem items }   繰り返し
#        OrderItem        { 2: OrderProduct   product }     ★単数★
from dataclasses import replace as _replace

from services.mcd.protocol import (
    build_pickup_payload, decode_as_mcdonalds, merge_items, pb_int, pb_msg, pb_str,
)

three = [
    OrderItem(product_code="2157", quantity=1, amount=200),   # 三角チョコパイ いちご
    OrderItem(product_code="2139", quantity=1, amount=190),   # 三角チョコパイ 黒
    OrderItem(product_code="2080", quantity=1, amount=250),   # シャカチキ
]

# いまの作り方を、相手の読み方で読む
got = decode_as_mcdonalds(build_hex("10528", three, "takeOut"))
check("3品すべて届く ★", len(got.items) == 3, [i.product_code for i in got.items])
check("合計が ¥640 のまま ★", got.total_amount == 640, got.total_amount)
check("並び順が変わらない",
      [i.product_code for i in got.items] == ["2157", "2139", "2080"])

# ⚠️ 昔の作り方（field 8 を1つにまとめる）だと、相手には最後の1品しか
#    届かない。実際に ¥640 の注文が ¥250 になった（2026-10-07）。
#    もう一度この形に戻してしまわないよう、症状を固定しておく。
_body = pb_str(1, "10528") + pb_msg(7, build_pickup_payload("takeOut"))
_body += pb_msg(8, b"".join(
    pb_msg(2, build_item(i, top_level=True)) for i in merge_items(three)
))
_old = decode_as_mcdonalds(_body.hex())
check("昔の作り方だと1品に潰れる（再現）★", len(_old.items) == 1, len(_old.items))
check("潰れると最後の1品が残る（¥250）★", _old.total_amount == 250, _old.total_amount)

# ⚠️ 自分の読み方では、昔の作り方でも3品に見えてしまう。
#    これが「自分で確かめても気づけない」の正体。
check("自分の読み方では昔の形も3品に見える（だから当てにならない）★",
      len(decode_hex(_body.hex()).items) == 3)

# 同じ商品を並べたときは、数量にまとめてから1件にする
same = [OrderItem(product_code="2080", quantity=1, amount=250) for _ in range(3)]
got_same = decode_as_mcdonalds(build_hex("10528", same, "takeOut"))
check("同じ商品は1件×数量3にまとまる", len(got_same.items) == 1, len(got_same.items))
check("数量が3になっている ★",
      got_same.items[0].quantity == 3, got_same.items[0].quantity)

# セット（入れ子）が混ざっても崩れない
nested = [
    OrderItem(product_code="9030", quantity=1, amount=500, components=[
        OrderItem(product_code="9997925", quantity=1, has_flag=True, components=[
            OrderItem(product_code="9997922", quantity=1, has_flag=True, components=[
                OrderItem(product_code="3120", quantity=1)])])]),
    OrderItem(product_code="2080", quantity=1, amount=250),
]
got_n = decode_as_mcdonalds(build_hex("10528", nested, "takeOut"))
check("セットと単品を混ぜても2件のまま ★", len(got_n.items) == 2, len(got_n.items))
check("セットの中身が残っている ★",
      bool(got_n.items[0].components)
      and got_n.items[0].components[0].product_code == "9997925")

# 実物の公式コードも、相手の読み方で同じに読める
check("実物の公式コードも相手の読み方で読める ★",
      [i.product_code for i in decode_as_mcdonalds(REAL).items]
      == [i.product_code for i in decode_hex(REAL).items])

print("\n[8] ★決済の成否を取り違えないか")
# ⚠️ 公式の定義（docs/04）：
#      CreateOrderOutput    { 1: Order }
#      AuthoriseOrderOutput { 1: Order }
#      GetPaidOrderOutput   { 1: isSettled bool / 2: Order / 3: PaymentError }
#
#    決済が成立したかは **isSettled** が答え。注文オブジェクトが返って
#    きたかどうかでは判断できない。カードを断られた注文にも order_code は
#    付く。そこを取り違えると、**お金が動いていないのに残高を引き落として
#    「成功しました」と伝える**ことになる。
from services.mcd.protocol import pb_int, parse_order_response

_order = pb_str(1, "OC123") + pb_int(7, 640) + pb_str(9, "7161") + pb_str(10, "tok")

r = parse_order_response(pb_msg(1, _order))
check("StoreOrder は注文を読める", r.order_code == "OC123" and r.total_amount == 640)
check("StoreOrder は成否を言わない（None）★", r.is_settled is None, r.is_settled)

r = parse_order_response(pb_int(1, 1) + pb_msg(2, _order))
check("GetPaidOrder・成立を読める ★", r.is_settled is True, r.is_settled)
check("成立でも注文の中身は読める", r.display_order_number == "7161")

r = parse_order_response(pb_int(1, 0) + pb_msg(2, _order))
check("GetPaidOrder・不成立を読める ★", r.is_settled is False, r.is_settled)
# ⚠️ ここが肝。不成立でも注文コードは付いている。
check("不成立でも注文コードは付いている（だから当てにならない）★",
      r.order_code == "OC123")

r = parse_order_response(pb_int(1, 0) + pb_msg(2, _order) + pb_msg(3, pb_int(1, 5)))
check("決済エラーを見つけられる ★", r.has_payment_error is True)

# saga 側が、不成立を「成立」と読まないこと
import asyncio as _asyncio

from core import saga as _saga


class _FakeHandle:
    def __init__(self, resp):
        self.client = type("C", (), {
            "get_paid_order": lambda _s, g, t, _r=resp: _asyncio.sleep(0, result=_r)
        })()


_settled = parse_order_response(pb_int(1, 1) + pb_msg(2, _order))
_failed = parse_order_response(pb_int(1, 0) + pb_msg(2, _order))
_nosay = parse_order_response(pb_msg(1, _order))

_got = _asyncio.run(_saga._probe_paid(_FakeHandle(_settled), "g", "t", attempts=1))
check("成立なら成立として返す ★", _got is not None and _got.is_settled is True)

_got = _asyncio.run(_saga._probe_paid(_FakeHandle(_failed), "g", "t", attempts=1))
check("不成立を『成立』と読まない ★★", _got is None, _got)

_got = _asyncio.run(_saga._probe_paid(_FakeHandle(_nosay), "g", "t", attempts=1))
check("成否を言わない応答は、注文の有無で判断する", _got is not None)

print("\n[9] 決済の受取方法が固定されていないか")
# ⚠️ 以前は takeOut 固定だった。店内を選んでも決済だけ「お持ち帰り」で
#    送っていた。StoreOrder と食い違う指定になる。
from services.mcd.protocol import build_authorise_body

for _m, _f in (("eatIn", 1), ("takeOut", 2), ("tableDelivery", 3), ("driveThru", 5)):
    _inner = proto_parse(proto_parse(build_authorise_body("tok", _m))[2][0])
    check(f"決済でも {_m} が伝わる ★", _f in _inner, list(_inner))

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
