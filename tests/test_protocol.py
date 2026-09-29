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
check("ラベル表示", d.pickup_label == "店内（カウンター受取）", d.pickup_label)
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

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
