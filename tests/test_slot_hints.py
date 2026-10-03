"""
参照商品が載っていない選択枠（ハッピーセットのドリンク）

ハッピーセットの3つ目の枠（9987016 / 9987017）は、8商品すべてで
参照商品・既定商品がどちらも空。構成品の 2981 もカタログに定義が無い。
カタログだけでは何の枠か導き出せないため、手がかりの表を持たせた。

あわせて、参照が無い枠に **おもちゃやスライスチーズが並ぶ** という
別のバグも直したので、そこも固定しておく。
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

HERE = os.path.dirname(os.path.abspath(__file__))
HAPPY = ["9005", "9006", "9007", "9008", "9058", "9059", "9064", "9138"]
MORNING = {"9005", "9006", "9007", "9008"}


def main():
    from services.mcd.menu import parse_menu
    from services.mcd import slot_hints

    menu = parse_menu("13934", json.load(open(f"{HERE}/m13934.json")))

    print("── 手がかりの表 ──")
    check("通常のハッピーセットの枠を知っている",
          slot_hints.reference_for("9987016") in menu.products,
          slot_hints.reference_for("9987016"))
    check("朝のハッピーセットの枠も知っている",
          slot_hints.reference_for("9987017") in menu.products)
    check("知らない枠には答えない", slot_hints.reference_for("9997918") == "")
    check("呼び名を持っている", "ドリンク" in slot_hints.note_for("9987016"),
          slot_hints.note_for("9987016"))
    check("先に並べるものが全部カタログにある",
          all(c in menu.products for c in slot_hints.preferred("9987016")),
          [c for c in slot_hints.preferred("9987016") if c not in menu.products])

    print("\n── ハッピーセット8商品 ──")
    for code in HAPPY:
        p = menu.products[code]
        check(f"{code} {p.name[:22]} の枠が全部埋まる",
              not menu.unfillable_slots(p), [s.code for s in menu.unfillable_slots(p)])

    print("\n── 時間帯（画像と同じ 8:58）──")
    morning = {c for c in HAPPY if menu.orderable(menu.products[c], 8 * 60 + 58)}
    check("朝は朝マックのハッピーセットだけ4つ", morning == MORNING, sorted(morning))
    noon = {c for c in HAPPY if menu.orderable(menu.products[c], 12 * 60)}
    check("昼は通常のハッピーセットだけ4つ",
          noon == set(HAPPY) - MORNING, sorted(noon))

    print("\n── ドリンクの候補 ──")
    p = menu.products["9138"]
    slot = next(s for s in p.slots_of("choices") if s.code == "9987016")
    cand = menu.choice_candidates(slot, 12 * 60, parent=p)
    names = [q.name for q in cand]
    check("候補が出る", len(cand) >= 10, len(cand))
    check("実際のハッピーセットで選べるものが先頭4つ",
          [q.code for q in cand[:4]] == slot_hints.preferred("9987016"),
          [q.code for q in cand[:4]])
    check("ミルクが入っている", any("ミルク" == n for n in names), names[:6])
    check("おもちゃが混ざっていない",
          not any("おもちゃ" in n or "えほん" in n or "プラレール" in n for n in names),
          [n for n in names if "おもちゃ" in n or "えほん" in n])
    check("食べ物が混ざっていない",
          not any("バーガー" in n or "ポテト" in n for n in names),
          [n for n in names if "バーガー" in n or "ポテト" in n])

    print("\n── 価格（prePrice が実売価格）──")
    check("ハンバーガー ハッピーセットは540円",
          menu.products["9138"].price_for("takeOut") == 540,
          menu.products["9138"].price_for("takeOut"))
    check("店内でも同じ540円",
          menu.products["9138"].price_for("eatIn") == 540)
    check("宅配は630円（セットでも分けて見る）",
          menu.products["9138"].price_for("addressDelivery") == 630,
          menu.products["9138"].price_for("addressDelivery"))
    check("単品は宅配のほうが高い",
          menu.products["1010"].price_for("addressDelivery")
          >= menu.products["1010"].price_for("takeOut"))

    print("\n── 参照の無い枠に関係ないものを出さない ──")
    bad = []
    for q in menu.products.values():
        for s in q.slots_of("choices"):
            if menu.slot_reference(s):
                continue
            for e in menu.choice_candidates(s, 12 * 60, parent=q):
                n = getattr(e, "name", "")
                if "おもちゃ" in n or "えほん" in n or "プラレール" in n \
                        or "スライスチーズ" in n or "ソースなし" in n:
                    bad.append((q.code, q.name, s.code, n))
    check("おもちゃ・スライスチーズが別の枠に出ない ★", not bad, bad[:4])

    print("\n── もともと正しかったものを壊していない ──")
    sk = menu.products["2080"]
    s1 = next(s for s in sk.slots_of("choices") if not menu.slot_reference(s))
    check("シャカチキの味は残っている",
          [getattr(e, "name", "") for e in menu.choice_candidates(s1, 12 * 60, parent=sk)]
          == ["シャカチキ チェダーチーズ味シーズニング"],
          [getattr(e, "name", "") for e in menu.choice_candidates(s1, 12 * 60, parent=sk)])
    sal = menu.products["2323"]
    s2 = next(s for s in sal.slots_of("choices") if not menu.slot_reference(s))
    got = [getattr(e, "name", "") for e in menu.choice_candidates(s2, 12 * 60, parent=sal)]
    check("サイドサラダのドレッシングは残っている",
          len(got) == 1 and "ドレッシング" in got[0], got)
    nug = menu.products["1670"]
    s3 = next(s for s in nug.slots_of("choices") if s.code == "7251")
    check("ナゲットのソースは3種類のまま",
          len(menu.choice_candidates(s3, 12 * 60, parent=nug)) == 3,
          [getattr(e, "name", "") for e in menu.choice_candidates(s3, 12*60, parent=nug)])
    toy = next(s for s in menu.products["9138"].slots_of("choices")
               if s.code == "9997008")
    check("おもちゃの枠にはおもちゃが出る",
          any("おもちゃ" in getattr(e, "name", "")
              for e in menu.choice_candidates(toy, 12 * 60, parent=menu.products["9138"])))

    print("\n── 従業員用の食事は売らない ──")
    check("エンプロイミールを6件つかんでいる", len(menu.staff_only) == 6,
          sorted(menu.staff_only))
    check("全部エンプロイミールという名前",
          all("エンプロイミール" in menu.products[c].name for c in menu.staff_only))
    check("どれも注文できない ★",
          all(not menu.orderable(menu.products[c], m)
              for c in menu.staff_only for m in (8 * 60, 12 * 60, 20 * 60)))

    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
