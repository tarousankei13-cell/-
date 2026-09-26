"""Friends and guilds. Small, explicit state machines — nothing implicit."""
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError, Forbidden, NotFound
from ..core.timeutil import utcnow
from ..models import Friendship, Guild, GuildMember, Item, Roll, User, WeeklyStats
from . import users as users_svc
from .engagement import week_key

MAX_FRIENDS = 100
MAX_PENDING = 20
GUILD_CREATE_COST = 10_000
GUILD_MAX_MEMBERS = 30


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------
async def _pair(db: AsyncSession, a: int, b: int) -> Friendship | None:
    return (await db.execute(select(Friendship).where(
        or_((Friendship.user_id == a) & (Friendship.friend_id == b),
            (Friendship.user_id == b) & (Friendship.friend_id == a))))).scalars().first()


async def request_friend(db: AsyncSession, me: int, other_id: int) -> dict[str, Any]:
    if me == other_id:
        raise AppError("自分自身には送れません", code="self")
    other = await db.get(User, other_id)
    if other is None or other.status == "banned":
        raise NotFound("ユーザーが見つかりません")
    existing = await _pair(db, me, other_id)
    if existing is not None:
        if existing.status == "accepted":
            raise AppError("既にフレンドです", code="already_friends")
        if existing.user_id == me:
            raise AppError("申請済みです", code="already_requested")
        # They asked first — treat my request as an accept.
        existing.status = "accepted"
        return {"ok": True, "accepted": True}
    accepted = (await db.execute(select(func.count()).select_from(Friendship).where(
        or_(Friendship.user_id == me, Friendship.friend_id == me), Friendship.status == "accepted"))).scalar_one()
    if accepted >= MAX_FRIENDS:
        raise AppError(f"フレンドは{MAX_FRIENDS}人までです", code="friend_limit")
    pending = (await db.execute(select(func.count()).select_from(Friendship).where(
        Friendship.user_id == me, Friendship.status == "pending"))).scalar_one()
    if pending >= MAX_PENDING:
        raise AppError("保留中の申請が多すぎます", code="pending_limit")
    db.add(Friendship(user_id=me, friend_id=other_id, status="pending"))
    from . import feed as feed_svc

    sender = await db.get(User, me)
    await feed_svc.notify_user(db, other_id, "friend_request", f"{sender.display_name} からフレンド申請が届きました")
    return {"ok": True, "accepted": False}


async def respond_friend(db: AsyncSession, me: int, other_id: int, accept: bool) -> dict[str, Any]:
    row = (await db.execute(select(Friendship).where(
        Friendship.user_id == other_id, Friendship.friend_id == me, Friendship.status == "pending"))).scalars().first()
    if row is None:
        raise NotFound("申請が見つかりません")
    if accept:
        row.status = "accepted"
    else:
        await db.delete(row)
    return {"ok": True}


async def remove_friend(db: AsyncSession, me: int, other_id: int) -> dict[str, Any]:
    row = await _pair(db, me, other_id)
    if row is None:
        raise NotFound("フレンドが見つかりません")
    await db.delete(row)
    return {"ok": True}


async def friend_ids(db: AsyncSession, me: int) -> list[int]:
    rows = (await db.execute(select(Friendship).where(
        or_(Friendship.user_id == me, Friendship.friend_id == me), Friendship.status == "accepted"))).scalars().all()
    return [r.friend_id if r.user_id == me else r.user_id for r in rows]


async def friend_list(db: AsyncSession, me: int) -> dict[str, Any]:
    now = utcnow()
    ids = await friend_ids(db, me)
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()} if ids else {}
    online_cut = now - timedelta(seconds=90)
    friends = sorted(
        [{**users_svc.user_brief(u), "online": bool(u.last_seen_at and u.last_seen_at > online_cut),
          "last_seen": u.last_seen_at.isoformat() if u.last_seen_at else None} for u in users.values()],
        key=lambda x: (not x["online"], -(x["level"] or 0)))
    inc = (await db.execute(select(Friendship, User).join(User, User.id == Friendship.user_id)
                            .where(Friendship.friend_id == me, Friendship.status == "pending"))).all()
    out = (await db.execute(select(Friendship, User).join(User, User.id == Friendship.friend_id)
                            .where(Friendship.user_id == me, Friendship.status == "pending"))).all()
    return {
        "friends": friends,
        "incoming": [users_svc.user_brief(u) for _, u in inc],
        "outgoing": [users_svc.user_brief(u) for _, u in out],
    }


