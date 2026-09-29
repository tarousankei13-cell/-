"""
店舗が「いま注文できるか」の判定の検証

注文できない店舗で商品を選ばせてしまうと、選び終えてから断られる。
店舗を選んだ時点で止まること、理由がきちんと伝わることを確かめる。
"""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from services.mcd import availability as av

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

DATE = "2026-09-30"


def store(*, mop=True, foe="NORMAL", methods=("eatIn", "takeOut"),
          dayparts=None, opening=(360, 1440), method_hours=None):
    """実データと同じ形の店舗JSONを組み立てる。"""
    from services.mcd.protocol import STORE_DELIVERY_KEY
    dm = {}
    for m, key in STORE_DELIVERY_KEY.items():
        node = {"isSupported": m in methods}
        if method_hours and m in method_hours:
            node["businessHours"] = {"businessHours": {DATE: method_hours[m]}}
        dm[key] = node
    if dayparts is None:
        dayparts = [
            ("DAYPART_BREAKFAST", [{"start": 360, "end": 620}]),
            ("DAYPART_REGULAR", [{"start": 620, "end": 1430}]),
        ]
    raw = {
        "store": {
            "id": "10571", "name": "テスト店", "mopEnabled": mop, "foeStatus": foe,
            "deliveryMethod": dm,
            "openingHours": {"businessHours": {DATE: {"start": opening[0], "end": opening[1]}}},
        },
        "mopDaypartAbilityLists": {
            DATE: {"daypartAbilities": [
                {"daypart": n, "checkoutable": w} for n, w in dayparts
            ]}
        },
    }
    return raw


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/av.db")

    print("\n[1] 時刻による可否")
    s = store()
    for minutes, want, why in [
        (0, False, "深夜"), (359, False, "開店1分前"), (360, True, "開店ちょうど"),
        (620, True, "朝からレギュラーへの切り替わり"), (1429, True, "受付終了1分前"),
        (1430, False, "受付終了ちょうど"), (1439, False, "閉店間際"),
    ]:
        a = av.check(s, minutes=minutes, date_key=DATE)
        check(f"{av.fmt(minutes)} は{'注文できる' if want else '注文できない'}（{why}）",
              a.orderable == want, f"{a.code} {a.title}")

    print("\n[2] 受付時間の切れ目（朝とレギュラーの間が空くお店）")
    gap = store(dayparts=[
        ("DAYPART_BREAKFAST", [{"start": 360, "end": 600}]),
        ("DAYPART_REGULAR", [{"start": 630, "end": 1380}]),
    ])
    a = av.check(gap, minutes=615, date_key=DATE)
    check("切れ目の時間は注文できない", not a.orderable, a.code)
    check("営業時間外ではなく『受け付けていない』と伝える", a.code == av.OUTSIDE, a.code)
    check("次に注文できる時刻を示す", a.next_at == 630, a.next_at)
    check("案内文に時刻が入る", "10:30" in a.hint, a.hint)

    print("\n[3] 次に注文できる時刻")
    a = av.check(store(), minutes=100, date_key=DATE)
    check("開店前なら今日の開店時刻", a.next_at == 360, a.next_at)
    check("表示は 6:00", av.fmt(a.next_at) == "6:00", av.fmt(a.next_at))
    a = av.check(store(), minutes=1435, date_key=DATE)
    check("受付終了後は翌日の開店時刻", a.next_at == 360 + 1440, a.next_at)
    check("表示は 翌6:00", av.fmt(a.next_at) == "翌6:00", av.fmt(a.next_at))

    print("\n[4] モバイルオーダー非対応の店舗")
    a = av.check(store(mop=False), minutes=660, date_key=DATE)
    check("注文できない", not a.orderable)
    check("理由が分かる文言", a.code == av.NOT_MOP and "対応していません" in a.title, a.title)
    check("別の店舗を勧める", "別の店舗" in a.reason, a.reason[:40])

    print("\n[5] 一時休業")
    a = av.check(store(foe="TEMPORARILY_CLOSED"), minutes=660, date_key=DATE)
    check("注文できない", not a.orderable)
    check("休業として扱う", a.code == av.SUSPENDED, a.code)

    print("\n[6] 受け取り方法")
    s = store(methods=("eatIn", "takeOut"))
    a = av.check(s, minutes=660, date_key=DATE, pickup_method="driveThru")
    check("対応していない方法は断る", not a.orderable and a.code == av.NO_METHOD, a.code)
    check("方法の名前を文言に出す", "ドライブスルー" in a.title, a.title)
    a = av.check(s, minutes=660, date_key=DATE, pickup_method="eatIn")
    check("対応している方法は通る", a.orderable)

    print("\n[7] 受け取り方法ごとの営業時間")
    s = store(methods=("eatIn", "takeOut"),
              method_hours={"eatIn": {"start": 360, "end": 1320}})   # 店内は22時まで
    a = av.check(s, minutes=1380, date_key=DATE, pickup_method="eatIn")   # 23時
    check("店内飲食の終了後は断る", not a.orderable and a.code == av.METHOD_CLOSED, a.code)
    check("対応時間を文言に出す", "22:00" in a.reason, a.reason)
    a = av.check(s, minutes=1380, date_key=DATE, pickup_method="takeOut")
    check("持ち帰りはまだ使える", a.orderable)
    a = av.check(s, minutes=1380, date_key=DATE)
    check("方法を指定しなければ、使える方法だけを返す",
          a.orderable and a.methods == ["takeOut"], a.methods)

    print("\n[8] いまの時間帯の名前")
    s = store(dayparts=[
        ("DAYPART_BREAKFAST", [{"start": 360, "end": 620}]),
        ("DAYPART_HIRU_MAC", [{"start": 620, "end": 1020}]),
        ("DAYPART_YORU_MAC", [{"start": 1020, "end": 1430}]),
        ("DAYPART_BREAKFAST_REGULAR", [{"start": 360, "end": 1430}]),
    ])
    for minutes, want in [(400, "朝マック"), (700, "ヒルマック"), (1200, "夜マック")]:
        a = av.check(s, minutes=minutes, date_key=DATE)
        check(f"{av.fmt(minutes)} は「{want}」", a.title == want, a.title)

    print("\n[9] 判断材料が無いときは止めない")
    a = av.check(store(), minutes=660, date_key="2030-01-01")
    check("知らない日付では通す", a.orderable and a.code == av.UNKNOWN, a.code)
    bare = {"store": {"id": "1", "mopEnabled": True,
                      "deliveryMethod": {"eatIn": {"isSupported": True}}}}
    a = av.check(bare, minutes=660, date_key=DATE)
    check("時間帯も営業時間も無ければ通す", a.orderable, a.code)

    print("\n[10] 深夜まで開いているお店（日をまたぐ指定）")
    late = store(dayparts=[("DAYPART_REGULAR", [{"start": 600, "end": 1560}])],
                 opening=(600, 1560))   # 10:00〜翌2:00
    check("25時（翌1時）も注文できる",
          av.check(late, minutes=60, date_key=DATE).orderable)
    check("朝9時はまだ開いていない",
          not av.check(late, minutes=540, date_key=DATE).orderable)

    print("\n[11] 保存済みの情報だけで判定できる（通信しない）")
    from db.models import StoreCache, StoreDaypart
    async with session_scope() as sess:
        sess.add(StoreCache(
            store_id="10571", group_name="group-h", store_name="高崎モントレー店",
            address="群馬県", cat_root_url="x",
            delivery_methods=json.dumps({"eatIn": True, "takeOut": True}),
            mop_enabled=True, foe_status="NORMAL",
            method_hours=json.dumps({"eatIn": {DATE: {"start": 360, "end": 1320}}}),
        ))
        sess.add(StoreDaypart(store_id="10571", daypart="DAYPART_BREAKFAST", date=DATE,
                              visible="[]", checkoutable=json.dumps([{"start": 360, "end": 620}])))
        sess.add(StoreDaypart(store_id="10571", daypart="DAYPART_REGULAR", date=DATE,
                              visible="[]", checkoutable=json.dumps([{"start": 620, "end": 1430}])))
    a = await av.check_store("10571", minutes=660, date_key=DATE)
    check("営業中と判定できる", a.orderable, f"{a.code} {a.title}")
    a = await av.check_store("10571", minutes=100, date_key=DATE)
    check("深夜は断れる", not a.orderable and a.next_at == 360, f"{a.code} {a.next_at}")
    a = await av.check_store("10571", minutes=1380, date_key=DATE, pickup_method="eatIn")
    check("受取方法ごとの時間も効く", not a.orderable and a.code == av.METHOD_CLOSED, a.code)
    a = await av.check_store("99999", minutes=660, date_key=DATE)
    check("知らない店舗は止めない", a.orderable and a.code == av.UNKNOWN, a.code)

    print("\n[12] 保存済みの店舗がモバイルオーダー非対応なら断る")
    async with session_scope() as sess:
        sess.add(StoreCache(
            store_id="10572", group_name="group-h", store_name="非対応店",
            address="群馬県", cat_root_url="x",
            delivery_methods=json.dumps({"eatIn": True}),
            mop_enabled=False, foe_status="NORMAL",
        ))
    a = await av.check_store("10572", minutes=660, date_key=DATE)
    check("非対応として断る", not a.orderable and a.code == av.NOT_MOP, a.code)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
