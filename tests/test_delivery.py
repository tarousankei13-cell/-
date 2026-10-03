"""
控えの届け方の検証

注文が成立したあと、利用者に注文番号が確実に届くかを確かめる。
DMが閉じていても番号が届かないと店頭で受け取れないので、
ここは落とせない経路。
"""
import asyncio, os, sys, tempfile, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from core import ledger as L, saga, settings
from core.subsidy import Quote, calculate
from services.mcd import accounts as mcd_accounts, stores as mcd_stores
from services.mcd.protocol import DecodedOrder, OrderItem
from ui import flows
from _fake_discord import FakeInteraction, FakeUser, FakeClient as FakeBot

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


DECODED = DecodedOrder(
    store_id="13934", pickup_method="eatIn",
    items=[OrderItem("9180", amount=800)], raw_hex="0a0531333933",
)


class FakeMcd:
    """注文は必ず通る偽物。注文番号は指定できる。"""
    def __init__(self, number="7161"):
        self.number = number
    async def ensure_auth(self, force=False): pass
    async def get_pos_paseto(self, group): return "v2.local.fake"
    async def store_order(self, group, body):
        from services.mcd.client import OrderResponse
        return OrderResponse(order_code="OC1", order_token="T1")
    async def authorise_order(self, group, token):
        from services.mcd.client import OrderResponse
        return OrderResponse(order_code="OC1", display_order_number=self.number)
    async def aclose(self): pass


class FakeHandle:
    def __init__(self, client):
        self.account_id, self.label, self.card_id, self.client = 1, "test", "card", client
    async def aclose(self): pass


CURRENT = {"client": None}

def install_mocks():
    async def pick(exclude=None): return FakeHandle(CURRENT["client"])
    async def open_(account_id): return FakeHandle(CURRENT["client"])
    async def noop(*a, **k): pass
    async def failure(aid, err, *, fatal=False): return "ACTIVE"
    mcd_accounts.pick_account = pick
    mcd_accounts.open_account = open_
    mcd_accounts.report_success = noop
    mcd_accounts.report_failure = failure
    mcd_stores.ensure_menu_fresh = noop
    saga.mcd_accounts = mcd_accounts


async def give(uid, receipt_id, amount=5000):
    """テスト用に残高を積む。"""
    async with user_scope(uid) as s:
        await L.charge(s, uid, amount, receipt_id=receipt_id)


async def bal(uid):
    async with session_scope() as s:
        return await L.user_balance(s, uid)


def quote(price=800, rate=40.0):
    u, sub = calculate(price, rate)
    return Quote(list_price=price, subsidy_rate=rate, user_amount=u,
                 subsidy_amount=sub, source="test")