async def friend_activity(db: AsyncSession, me: int, limit: int = 30) -> list[dict[str, Any]]:
    """Friends' recent finds, tier 4 and up — the feed that makes friends fun."""
    ids = await friend_ids(db, me)
    if not ids:
        return []
    rows = (await db.execute(
        select(Roll, User, Item).join(User, User.id == Roll.user_id).join(Item, Item.id == Roll.item_id)
        .where(Roll.user_id.in_(ids), Roll.tier >= 4)
        .order_by(Roll.id.desc()).limit(limit))).all()
    return [{
        "player": users_svc.user_brief(u), "at": r.created_at.isoformat(),
        "item": {"name": it.name, "name_ja": it.name_ja, "rarity": it.rarity_key, "odds": it.odds, "visual": it.visual or {}},
    } for r, u, it in rows]


# ---------------------------------------------------------------------------
# Guilds
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[0-9A-Za-zぁ-んァ-ヶ一-龠ー・\s]{2,24}$")
_TAG_RE = re.compile(r"^[0-9A-Z]{2,5}$")


async def my_membership(db: AsyncSession, user_id: int) -> GuildMember | None:
    return await db.get(GuildMember, user_id)


async def create_guild(db: AsyncSession, user_id: int, name: str, tag: str, description: str = "") -> dict[str, Any]:
    name, tag = name.strip(), tag.strip().upper()
    if not _NAME_RE.match(name):
        raise AppError("ギルド名は2〜24文字（記号不可）です", code="invalid_name")
    if not _TAG_RE.match(tag):
        raise AppError("タグは英数字2〜5文字です", code="invalid_tag")
    if await my_membership(db, user_id) is not None:
        raise AppError("既にギルドに所属しています", code="already_in_guild")
    user = await users_svc.lock_user(db, user_id)
    if user.stardust < GUILD_CREATE_COST:
        raise AppError(f"設立には ✦{GUILD_CREATE_COST:,} が必要です", code="insufficient_funds")
    dup = (await db.execute(select(Guild).where(or_(Guild.name == name, Guild.tag == tag)))).scalars().first()
    if dup is not None:
        raise AppError("その名前またはタグは使われています", code="name_taken")
    user.stardust -= GUILD_CREATE_COST
    g = Guild(name=name, tag=tag, description=description[:140], owner_id=user_id)
    db.add(g)
    await db.flush()
    db.add(GuildMember(user_id=user_id, guild_id=g.id, role="owner"))
    _online_cache.pop(g.id, None)
    return {"ok": True, "guild_id": g.id, "stardust": user.stardust}


async def join_guild(db: AsyncSession, user_id: int, guild_id: int) -> dict[str, Any]:
    if await my_membership(db, user_id) is not None:
        raise AppError("既にギルドに所属しています", code="already_in_guild")
    g = await db.get(Guild, guild_id)
    if g is None:
        raise NotFound("ギルドが見つかりません")
    if not g.is_open:
        raise Forbidden("このギルドは募集停止中です", code="closed")
    n = (await db.execute(select(func.count()).select_from(GuildMember).where(GuildMember.guild_id == guild_id))).scalar_one()
    if n >= GUILD_MAX_MEMBERS:
        raise AppError("定員に達しています", code="guild_full")
    db.add(GuildMember(user_id=user_id, guild_id=guild_id, role="member"))
    _online_cache.pop(guild_id, None)
    return {"ok": True}


async def leave_guild(db: AsyncSession, user_id: int) -> dict[str, Any]:
    m = await my_membership(db, user_id)
    if m is None:
        raise NotFound("ギルドに所属していません")
    gid = m.guild_id
    await db.delete(m)
    await db.flush()
    g = await db.get(Guild, gid)
    if g and g.owner_id == user_id:
        rest = (await db.execute(select(GuildMember).where(GuildMember.guild_id == gid)
                                 .order_by(GuildMember.joined_at).limit(1))).scalars().first()
        if rest is None:
            await db.delete(g)
        else:
            g.owner_id = rest.user_id
            rest.role = "owner"
    _online_cache.pop(gid, None)
    return {"ok": True}


async def kick_member(db: AsyncSession, owner_id: int, target_id: int) -> dict[str, Any]:
    m = await my_membership(db, owner_id)
    if m is None or m.role != "owner":
        raise Forbidden("オーナーのみ実行できます", code="not_owner")
    if target_id == owner_id:
        raise AppError("自分は除名できません（脱退を使ってください）", code="self")
    t = await my_membership(db, target_id)
    if t is None or t.guild_id != m.guild_id:
        raise NotFound("メンバーが見つかりません")
    await db.delete(t)
    _online_cache.pop(m.guild_id, None)
    return {"ok": True}


