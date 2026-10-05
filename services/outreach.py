"""
声かけ（ようこそ・途中離脱・残高のお知らせ）

あと一歩で使える人に、DMで1回だけ声をかける。

⚠️ **この機能の一番の失敗は「送りすぎ」。**
   しつこいDMは、使ってもらえないどころかサーバーごと抜けられる。
   そのため3つの歯止めを必ず通す。
     ① 利用者が止められる        … User.nudge_notify
     ② 同じことは二度言わない    … Nudge 表の一意制約
     ③ 一度に送る数を絞る        … NUDGE_BATCH

⚠️ DMを閉じている人には届かない。届かなかったことも記録して、
   毎回むだに試さないようにする。

⚠️ 「残高が余っています」の声かけは、**運営の持ち出しを増やさない**。
   すでに受け取った額の範囲で使ってもらうだけなので、
   他の施策より気兼ねなく使える。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import func, select

import config
from core import ledger as L
from core import settings
from db.models import Cart, KyashReceipt, Nudge, Order, PayPayReceipt, User, as_utc
from db.session import session_scope

log = logging.getLogger("bot.outreach")

# 声かけの種類
WELCOME = "welcome"
CART_LEFT = "cart_left"
CHARGED_UNUSED = "charged_unused"
IDLE_BALANCE = "idle_balance"

LABELS = {
    WELCOME: "ようこそ案内",
    CART_LEFT: "カートの残り",
    CHARGED_UNUSED: "チャージ後の未注文",
    IDLE_BALANCE: "残高のお知らせ",
}


def enabled(kind: str | None = None) -> bool:
    """声かけを行う設定になっているか。"""
    if not settings.get("nudge_enabled", False):
        return False
    if kind == WELCOME:
        return bool(settings.get("nudge_welcome", True))
    if kind == CART_LEFT:
        return int(settings.get("nudge_cart_minutes", 0) or 0) > 0
    if kind == CHARGED_UNUSED:
        return int(settings.get("nudge_charged_hours", 0) or 0) > 0
    if kind == IDLE_BALANCE:
        return int(settings.get("nudge_idle_days", 0) or 0) > 0
    return True


async def wants(discord_id: int) -> bool:
    """本人が声かけを受け取る設定か。"""
    async with session_scope() as s:
        row = await s.get(User, discord_id)
    # 登録が無い人は「まだ何も言っていない」ので受け取る扱い
    return True if row is None else bool(row.nudge_notify)


async def set_notify(discord_id: int, on: bool) -> bool:
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        if row is not None:
            row.nudge_notify = bool(on)
    return bool(on)


async def already_sent(discord_id: int, kind: str, key: str) -> bool:
    async with session_scope() as s:
        got = (await s.execute(
            select(Nudge.id).where(
                Nudge.discord_id == discord_id,
                Nudge.kind == kind,
                Nudge.key == str(key),
            )
        )).first()
    return got is not None


async def _record(discord_id: int, kind: str, key: str, *, delivered: bool) -> bool:
    """
    送ったことを残す。すでに同じ記録があれば False。

    ⚠️ 一意制約で守っているので、処理が重なっても二重に送らない。
    """
    from sqlalchemy.exc import IntegrityError

    try:
        async with session_scope() as s:
            s.add(Nudge(discord_id=discord_id, kind=kind, key=str(key),
                        delivered=delivered))
        return True
    except IntegrityError:
        return False


async def send(
    bot: discord.Client, discord_id: int, kind: str, key: str,
    embed: discord.Embed, *, view: discord.ui.View | None = None,
) -> bool:
    """
    1人に1回だけ声をかける。送れたら True。

    ⚠️ **記録を先に書く。** 送ってから書くと、送信の途中で落ちたときに
       同じ人へ何度も送ってしまう。届かなかった場合も記録は残す
       （DMを閉じている人を毎回試さないため）。
    """
    if not await wants(discord_id):
        return False
    if await already_sent(discord_id, kind, key):
        return False

    user = bot.get_user(discord_id)
    if user is None:
        try:
            user = await bot.fetch_user(discord_id)
        except (discord.NotFound, discord.HTTPException):
            return False

    # 先に記録を取る（ここで負けたら、他の処理がすでに送っている）
    if not await _record(discord_id, kind, key, delivered=True):
        return False

    try:
        await user.send(embed=embed, view=view) if view else await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        async with session_scope() as s:
            row = (await s.execute(
                select(Nudge).where(
                    Nudge.discord_id == discord_id, Nudge.kind == kind,
                    Nudge.key == str(key),
                )
            )).scalar_one_or_none()
            if row is not None:
                row.delivered = False
        log.debug("声かけが届きませんでした（%s / %s）", discord_id, kind)
        return False
    return True


# ------------------------------------------------------------
#  誰に声をかけるか
# ------------------------------------------------------------

async def cart_left_targets(limit: int) -> list[tuple[int, str, str]]:
    """
    カートを残したまま離れた方。(利用者ID, 鍵, 店名) を返す。

    ⚠️ カートが消える前に声をかける。消えたあとで「続きからどうぞ」と
       言っても続きが無く、かえって不親切になる。
    """
    mins = int(settings.get("nudge_cart_minutes", 0) or 0)
    if mins <= 0:
        return []
    now = datetime.now(timezone.utc)
    old_enough = now - timedelta(minutes=mins)
    # カートの有効時間を過ぎたものは対象にしない
    still_alive = now - timedelta(minutes=config.CART_RESUME_MINUTES)

    async with session_scope() as s:
        rows = list((await s.execute(select(Cart))).scalars().all())

    out = []
    for row in rows:
        updated = as_utc(row.updated_at)
        if updated is None or not (still_alive < updated <= old_enough):
            continue
        if not (row.items_json or "").strip("[] \n"):
            continue          # 空のカートに声をかけない
        # 鍵はカートの更新時刻。次に組み立て直せば、また声をかけられる。
        out.append((row.discord_id, updated.isoformat(timespec="seconds"),
                    row.store_id or ""))
        if len(out) >= limit:
            break
    return out


async def charged_unused_targets(limit: int) -> list[tuple[int, str, int]]:
    """
    チャージしたのに一度も注文していない方。(利用者ID, 鍵, 残高)。

    ⚠️ 「初めての1回」に辿り着いていない人が対象。すでに注文したことが
       ある人は、別の声かけ（残高のお知らせ）に任せる。

    ⚠️ **人数が増えても重くならないようにする。**
       全件を読んで Python 側で絞ると、利用者が増えたぶんだけ
       10分おきに重い処理が走ることになる。
       絞り込みはSQLで済ませ、残高の計算は候補だけに行う。
    """
    hours = int(settings.get("nudge_charged_hours", 0) or 0)
    if hours <= 0:
        return []
    cut = config.to_db(datetime.now(timezone.utc) - timedelta(hours=hours))

    async with session_scope() as s:
        # 一度でも注文した人は対象外。先に集めて除く。
        ordered = set((await s.execute(
            select(Order.discord_id).distinct()
        )).scalars().all())

        seen: dict[int, tuple[str, datetime]] = {}
        for model in (KyashReceipt, PayPayReceipt):
            rows = (await s.execute(
                select(model.discord_id, model.id, model.created_at)
                .where(model.status == "CREDITED", model.created_at <= cut)
                .order_by(model.created_at.desc())
            )).all()
            for uid, rid, created in rows:
                if uid in ordered:
                    continue
                when = as_utc(created)
                if when is None:
                    continue
                if uid not in seen or when > seen[uid][1]:
                    seen[uid] = (f"{model.__tablename__}:{rid}", when)

    async with session_scope() as s:
        balances = await L.user_balances(s, list(seen))

    out = []
    for uid, (key, _) in seen.items():
        bal = balances.get(uid, 0)
        if bal <= 0:
            continue
        out.append((uid, key, bal))
        if len(out) >= limit:
            break
    return out


async def idle_balance_targets(limit: int) -> list[tuple[int, str, int, int]]:
    """
    残高があるのに使っていない方。(利用者ID, 鍵, 残高, 何日ぶりか)。

    ⚠️ この声かけは運営の持ち出しを増やさない（預かっている額の範囲）。
    ⚠️ 同じ人に毎日言わない。鍵に「何回目か」を入れて間隔を空ける。
    """
    days = int(settings.get("nudge_idle_days", 0) or 0)
    if days <= 0:
        return []
    low = int(settings.get("nudge_idle_min_balance",
                           config.NUDGE_IDLE_MIN_BALANCE) or 0)
    now = datetime.now(timezone.utc)
    cut = config.to_db(now - timedelta(days=days))

    # ⚠️ **利用者全員に問い合わせない。**
    #    1人につき「最後の注文」と「残高」を引くと、人数×2 のクエリが
    #    10分おきに走る。最後の注文はSQLで一度にまとめ、
    #    残高は候補になった人の分だけ計算する。
    async with session_scope() as s:
        rows = (await s.execute(
            select(Order.discord_id, func.max(Order.created_at).label("last"))
            .group_by(Order.discord_id)
            .having(func.max(Order.created_at) < cut)
            .order_by(func.max(Order.created_at))
        )).all()
        banned = set((await s.execute(
            select(User.discord_id).where(User.is_banned.is_(True))
        )).scalars().all())

    candidates = [uid for uid, _ in rows if uid not in banned]
    async with session_scope() as s:
        balances = await L.user_balances(s, candidates)

    out = []
    for uid, last_raw in rows:
        if uid in banned:
            continue
        last = as_utc(last_raw)
        if last is None:
            continue
        bal = balances.get(uid, 0)
        if bal < low:
            continue
        # 鍵は「何回目の期間か」。NUDGE_IDLE_REPEAT_DAYS ごとに1つ進む。
        gone = (now - last).days
        bucket = int(gone // max(1, config.NUDGE_IDLE_REPEAT_DAYS))
        out.append((uid, f"b{bucket}", bal, gone))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------
#  実行
# ------------------------------------------------------------

async def run(bot: discord.Client) -> dict[str, int]:
    """
    定期処理から呼ぶ。送った件数を種類ごとに返す。

    ⚠️ 1種類が失敗しても、残りは続ける。
    """
    done = {k: 0 for k in LABELS}
    if not enabled():
        return done
    batch = max(1, int(config.NUDGE_BATCH))
    from ui import nudge_views

    if enabled(CART_LEFT):
        try:
            for uid, key, store in await cart_left_targets(batch):
                if await send(bot, uid, CART_LEFT, key,
                              nudge_views.cart_left_embed(store),
                              view=nudge_views.NudgeView()):
                    done[CART_LEFT] += 1
        except Exception:
            log.warning("カートの声かけに失敗しました", exc_info=True)

    if enabled(CHARGED_UNUSED):
        try:
            for uid, key, bal in await charged_unused_targets(batch):
                if await send(bot, uid, CHARGED_UNUSED, key,
                              nudge_views.charged_unused_embed(bal),
                              view=nudge_views.NudgeView()):
                    done[CHARGED_UNUSED] += 1
        except Exception:
            log.warning("チャージ後の声かけに失敗しました", exc_info=True)

    if enabled(IDLE_BALANCE):
        try:
            for uid, key, bal, gone in await idle_balance_targets(batch):
                if await send(bot, uid, IDLE_BALANCE, key,
                              nudge_views.idle_balance_embed(bal, gone),
                              view=nudge_views.NudgeView()):
                    done[IDLE_BALANCE] += 1
        except Exception:
            log.warning("残高の声かけに失敗しました", exc_info=True)

    total = sum(done.values())
    if total:
        log.info("声かけを %d 件送りました（%s）", total,
                 "／".join(f"{LABELS[k]} {n}" for k, n in done.items() if n))
    return done


async def welcome(bot: discord.Client, member: discord.Member) -> bool:
    """
    ようこそ案内。認証が済んだ直後に呼ぶ。

    ⚠️ 一生に1回だけ。入り直しても二度は送らない。
    """
    if not enabled(WELCOME):
        return False
    from ui import nudge_views

    return await send(
        bot, member.id, WELCOME, "once",
        nudge_views.welcome_embed(member.guild.name),
        view=nudge_views.NudgeView(),
    )


async def stats() -> dict[str, dict[str, int]]:
    """送った数と、届いた数。"""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Nudge.kind, Nudge.delivered, func.count())
            .group_by(Nudge.kind, Nudge.delivered)
        )).all()
    out: dict[str, dict[str, int]] = {}
    for kind, ok, n in rows:
        slot = out.setdefault(str(kind), {"sent": 0, "delivered": 0})
        slot["sent"] += int(n)
        if ok:
            slot["delivered"] += int(n)
    return out
