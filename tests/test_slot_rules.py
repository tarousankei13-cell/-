"""
セットの選択枠の学習の検証

⚠️ カタログには「どの枠に何を入れられるか」が書かれていない。
   そのため候補を出しすぎており、マクドナルドに断られることがある。
   実際に断られた組み合わせを覚えて、次から出さないことを確かめる。
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    tmp = tempfile.mkdtemp()
    from services.mcd import slot_rules as R
    R.STORE_PATH = Path(tmp) / "slot_rules.json"
    R.load()

    from services.mcd.menu import parse_menu
    from services.mcd.protocol import OrderItem

    menu = parse_menu("13934", json.load(open(os.path.join(HERE, "m13934.json"))))
    MIN = 18 * 60 + 14          # 18:14（実際に失敗した時刻）
    meal = menu.products["9180"]
    side, drink = meal.slots_of("choices")[0], None
    for sl in meal.slots_of("choices"):
        if sl.reference_product == "3120":
            drink = sl

    print("\n[1] カタログには候補一覧が無い（前提の確認）★")
    raw = json.load(open(os.path.join(HERE, "m13934.json")))
    ch = raw["products"]["9180"]["choices"][0]
    check("枠が持つのは参照と既定だけ ★",
          set(ch) & {"productCodes", "candidates", "options", "allowed"} == set(),
          [k for k in ch])
    check("参照商品だけが手掛かり", ch.get("referenceProduct") == "2020", ch.get("referenceProduct"))

    print("\n[2] 確かなものが先に並ぶ ★")
    cands = menu.choice_candidates(side, MIN)
    names = [p.name for p in cands]
    check("候補が複数ある（コーラだけ問題の再発防止）★", len(cands) > 3, len(cands))
    check("参照商品が先頭 ★", names[0] == "マックフライポテト® M", names[:3])
    sizes = {p.code for p in menu.size_variants("2020")}
    top = {p.code for p in cands[:len(sizes)]}
    check("サイズ違いも前のほう ★", sizes & top, (sorted(sizes), names[:4]))

    print("\n[3] 断られたら次から出さない ★")
    check("最初はソースも出てしまう（既知の問題）",
          any("ソース" in n for n in names), names)
    for code in ("5502", "5503", "5644"):
        R.reject("13934", side.code, code)
    after = [p.name for p in menu.choice_candidates(side, MIN)]
    check("ソースが消える ★", not any("ソース" in n for n in after), after)
    check("他は残る ★", "マックフライポテト® M" in after and "サイドサラダ" in after, after)
    check("消えたのは3件だけ", len(names) - len(after) == 3, (len(names), len(after)))

    print("\n[4] 店舗ごとに覚える ★")
    other = parse_menu("99999", json.load(open(os.path.join(HERE, "m13934.json"))))
    o_names = [p.name for p in other.choice_candidates(other.products["9180"].slots_of("choices")[0], MIN)]
    check("別の店舗には影響しない ★", any("ソース" in n for n in o_names), o_names[:5])

    print("\n[5] 一度通ったものは消さない ★")
    # 売り切れなど、その時だけの事情で断られることがある
    R.confirm("13934", side.code, "2020")
    check("通った記録がある", R.allowed("13934", side.code, "2020"))
    newly = R.reject("13934", side.code, "2020")
    check("通ったものは覚え直さない ★", newly is False)
    check("候補に残る ★", R.allowed("13934", side.code, "2020"))

    print("\n[6] 同じ組み合わせを二重に覚えない")
    first = R.reject("13934", side.code, "9999")
    second = R.reject("13934", side.code, "9999")
    check("1回目は覚える", first is True)
    check("2回目は覚えない", second is False)

    print("\n[7] 注文から枠と商品を取り出せる ★")
    # 枠 →（中間ノード）→ 商品 の入れ子を辿る
    item = OrderItem(product_code="9180", components=[
        OrderItem(product_code="9987009", components=[
            OrderItem(product_code="1610")]),                    # 中間なし
        OrderItem(product_code="9997918", components=[
            OrderItem(product_code="9997914", components=[
                OrderItem(product_code="3918")])]),              # 中間あり
        OrderItem(product_code="99901032", quantity=0),          # 具材（枠ではない）
    ])
    got = R.choices_of(item)
    check("サイド枠を取り出せる ★", ("9987009", "1610") in got, got)
    check("中間ノードを越えて取り出せる ★", ("9997918", "3918") in got, got)
    check("具材を枠と間違えない ★", len(got) == 2, got)

    print("\n[8] 保存して読み直せる ★")
    R2_rejected = dict(R.summary())
    R._loaded = False
    R._rejected.clear(); R._confirmed.clear()
    R.load()
    check("再起動しても覚えている ★", not R.allowed("13934", side.code, "5502"))
    check("件数も一致", R.summary() == R2_rejected, (R.summary(), R2_rejected))

    print("\n[9] 管理者が消せる ★")
    n = R.forget("13934", side.code, "5502")
    check("1件だけ消せる ★", n == 1 and R.allowed("13934", side.code, "5502"), n)
    n = R.forget("13934")
    check("店舗ごとまとめて消せる ★", n > 0, n)
    check("全部消えた", R.allowed("13934", side.code, "5503"))

    print("\n[10] 壊れたファイルでも止まらない ★")
    R.STORE_PATH.write_text("これはJSONではない")
    R._loaded = False
    R.load()
    check("読めなくても例外にならない ★", R.summary() == {"rejected": 0, "confirmed": 0},
          R.summary())
    check("その状態でも候補は出る ★", R.allowed("13934", side.code, "2020"))

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