async def _guild_public(db: AsyncSession, g: Guild, *, with_members: bool) -> dict[str, Any]:
    wk = week_key()
    n = (await db.execute(select(func.count()).select_from(GuildMember).where(GuildMember.guild_id == g.id))).scalar_one()
    weekly = int((await db.execute(
        select(func.coalesce(func.sum(WeeklyStats.rolls), 0)).select_from(GuildMember)
        .join(WeeklyStats, (WeeklyStats.user_id == GuildMember.user_id) & (WeeklyStats.week == wk))
        .where(GuildMember.guild_id == g.id))).scalar_one())
    out: dict[str, Any] = {"id": g.id, "name": g.name, "tag": g.tag, "description": g.description,
                           "owner_id": g.owner_id, "is_open": g.is_open, "members": int(n),
                           "max_members": GUILD_MAX_MEMBERS, "weekly_rolls": weekly}
    if with_members:
        cut = utcnow() - timedelta(seconds=90)
        rows = (await db.execute(
            select(User, GuildMember, WeeklyStats)
            .join(GuildMember, GuildMember.user_id == User.id)
            .outerjoin(WeeklyStats, (WeeklyStats.user_id == User.id) & (WeeklyStats.week == wk))
            .where(GuildMember.guild_id == g.id).order_by(GuildMember.joined_at))).all()
        out["member_list"] = [{**users_svc.user_brief(u), "role": gm.role,
                               "online": bool(u.last_seen_at and u.last_seen_at > cut),
                               "weekly_rolls": int(ws.rolls) if ws else 0} for u, gm, ws in rows]
        out["online_now"] = sum(1 for m in out["member_list"] if m["online"])
    return out


async def my_guild(db: AsyncSession, user_id: int) -> dict[str, Any]:
    m = await my_membership(db, user_id)
    if m is None:
        return {"guild": None, "bonus": bonus_terms()}
    g = await db.get(Guild, m.guild_id)
    if g is None:
        await db.delete(m)
        return {"guild": None, "bonus": bonus_terms()}
    return {"guild": await _guild_public(db, g, with_members=True), "role": m.role, "bonus": bonus_terms()}


async def list_guilds(db: AsyncSession, q: str = "", limit: int = 30) -> list[dict[str, Any]]:
    stmt = select(Guild).order_by(Guild.id.desc()).limit(limit)
    if q.strip():
        from .inventory import _escape_like  # noqa: SLF001

        stmt = select(Guild).where(Guild.name.ilike(f"%{_escape_like(q.strip())}%", escape="\\")).limit(limit)
    gs = (await db.execute(stmt)).scalars().all()
    return [await _guild_public(db, g, with_members=False) for g in gs]


async def guild_board(db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    wk = week_key()
    rows = (await db.execute(
        select(Guild, func.coalesce(func.sum(WeeklyStats.rolls), 0).label("wr"),
               func.count(GuildMember.user_id).label("n"))
        .join(GuildMember, GuildMember.guild_id == Guild.id)
        .outerjoin(WeeklyStats, (WeeklyStats.user_id == GuildMember.user_id) & (WeeklyStats.week == wk))
        .group_by(Guild.id).order_by(func.coalesce(func.sum(WeeklyStats.rolls), 0).desc()).limit(limit))).all()
    return [{"rank": i, "id": g.id, "name": g.name, "tag": g.tag, "members": int(n), "weekly_rolls": int(wr)}
            for i, (g, wr, n) in enumerate(rows, start=1)]


# --- the play-together bonus ------------------------------------------------
# +2% Luck per guild member online at the same time (self included), from the
# second member up to five. Capped small on purpose: a nudge to play together,
# not a tax on playing alone.
BONUS_PER_MEMBER = 0.02
BONUS_MIN_ONLINE = 2
BONUS_CAP_MEMBERS = 5

_online_cache: dict[int, tuple[float, int]] = {}


def bonus_terms() -> dict[str, Any]:
    return {"per_member": BONUS_PER_MEMBER, "min_online": BONUS_MIN_ONLINE, "cap": BONUS_CAP_MEMBERS}


async def guild_luck(db: AsyncSession, user_id: int) -> tuple[float, int]:
    """(multiplier, online_count) for the roller's guild; cached ~30s per guild."""
    m = await my_membership(db, user_id)
    if m is None:
        return 1.0, 0
    now = time.monotonic()
    hit = _online_cache.get(m.guild_id)
    if hit is not None and now - hit[0] < 30:
        online = hit[1]
    else:
        cut = utcnow() - timedelta(seconds=90)
        online = int((await db.execute(
            select(func.count()).select_from(GuildMember).join(User, User.id == GuildMember.user_id)
            .where(GuildMember.guild_id == m.guild_id, User.last_seen_at > cut))).scalar_one())
        _online_cache[m.guild_id] = (now, online)
    if online < BONUS_MIN_ONLINE:
        return 1.0, online
    return 1.0 + BONUS_PER_MEMBER * min(online, BONUS_CAP_MEMBERS), online
