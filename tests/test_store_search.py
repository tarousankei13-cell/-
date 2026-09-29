"""店名検索の検証"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from services.mcd import store_index

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

FIXTURE = {
    "13934": {"n": "南砂町店", "a": "東京都江東区新砂３－３－５２", "g": "group-f"},
    "13903": {"n": "ビックカメラＡＫＩＢＡ店", "a": "東京都千代田区外神田１－１５－１", "g": "group-f"},
    "11003": {"n": "所沢店", "a": "埼玉県所沢市くすのき台", "g": "group-h"},
    "10510": {"n": "大間々さくらもーる店", "a": "群馬県みどり市", "g": "group-h"},
    "20002": {"n": "松本店", "a": "長野県松本市", "g": "group-g"},
    "10528": {"n": "イオン本荘店", "a": "秋田県由利本荘市", "g": "group-h"},
    "12345": {"n": "イオンモール新潟店", "a": "新潟県新潟市", "g": "group-e"},
}

tmp = Path(tempfile.mkdtemp()) / "stores.json"
tmp.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")

print("\n[1] 読み込み")
n = store_index.load_index(tmp)
check(f"{len(FIXTURE)}件を読み込んだ", n == len(FIXTURE), n)
check("利用可能になる", store_index.available())

print("\n[2] 店名の一部で探す")
for q, expect in [("南砂", "13934"), ("所沢", "11003"), ("松本", "20002")]:
    hits = store_index.search(q)
    check(f"「{q}」→ {expect}", hits and hits[0].store_id == expect,
          [h.store_id for h in hits])

print("\n[3] 表記ゆれに強いか")
for q, expect in [
    ("akiba", "13903"),            # 小文字・半角
    ("ＡＫＩＢＡ", "13903"),         # 全角
    ("びっくかめら", "13903"),       # ひらがな
    ("ビックカメラ", "13903"),       # カタカナ
    ("さくらモール", "10510"),       # 長音の有無
    ("さくらもーる", "10510"),
]:
    hits = store_index.search(q)
    check(f"「{q}」→ {expect}", hits and hits[0].store_id == expect,
          [h.store_id for h in hits] or "0件")

print("\n[4] 複数ヒットの並び順")
hits = store_index.search("イオン")
check(f"イオン系が2件", len(hits) == 2, [h.name for h in hits])
hits = store_index.search("店")
check("多数ヒットでも25件まで", len(hits) <= 25, len(hits))

print("\n[5] 店舗IDでも探せる")
hits = store_index.search("13934")
check("IDで一意に当たる", len(hits) == 1 and hits[0].store_id == "13934", hits)
check("get() で直接引ける", store_index.get("11003").name == "所沢店")
check("無いIDは None", store_index.get("99999") is None)

print("\n[6] 住所でも探せる")
hits = store_index.search("江東区")
check("「江東区」→ 南砂町店", hits and hits[0].store_id == "13934", [h.name for h in hits])
hits = store_index.search("埼玉")
check("「埼玉」→ 所沢店", hits and hits[0].store_id == "11003", [h.name for h in hits])

print("\n[7] 見つからない場合")
check("空文字は0件", store_index.search("") == [])
check("該当なしは0件", store_index.search("ありえない店名XYZ") == [])

print("\n[8] インデックスが無いとき")
missing = Path(tempfile.mkdtemp()) / "none.json"
n2 = store_index.load_index(missing)
check("0件で安全に失敗する", n2 == 0)
check("available() が False", not store_index.available())
check("検索しても落ちない", store_index.search("南砂") == [])

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
