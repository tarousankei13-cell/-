"""利用者の操作フローの検証 — Discordに繋がずにボタンの流れを確かめる"""
import asyncio, sys, os, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from _fake_discord import FakeInteraction, FakeUser, FakeClient, press
from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from core import ledger as L, settings, users as user_repo
from services.mcd import store_index

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

async def guarded(name, coro):
    """例外が出たら失敗として記録する"""
    try:
        await coro
        return True
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return False

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/ui.db")
    await settings.load_all()

    # ⚠️ 画面の操作はすべて貸し出しの関所を通る（ui/gate.py）。
    #    ライセンスが無いサーバーでは、画面ごとの決まりを見る前に
    #    止められる。ここで見たいのは「本人だけが押せる」などの
    #    画面側の決まりなので、先にホームとして登録しておく。
    #    （偽の FakeGuild は id=1）
    from core import license as lic
    await lic.ensure_home(1)

    from ui import flows, panels, embeds, menu_flows

    UID = 7001
    user = FakeUser(UID)
    client = FakeClient()
    await user_repo.get_or_create(UID)

    # 注文できる状態にするため、マクドナルドアカウントを1件登録しておく
    from db.models import McdAccount
    async with session_scope() as s:
        s.add(McdAccount(
            id=1, label="test", email_enc=b"x", card_id="c1",
            device_uid="d", wmop_device_id="w", fb_instance_id="f",
            home_lat=35.0, home_lng=139.0,
        ))

    print("\n[1] 常設パネルの表示")
    for mode in ("both", "hex", "menu"):
        await settings.set_value("order_mode", mode)
        e = embeds.order_panel(mode)
        names = [f.name for f in e.fields]
        check(f"mode={mode}: 手順が書かれている",
              any("はじめての方へ" in n for n in names), names)
        steps = next(f.value for f in e.fields if "はじめて" in f.name)
        check(f"mode={mode}: 手順が番号付き", "**1.**" in steps and "**2.**" in steps, steps[:60])
        text = (e.description or "") + "".join(f.value for f in e.fields)
        check(f"mode={mode}: 割引の例が載っている", "¥354" in text or "¥590" in text, text[:80])
    await settings.set_value("order_mode", "both")

    print("\n[2] 「注文する」を押したとき（方式ごと）")
    for mode, expect in [("both", "send_message"), ("hex", "send_modal"), ("menu", "send_message")]:
        await settings.set_value("order_mode", mode)
        itx = FakeInteraction(user, client)
        if await guarded(f"mode={mode} が例外なく動く", flows.start_order(itx)):
            check(f"mode={mode} → {expect}", itx.kinds[0] == expect, itx.kinds)
    await settings.set_value("order_mode", "both")

    print("\n[2b] アカウント未登録のときの案内")
    from sqlalchemy import update
    from db.models import McdAccount as _MA
    async with session_scope() as s:
        await s.execute(update(_MA).values(status="BANNED"))
    itx = FakeInteraction(user, client)
    await flows.start_order(itx)
    check("分かりやすい案内が出る", "受け付けできません" in itx.text(), itx.text()[:60])
    async with session_scope() as s:
        await s.execute(update(_MA).values(status="ACTIVE"))

    print("\n[2c] 決済カード未設定のときの案内")
    from sqlalchemy import update as _upd
    async with session_scope() as s:
        await s.execute(_upd(_MA).values(card_id=None))
    itx = FakeInteraction(user, client)
    await flows.start_order(itx)
    check("カード未設定でも案内が出る", "受け付けできません" in itx.text(), itx.text()[:60])
    async with session_scope() as s:
        await s.execute(_upd(_MA).values(card_id="c1"))
    itx = FakeInteraction(user, client)
    await flows.start_order(itx)
    check("カードを設定すれば注文できる", "受け付けできません" not in itx.text(), itx.text()[:60])

    print("\n[3] 残高の表示")
    async with user_scope(UID) as s:
        await L.charge(s, UID, 3000, receipt_id="ui-1")
    itx = FakeInteraction(user, client)
    if await guarded("残高が表示できる", flows.show_balance(itx)):
        check("¥3,000 が出る", "3,000" in itx.text(), itx.text()[:100])
        check("defer してから followup", itx.kinds == ["defer", "followup"], itx.kinds)

    print("\n[4] 履歴（注文ゼロのとき）")
    itx = FakeInteraction(user, client)
    if await guarded("履歴が表示できる", flows.show_history(itx)):
        check("「まだ注文履歴がありません」", "まだ注文履歴" in itx.text(), itx.text()[:80])

    print("\n[5] 壊れた注文コードを弾く")
    for bad, why in [("", "空"), ("zzzz", "16進数でない"), ("0a0531", "途中で切れている")]:
        itx = FakeInteraction(user, client)
        await itx.response.defer(ephemeral=True, thinking=True)
        if await guarded(f"{why}: 例外を出さない", flows.open_preview(itx, bad)):
            check(f"{why}: エラーとして返す", "読み取れません" in itx.text() or "含まれていません" in itx.text(),
                  itx.text()[:80])

    print("\n[6] 店舗選択の画面")
    itx = FakeInteraction(user, client)
    if await guarded("店舗選択が開ける", menu_flows.start_store_select(itx, "order")):
        v = itx.last_view()
        check("「店名でさがす」がある", press(v, "店名でさがす") is not None)
        check("「店舗IDで指定」がある", press(v, "店舗IDで指定") is not None)

    print("\n[7] 店名検索（インデックスあり）")
    idx = tempfile.mkdtemp() + "/stores.json"
    open(idx, "w").write(json.dumps({
        "13934": {"n": "南砂町店", "a": "東京都江東区新砂", "g": "group-f"},
        "11003": {"n": "所沢店", "a": "埼玉県所沢市", "g": "group-h"},
        "10001": {"n": "南砂second店", "a": "東京都江東区", "g": "group-f"},
    }, ensure_ascii=False))
    from pathlib import Path
    store_index.load_index(Path(idx))
    itx = FakeInteraction(user, client)
    await itx.response.defer(ephemeral=True, thinking=True)
    if await guarded("検索が動く", menu_flows.show_search_results(itx, "南砂", "order")):
        check("2件ヒットして選択肢が出る", itx.last_view() is not None, itx.kinds)
        if itx.last_view():
            sel = itx.last_view().children[0]
            check("候補が2件", len(sel.options) == 2, [o.label for o in sel.options])

    itx = FakeInteraction(user, client)
    await itx.response.defer(ephemeral=True, thinking=True)
    if await guarded("該当なしでも落ちない", menu_flows.show_search_results(itx, "存在しない店XYZ", "order")):
        check("見つからない旨を伝える", "見つかりません" in itx.text(), itx.text()[:80])

    print("\n[7b] インデックスが無いとき（過去に使った店舗から探す）")
    from pathlib import Path as _P
    from db.models import StoreCache
    store_index.load_index(_P("/nonexistent/none.json"))
    async with session_scope() as s:
        s.add(StoreCache(store_id="13934", group_name="group-f", store_name="南砂町店",
                         address="東京都江東区新砂", cat_root_url="x", hit_count=5))
    itx = FakeInteraction(user, client)
    await menu_flows.start_store_select(itx, "order")
    check("店舗IDでの指定を案内する", "店舗IDで指定" in itx.text(), itx.text()[:80])
    itx = FakeInteraction(user, client)
    await itx.response.defer(ephemeral=True, thinking=True)
    await menu_flows.show_search_results(itx, "南砂", "order")
    check("過去に使った店舗からは探せる", "見つかりません" not in itx.text(), itx.text()[:80])
    itx = FakeInteraction(user, client)
    await itx.response.defer(ephemeral=True, thinking=True)
    await menu_flows.show_search_results(itx, "ありえない店XYZ", "order")
    check("該当なしを伝える", "見つかりません" in itx.text(), itx.text()[:80])
    store_index.load_index(_P(idx))

    print("\n[8] チャージ画面")
    itx = FakeInteraction(user, client)
    if await guarded("チャージのモーダルが開く", flows.open_charge_modal(itx)):
        check("send_modal が呼ばれる", itx.kinds == ["send_modal"], itx.kinds)
        m = itx.last_modal()
        check("リンク入力欄がある", hasattr(m, "link"))

    print("\n[9] メンテナンス中・利用停止")
    await settings.set_value("maintenance", True)
    itx = FakeInteraction(user, client)
    ok_guard = await panels.guard_user(itx)
    check("メンテ中は弾かれる", ok_guard is False)
    check("メンテの案内が出る", "メンテナンス" in itx.text(), itx.text()[:60])
    await settings.set_value("maintenance", False)

    from db.models import User
    async with session_scope() as s:
        (await s.get(User, UID)).is_banned = True
    itx = FakeInteraction(user, client)
    check("利用停止は弾かれる", (await panels.guard_user(itx)) is False)
    check("停止の案内が出る", "停止" in itx.text(), itx.text()[:60])
    async with session_scope() as s:
        (await s.get(User, UID)).is_banned = False

    print("\n[10] 他人の操作を拒否する")
    other = FakeUser(9999, "別の人")
    view = flows.MethodView(UID)
    itx = FakeInteraction(other, client)
    check("本人以外は拒否", (await view.interaction_check(itx)) is False)
    check("理由を伝える", "本人" in itx.text(), itx.text()[:60])

    print("\n[11] 実績パネル（代理送信と同じ経路）")
    await settings.set_value("channel_achievement", 555)
    sent = await flows.send_achievement(
        client, discord_id=UID, display_name="テスト太郎",
        list_price=590, subsidy_rate=40.0, user_amount=354,
    )
    check("実績を送信できる", sent is True)
    check("チャンネルに届く", 555 in client.sent and len(client.sent[555]) == 1, client.sent.keys())

    print("\n[12] 管理パネルの権限")
    admin_view = panels.AdminPanel()
    itx = FakeInteraction(FakeUser(1), client)     # owner
    check("オーナーは通る", (await admin_view.interaction_check(itx)) is True)
    itx = FakeInteraction(FakeUser(4242), client)  # 一般
    check("一般利用者は弾かれる", (await admin_view.interaction_check(itx)) is False)
    check("権限エラーを伝える", "権限" in itx.text(), itx.text()[:60])

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
