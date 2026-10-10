"""
セットの選択枠と、時間帯による絞り込みの検証

実際に起きた不具合:
  ・セットのドリンクがコカ・コーラしか選べない
  ・朝の時間にマックフライポテト（620分以降しか扱わない）を選べてしまい、
    注文時にマクドナルド側から弾かれる
  ・セットの中身の販売時間を確定直前に確認していなかった
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.mcd.menu import parse_menu, parse_size_groups, _limited_ability

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "menu_sample.json")


def build_menu():
    """実データの構造をそのまま小さくしたもの。"""
    def prod(code, cls="PRODUCT", day="", pre=0, comp=(), choices=()):
        return {
            "productCode": code, "productClass": cls, "dayPart": day,
            "priceList": [{"priceCode": "TAKEOUT", "price": 100},
                          {"priceCode": "EATIN", "price": 100}],
            "prePrice": {"valid": True, "price": pre},
            "composition": [{"productCode": c, "minQuantity": 1, "maxQuantity": 1}
                            for c in comp],
            "choices": [
                {"productCode": g, "defaultProduct": d, "referenceProduct": r,
                 "minQuantity": 1, "maxQuantity": 1,
                 "prePrice": {"valid": True, "price": 90}}
                for g, d, r in choices
            ],
            "canAdds": [],
        }

    products = {
        "9003": prod("9003", "VALUE_MEAL", "BREAKFAST_MENU", pre=630,
                     comp=["1110"],
                     choices=[("9987010", "5010", "5010"), ("9997925", "", "3120")]),
        "1110": prod("1110"),
        # サイド
        "5010": prod("5010"),          # ハッシュポテト（朝のみ）
        "2010": prod("2010"), "2020": prod("2020"), "2050": prod("2050"),  # ポテトS/M/L（昼のみ）
        "7010": prod("7010"),          # ナゲット（終日）
        # ドリンク
        "3110": prod("3110"), "3120": prod("3120"), "3150": prod("3150"),  # コーラS/M/L
        "3170": prod("3170"),          # スプライトM
        "3402": prod("3402"),          # ファンタM
    }
    names = {
        "9003": "フィレオフィッシュ® セット", "1110": "フィレオフィッシュ®",
        "5010": "ハッシュポテト", "2010": "マックフライポテト®", "2020": "マックフライポテト®",
        "2050": "マックフライポテト®", "7010": "チキンマックナゲット® 5ピース",
        "3110": "コカ・コーラ", "3120": "コカ・コーラ", "3150": "コカ・コーラ",
        "3170": "スプライト", "3402": "ファンタ グレープ",
    }
    ALLDAY = [{"start": 360, "end": 1430}]
    MORNING = [{"start": 360, "end": 620}]
    NOON = [{"start": 620, "end": 1430}]
    ability = {
        "9003": MORNING, "1110": ALLDAY, "5010": MORNING,
        "2010": NOON, "2020": NOON, "2050": NOON, "7010": ALLDAY,
        "3110": ALLDAY, "3120": ALLDAY, "3150": ALLDAY,
        "3170": ALLDAY, "3402": ALLDAY,
    }
    def day(a):
        return {"ability": {c: {"productCode": c, "checkoutable": w}
                            for c, w in a.items()}}
    return {
        "products": products,
        "groupMenu": {"products": {c: {"productCode": c, "tName": {"ja": n}}
                                   for c, n in names.items()}},
        "collections": [
            {"id": "3", "tName": {"ja": "サイドメニュー"},
             "productCodes": ["5010", "2010", "2020", "2050", "7010"]},
            {"id": "4", "tName": {"ja": "ドリンク"},
             "productCodes": ["3120", "3170", "3402"]},
            {"id": "6", "tName": {"ja": "朝マック"},
             "productCodes": ["9003", "5010", "3120", "3170", "3402", "7010"]},
        ],
        "sizeVariants": {
            "3120": {"productCode": "3120", "sizes": [
                {"productCode": "3110"}, {"productCode": "3120"}, {"productCode": "3150"}]},
            "2020": {"productCode": "2020", "sizes": [
                {"productCode": "2010"}, {"productCode": "2020"}, {"productCode": "2050"}]},
        },
        # ⚠️ 実データと同じく、壊れた日付キーを先頭に置く
        "limitedAbility": {
            "0008-09-30": day({c: ALLDAY for c in ability}),   # 壊れた日（全部終日）
            "2026-09-30": day(ability),
        },
    }


def main():
    raw = build_menu()

    print("\n[1] 壊れた日付キーを掴まない")
    ab = _limited_ability(raw, "2026-09-30")
    check("2000年以降の日付だけを見る",
          ab.get("2020", {}).get("checkoutable") == [{"start": 620, "end": 1430}],
          ab.get("2020"))
    ab_any = _limited_ability(raw)
    check("日付を指定しなくても壊れた日は選ばない",
          ab_any.get("2020", {}).get("checkoutable") != [{"start": 360, "end": 1430}],
          ab_any.get("2020"))

    m = parse_menu("10571", raw)
    print(f"  （商品 {len(m.products)} / カテゴリ {len(m.collections)}）")

    print("\n[2] 時間帯が正しく読めている")
    check("マックフライポテトは昼のみ",
          not m.products["2020"].is_orderable_at(410)
          and m.products["2020"].is_orderable_at(720))
    check("ハッシュポテトは朝のみ",
          m.products["5010"].is_orderable_at(410)
          and not m.products["5010"].is_orderable_at(720))
    check("フィレオフィッシュは終日", m.products["1110"].is_orderable_at(410))

    print("\n[3] ドリンクがコカ・コーラだけにならない ★今回の不具合")
    s9003 = m.products["9003"]
    drink_slot = s9003.slots_of("choices")[1]
    cands = m.choice_candidates(drink_slot, 410)
    names = [p.name for p in cands]
    check(f"ドリンクが複数選べる（{len(cands)}件）", len(cands) >= 3, names)
    check("コカ・コーラ以外も出る",
          any("スプライト" in n for n in names) and any("ファンタ" in n for n in names),
          names)

    print("\n[4] サイドは時間帯で入れ替わる")
    side_slot = s9003.slots_of("choices")[0]
    morning = [p.name for p in m.choice_candidates(side_slot, 410)]
    noon = [p.name for p in m.choice_candidates(side_slot, 720)]
    check("朝はハッシュポテトが出る", any("ハッシュ" in n for n in morning), morning)
    check("朝はマックフライポテトを出さない ★",
          not any("マックフライ" in n for n in morning), morning)
    check("昼はマックフライポテトが出る", any("マックフライ" in n for n in noon), noon)
    check("昼はハッシュポテトを出さない", not any("ハッシュ" in n for n in noon), noon)
    check("終日のナゲットはどちらにも出る",
          any("ナゲット" in n for n in morning) and any("ナゲット" in n for n in noon))

    print("\n[5] 一番小さいカテゴリを選ぶ")
    col = m.collection_of("5010")
    check("ハッシュポテトは「サイドメニュー」扱い（朝マックではない）",
          col is not None and col.name == "サイドメニュー", col.name if col else None)

    print("\n[6] サイズ違い")
    sizes = [p.code for p in m.size_variants("3120")]
    check("コーラのS/M/Lが揃う", sizes == ["3110", "3120", "3150"], sizes)
    check("サイズの無い商品は空", m.size_variants("1110") == [])
    check("どのサイズからも辿れる",
          [p.code for p in m.size_variants("3150")] == sizes)

    print("\n[7] 時間帯の情報が無い商品は止めない")
    raw2 = json.loads(json.dumps(raw))
    del raw2["limitedAbility"]["2026-09-30"]["ability"]["7010"]
    m2 = parse_menu("10571", raw2)
    check("扱いが不明なら注文できる扱い", m2.products["7010"].is_orderable_at(410))

    print("\n[8] カテゴリが分からない枠でも候補を出す")
    from services.mcd.menu import Slot
    orphan = Slot(kind="choices", code="9999999", default_product="", reference_product="3150")
    cands = m.choice_candidates(orphan, 410)
    check("サイズ違いで代用する", len(cands) >= 1, [p.name for p in cands])
    empty = Slot(kind="choices", code="9999998")
    check("基準商品が無ければ空", m.choice_candidates(empty, 410) == [])

    print("\n[時間帯区分の相性 ★]")
    # 時間帯区分の相性（分からないものは外さない）
    from services.mcd.menu import _dayparts_fit
    for a, b, want, label in (
        ("DAY_MENU", "DAY_MENU", True, "昼と昼"),
        ("DAY_MENU", "BREAKFAST_MENU", False, "昼のセットに朝の商品"),
        ("BREAKFAST_MENU", "DAY_MENU", False, "朝のセットに昼の商品"),
        ("DAY_MENU", "BREAKFAST_DAY_MENU", True, "朝も昼もの商品"),
        ("DAY_MENU", "", True, "区分が空（ナゲット）"),
        ("", "BREAKFAST_MENU", True, "親の区分が空"),
    ):
        check(f"時間帯の相性: {label}", _dayparts_fit(a, b) is want,
              _dayparts_fit(a, b))

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
