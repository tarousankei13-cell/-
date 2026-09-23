"""Discord DM notifications for administrators (rare drops, first discoveries, market alerts).

Messages are written to a DB outbox inside the same transaction as the game
event (so nothing is lost or sent for rolled-back work) and delivered by the
leader worker with retries and Discord rate-limit handling.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..content.registry import get_registry
from ..core.timeutil import utcnow
from ..models import DiscordOutbox
from .icons import render_icon

log = logging.getLogger("cosmic.discord")

_dm_channels: dict[str, str] = {}


def _enabled() -> bool:
    return bool(get_registry().setting("discord.enabled")) and bool(get_settings().discord_bot_token)


def _targets() -> list[str]:
    raw = get_registry().setting("discord.dm_targets") or []
    return [str(t) for t in raw if str(t).isdigit()][:20]


async def enqueue_drop(db: AsyncSession, *, player: str, item: dict[str, Any], odds: float | None, final_luck: float,
                       biome: str, first_discovery: bool, obtained_at: str, serial: int | None) -> None:
    if not _enabled() or not _targets():
        return
    reg = get_registry()
    snap = reg.snap
    min_tier = snap.tier_of(str(reg.setting("discord.min_tier") or "secret"))
    if item.get("tier", 1) < min_tier and not (first_discovery and reg.setting("discord.first_discovery_always")):
        return
    db.add(DiscordOutbox(kind="drop", payload={
        "player": player, "item": {k: item.get(k) for k in ("id", "name", "rarity", "tier", "visual", "display_odds")},
        "odds": odds, "luck": final_luck, "biome": biome, "first": first_discovery, "time": obtained_at, "serial": serial,
    }))


async def enqueue_alert(db: AsyncSession, title: str, body: str) -> None:
    if not _enabled() or not _targets() or not get_registry().setting("discord.market_alerts"):
        return
    db.add(DiscordOutbox(kind="alert", payload={"title": title[:200], "body": body[:1500]}))


def build_embed(row: DiscordOutbox) -> tuple[dict[str, Any], bytes | None]:
    snap = get_registry().snap
    fields_cfg = get_registry().setting("discord.fields") or {}
    p = row.payload
    if row.kind == "alert":
        return {"title": f"⚠ {p['title']}", "description": p["body"], "color": 0xFF5C5C}, None
    item = p["item"]
    rar = snap.rarities.get(item.get("rarity") or "common")
    color = int((rar.color if rar else "#ffffff").lstrip("#"), 16)
    title = ("🌟 FIRST DISCOVERY — " if p.get("first") else "✦ ") + str(item.get("name"))
    fields = []
    if fields_cfg.get("player", True):
        fields.append({"name": "Player", "value": str(p["player"])[:100], "inline": True})
    if fields_cfg.get("rarity", True):
        fields.append({"name": "Rarity", "value": rar.name if rar else str(item.get("rarity")), "inline": True})
    if fields_cfg.get("odds", True):
        odds = item.get("display_odds") or (f"1 / {p['odds']:,.0f}" if p.get("odds") else "—")
        fields.append({"name": "Probability", "value": odds, "inline": True})
    if fields_cfg.get("biome", True):
        fields.append({"name": "Biome", "value": str(p["biome"]), "inline": True})
    if fields_cfg.get("luck", True):
        fields.append({"name": "Final Luck", "value": f"×{p['luck']:,.2f}", "inline": True})
    if p.get("serial"):
        fields.append({"name": "Serial", "value": f"#{p['serial']}", "inline": True})
    if fields_cfg.get("first", True) and p.get("first"):
        fields.append({"name": "World Discovery", "value": "世界初発見！", "inline": False})
    embed: dict[str, Any] = {"title": title[:250], "color": color, "fields": fields}
    if fields_cfg.get("time", True):
        embed["timestamp"] = p.get("time")
    image = None
    if fields_cfg.get("image", True):
        image = render_icon(item.get("visual") or {}, rar.color if rar else "#fff", rar.color2 if rar else "#888")
        embed["thumbnail"] = {"url": "attachment://item.png"}
    return embed, image


async def _dm_channel(client: httpx.AsyncClient, user_id: str) -> str:
    if user_id in _dm_channels:
        return _dm_channels[user_id]
    r = await client.post("/users/@me/channels", json={"recipient_id": user_id})
    r.raise_for_status()
    cid = str(r.json()["id"])
    _dm_channels[user_id] = cid
    return cid


async def process_outbox(db: AsyncSession, batch: int = 10) -> int:
    s = get_settings()
    now = utcnow()
    rows = (await db.execute(
        select(DiscordOutbox).where(DiscordOutbox.status == "pending", DiscordOutbox.next_attempt_at <= now)
        .order_by(DiscordOutbox.id).limit(batch).with_for_update(skip_locked=True)
    )).scalars().all()
    if not rows:
        return 0
    if not s.discord_bot_token:
        for r in rows:
            r.status = "skipped"
            r.last_error = "DISCORD_BOT_TOKEN not configured"
        await db.commit()
        return 0
    headers = {"Authorization": f"Bot {s.discord_bot_token}", "User-Agent": "CosmicRNG (https://github.com, 1.0)"}
    sent = 0
    async with httpx.AsyncClient(base_url=s.discord_api_base, headers=headers, timeout=10) as client:
        for row in rows:
            try:
                embed, image = build_embed(row)
                for target in _targets():
                    cid = await _dm_channel(client, target)
                    payload = {"embeds": [embed]}
                    if image:
                        files = {"files[0]": ("item.png", image, "image/png")}
                        resp = await client.post(f"/channels/{cid}/messages", data={"payload_json": json.dumps(payload)}, files=files)
                    else:
                        resp = await client.post(f"/channels/{cid}/messages", json=payload)
                    if resp.status_code == 429:
                        retry = float(resp.json().get("retry_after", 5))
                        raise RuntimeError(f"rate limited retry_after={retry}")
                    resp.raise_for_status()
                row.status = "sent"
                row.sent_at = utcnow()
                sent += 1
            except Exception as e:  # retry with backoff; never crash the worker
                row.attempts += 1
                row.last_error = str(e)[:500]
                if row.attempts >= 6:
                    row.status = "failed"
                    log.warning("discord outbox %s failed permanently: %s", row.id, e)
                else:
                    row.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 10 * 2 ** row.attempts))
    await db.commit()
    return sent