async def place(user, bot, number="7161", price=800):
    """注文を1件通して、そのときのやりとりを返す。"""
    CURRENT["client"] = FakeMcd(number)
    itx = FakeInteraction(user, bot)
    await itx.response.defer(ephemeral=True, thinking=True)
    await flows.run_order(
        itx, DECODED, quote(price), "eatIn", "テスト店", str(uuid.uuid4())
    )
    return itx


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/delivery.db")
    await settings.load_all()
    install_mocks()

    from db.models import McdAccount, StoreCache
    async with session_scope() as s:
        s.add(McdAccount(id=1, label="test", email_enc=b"x", status="ACTIVE",
                         card_id="card", device_uid="d", wmop_device_id="w",
                         fb_instance_id="f", home_lat=35.0, home_lng=139.0))
        s.add(StoreCache(store_id="13934", group_name="group-f", store_name="テスト店",
                         address="東京都", cat_root_url="https://example.invalid"))

    bot = FakeBot()

    print("\n[1] DMが開いているとき")
    user = FakeUser(5001)
    await give(user.id, "r1")
    itx = await place(user, bot, number="7161")
    check("DMが1通届く", len(user.dms) == 1, f"{len(user.dms)}通")
    dm = user.dms[0] if user.dms else {}
    dm_e = dm.get("embed")
    check("DMに注文番号が入っている", dm_e is not None and "7161" in str([f.value for f in dm_e.fields]))
    check("DMに控え画像が付いている", dm.get("file") is not None)
    check("DMに受け取り画面のボタンが付いている",
          dm.get("view") is not None and len(dm["view"].children) == 1)
    check("その場の案内はDMを見るよう促す", "DM" in itx.text(), itx.text()[:120])
    last = itx.last_embed()
    check("最後の表示は完了の案内", last is not None and "完了" in (last.description or "") + (last.title or ""),
          str(last.description)[:120] if last else "なし")
    check("DMが届いたので、その場には控えを重ねて出さない",
          not [kw for _, kw in itx.actions if kw.get("file")])

    print("\n[2] DMを拒否しているとき")
    user2 = FakeUser(5002)
    user2.dm_ok = False
    await give(user2.id, "r2")
    itx2 = await place(user2, bot, number="8422")
    check("DMは届かない", len(user2.dms) == 0)
    check("送信できなかったことを伝える", "DMを送信できませんでした" in itx2.text(), itx2.text()[:160])
    check("その場に注文番号が出る", "8422" in itx2.text(), itx2.text()[:200])
    files = [kw.get("file") for _, kw in itx2.actions if kw.get("file")]
    check("その場に控え画像が出る", len(files) == 1, f"{len(files)}枚")
    views = [kw.get("view") for _, kw in itx2.actions if kw.get("view")]
    check("その場に受け取り画面のボタンが出る", any(len(v.children) == 1 for v in views))
    check("本人にだけ見える形で出す",
          all(kw.get("ephemeral", True) for k, kw in itx2.actions if k == "followup"))
    check("注文自体は成立している（残高が減っている）",
          await bal(user2.id) == 5000 - quote().user_amount,
          str(await bal(user2.id)))

    print("\n[3] 実績チャンネルへの投稿")
    await settings.set_value("channel_achievement", "9999")
    bot.sent[9999] = []
    user3 = FakeUser(5003)
    await give(user3.id, "r-" + str(user3.id))
    await place(user3, bot, number="1234")
    posts = [kw["embed"] for kw in bot.sent.get(9999, [])]
    check("実績が1件投稿される", len(posts) == 1, f"{len(posts)}件")
    if posts:
        e = posts[0]
        body = (e.title or "") + (e.description or "") + "".join(
            f"{f.name}{f.value}" for f in e.fields)
        check("実績に個人が特定できる情報を載せない",
              "5003" not in body and "テスト太郎" not in body, body[:160])

    print("\n[4] 番号の桁数が変わっても届く")
    for num in ["7", "71610"]:
        u = FakeUser(5100 + len(num))
        await give(u.id, "r-" + str(u.id))
        await place(u, bot, number=num)
        got = u.dms[0]["embed"] if u.dms else None
        check(f"{len(num)}桁の番号が正しく届く",
              got is not None and num in str([f.value for f in got.fields]), num)

    print("\n[4.5] 注文では残高パネルを出さない ★")
    # ⚠️ 公開パネルを出すのは、管理者が /admin grant で手で動かしたときだけ。
    #    利用者が自分で使ったぶんまで公開されるのは望まれていない。
    await settings.set_value("balance_panel", True)
    user45 = FakeUser(5045)
    await give(user45.id, "r-" + str(user45.id))
    itx45 = await place(user45, bot, number="4545", price=800)
    check("注文では公開パネルを出さない ★", len(itx45.public_embeds) == 0,
          str(itx45.public_embeds)[:120])
    check("DMの控えは届く", len(user45.dms) == 1, f"{len(user45.dms)}通")

    print("\n[5] DMが閉じていても実績は出る")
    await settings.set_value("channel_achievement", "9999")
    bot.sent[9999] = []
    u5 = FakeUser(5005); u5.dm_ok = False
    await give(u5.id, "r-" + str(u5.id))
    await place(u5, bot, number="5555")
    check("DM拒否でも実績は投稿される", len(bot.sent.get(9999, [])) == 1)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
