"""管理者コマンドの検証 — 実際に呼び出して例外が出ないか確かめる"""
import asyncio, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import discord
from _fake_discord import FakeInteraction, FakeUser, FakeClient
from core.crypto import init_cipher
from db.session import init_db, session_scope, user_scope, close_db
from core import ledger as L, settings, users as user_repo
from db.models import KyashAccount, McdAccount, SubsidyRule, utcnow

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

async def run(name, coro, expect=None):
    """コマンドを呼んで、例外が出ないことと応答があることを確かめる"""
    try:
        await coro
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        return None
    return True

class FakeRole:
    def __init__(self, rid, name): self.id = rid; self.name = name; self.mention = f"<@&{rid}>"

class FakeChoice:
    def __init__(self, value, name=None): self.value = value; self.name = name or value

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/a.db")
    await settings.load_all()

    from discord.ext import commands as dcommands
    from cogs.admin import AdminCog
    from cogs.config_cmd import ConfigCog
    from cogs.account import AccountCog
    from cogs.panel import PanelCog

    client = FakeClient()
    owner = FakeUser(1, "オーナー")
    admin_cog = AdminCog(client)
    config_cog = ConfigCog(client)
    account_cog = AccountCog(client)

    # 下ごしらえ
    async with session_scope() as s:
        s.add(McdAccount(id=1, label="mcd-1", email_enc=b"x", card_id="c1",
                         device_uid="d", wmop_device_id="w", fb_instance_id="f",
                         home_lat=35.0, home_lng=139.0))
        s.add(KyashAccount(id=1, label="kyash-1", email_enc=b"x", token_obtained_at=utcnow()))
    await user_repo.get_or_create(5001)
    async with user_scope(5001) as s:
        await L.charge(s, 5001, 5000, receipt_id="adm-1")

    print("\n[1] 統計まわり")
    for label, fn in [
        ("/stats show", lambda i: admin_cog.stats_show.callback(admin_cog, i)),
        ("/stats ledger", lambda i: admin_cog.stats_ledger.callback(admin_cog, i)),
        ("/stats account", lambda i: admin_cog.stats_account.callback(admin_cog, i)),
    ]:
        itx = FakeInteraction(owner, client)
        if await run(label, fn(itx)):
            check(f"{label} が応答する", bool(itx.actions), itx.kinds)

    print("\n[2] 設定の表示と変更")
    itx = FakeInteraction(owner, client)
    if await run("/config show", config_cog.show.callback(config_cog, itx)):
        check("設定一覧が出る", "設定" in itx.text(), itx.text()[:60])

    itx = FakeInteraction(owner, client)
    if await run("/config subsidy global", config_cog.subsidy_global.callback(config_cog, itx, 55.0)):
        check("負担率を変更できる", settings.get("subsidy_rate") == 55.0, settings.get("subsidy_rate"))
        check("利用者の支払い率を伝える", "45" in itx.text(), itx.text()[:80])
    await settings.set_value("subsidy_rate", 40.0)

    role = FakeRole(999, "VIP")
    itx = FakeInteraction(owner, client)
    if await run("/config subsidy role", config_cog.subsidy_role.callback(config_cog, itx, role, 70.0, 10, 5000)):
        async with session_scope() as s:
            from sqlalchemy import select
            rules = (await s.execute(select(SubsidyRule))).scalars().all()
        check("ロール別ルールが保存される", len(rules) == 1 and rules[0].target_id == 999, rules)

    itx = FakeInteraction(owner, client)
    if await run("/config subsidy list", config_cog.subsidy_list.callback(config_cog, itx)):
        check("ルール一覧が出る", "VIP" in itx.text() or "999" in itx.text(), itx.text()[:120])

    for label, fn, verify in [
        ("/config order_mode",
         lambda i: config_cog.order_mode.callback(config_cog, i, FakeChoice("menu", "メニューのみ")),
         lambda: settings.get("order_mode") == "menu"),
        ("/config maintenance",
         lambda i: config_cog.maintenance.callback(config_cog, i, True),
         lambda: settings.get("maintenance") is True),
        ("/config charge_limit",
         lambda i: config_cog.charge_limit.callback(config_cog, i, 200, 30000),
         lambda: settings.get("charge_min") == 200),
        ("/config menu interval",
         lambda i: config_cog.menu_interval.callback(config_cog, i, 10),
         lambda: settings.get("menu_sync_interval_minutes") == 10),
        ("/config menu store_refresh",
         lambda i: config_cog.store_refresh.callback(config_cog, i, 20),
         lambda: settings.get("store_refresh_minutes") == 20),
        ("/config balance_panel",
         lambda i: config_cog.balance_panel_fields.callback(
             config_cog, i, True, True, True, True, True, False),
         lambda: settings.get("balance_panel") is True
                 and settings.get("balance_panel_fields")
                     == ["name", "amount", "balance", "reason"]),
        ("/config balance_panel（OFF）",
         lambda i: config_cog.balance_panel_fields.callback(config_cog, i, False),
         lambda: settings.get("balance_panel") is False),
    ]:
        itx = FakeInteraction(owner, client)
        if await run(label, fn(itx)):
            check(f"{label} が反映される", verify(), label)
    await settings.set_value("order_mode", "both")
    await settings.set_value("maintenance", False)
    await settings.set_value("balance_panel", True)
    await settings.set_value("balance_panel_fields", config.BALANCE_PANEL_FIELDS_DEFAULT)

    print("\n[3] 利用者の管理")
    target = FakeUser(5001, "利用者A")
    itx = FakeInteraction(owner, client)
    if await run("/admin grant", admin_cog.grant.callback(admin_cog, itx, target, 1500, "テスト付与")):
        async with session_scope() as s:
            check("残高が増える（5000→6500）", await L.user_balance(s, 5001) == 6500,
                  await L.user_balance(s, 5001))
        pub = itx.public_embeds
        check("残高の増減パネルが公開で出る", len(pub) == 1, f"{len(pub)}件")
        if pub:
            body = (pub[0].title or "") + "".join(
                f"{f.name}{f.value}" for f in pub[0].fields)
            check("対象の利用者名で出る（操作した管理者ではない）",
                  "利用者A" in body and "テスト付与" in body, body[:160])
    itx = FakeInteraction(owner, client)
    if await run("/admin grant（減算）", admin_cog.grant.callback(admin_cog, itx, target, -500, "テスト減算")):
        async with session_scope() as s:
            check("残高が減る（6500→6000）", await L.user_balance(s, 5001) == 6000,
                  await L.user_balance(s, 5001))
    itx = FakeInteraction(owner, client)
    if await run("/admin grant（残高超過）", admin_cog.grant.callback(admin_cog, itx, target, -99999, "過剰減算")):
        check("残高を超える減算は拒否", "不足" in itx.text(), itx.text()[:80])

    for label, fn in [
        ("/admin ban", lambda i: admin_cog.ban.callback(admin_cog, i, target, "テスト")),
        ("/admin unban", lambda i: admin_cog.unban.callback(admin_cog, i, target)),
        ("/admin review", lambda i: admin_cog.review.callback(admin_cog, i)),
    ]:
        itx = FakeInteraction(owner, client)
        if await run(label, fn(itx)):
            check(f"{label} が応答する", bool(itx.actions), itx.kinds)

    print("\n[4] 代理実績")
    await settings.set_value("channel_achievement", 777)
    itx = FakeInteraction(owner, client)
    itx.guild = None
    if await run("/admin achievement", admin_cog.achievement.callback(
            admin_cog, itx, target, 590, None, None, None, None, None, None)):
        check("実績が送信される", 777 in client.sent, list(client.sent))
        check("完了を伝える", "送信しました" in itx.text(), itx.text()[:80])
    itx = FakeInteraction(owner, client)
    if await run("/admin achievement（不正な金額）", admin_cog.achievement.callback(
            admin_cog, itx, target, -100, None, None, None, None, None, None)):
        check("負の定価を拒否", "0円以上" in itx.text(), itx.text()[:80])

    print("\n[5] アカウント一覧")
    for label, fn in [
        ("/mcd list", lambda i: account_cog.mcd_list.callback(account_cog, i)),
        ("/kyash list", lambda i: account_cog.kyash_list.callback(account_cog, i)),
    ]:
        itx = FakeInteraction(owner, client)
        if await run(label, fn(itx)):
            check(f"{label} に登録済みが出る", "mcd-1" in itx.text() or "kyash-1" in itx.text(),
                  itx.text()[:100])

    itx = FakeInteraction(owner, client)
    if await run("/mcd enable", account_cog.mcd_enable.callback(account_cog, itx, 1)):
        check("有効化できる", "有効" in itx.text(), itx.text()[:60])
    itx = FakeInteraction(owner, client)
    if await run("/mcd enable（存在しないID）", account_cog.mcd_enable.callback(account_cog, itx, 999)):
        check("存在しないIDを弾く", "見つかりません" in itx.text(), itx.text()[:60])

    print("\n[6] 店舗一覧・調査")
    itx = FakeInteraction(owner, client)
    if await run("/store info", admin_cog.store_info.callback(admin_cog, itx)):
        check("店舗一覧の状態が出る", "店舗" in itx.text(), itx.text()[:60])
    itx = FakeInteraction(owner, client)
    if await run("/store search", admin_cog.store_search.callback(admin_cog, itx, "南砂")):
        check("検索結果が返る", bool(itx.actions), itx.kinds)

    HEX = ("0a0531333933343a020a00424e124c120439313830180120a006"
           "2a17080112073939383730303918012a0812043230323018012a26"
           "080112073939393739313818012a1708011207393939373931341801"
           "2a081204333132301801")
    itx = FakeInteraction(owner, client)
    if await run("/debug hex", admin_cog.debug_hex.callback(admin_cog, itx, HEX)):
        check("解析結果が出る", "13934" in itx.text(), itx.text()[:150])
    itx = FakeInteraction(owner, client)
    if await run("/debug hex（壊れたコード）", admin_cog.debug_hex.callback(admin_cog, itx, "zzzz")):
        check("壊れたコードを弾く", "解析できません" in itx.text(), itx.text()[:80])

    print("\n[7] 元帳の整合性")
    async with session_scope() as s:
        rep = await L.verify_integrity(s)
        check(f"全{rep.checked}取引で貸借一致", not rep.broken, rep.broken)
        check("マイナス残高なし", not rep.negative, rep.negative)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
