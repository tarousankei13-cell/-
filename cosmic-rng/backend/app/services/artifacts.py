"""Admin Artifacts: usage (effects that bend the game's rules), granting and recalling.

Only administrators (in Admin Mode) acquire or grant artifacts. A player who
was granted an artifact can use it only when the artifact is marked
``player_usable`` *and* the grant explicitly allows use. Artifacts can never
be sold, traded, gifted or listed. Every grant, recall and use is audited.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import bump_content_version, get_registry
from ..core.errors import AppError, Forbidden, NotFound
from ..core.pubsub import queue_event
from ..core.security import Principal
from ..core.timeutil import utcnow
from ..models import ActiveEffect, AdminGrant, ArtifactCooldown, GameEvent, ItemInstance, User
from ..rng.engine import system_rng
from ..rng.procedural import generate
from . import audit
from . import biomes as biomes_svc
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import feed as feed_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import users as users_svc
from .constants import TIER_BY_KEY


def artifact_public(art: dict[str, Any]) -> dict[str, Any]:
    snap = get_registry().snap
    item = snap.items.get(art["item_id"])
    return {
        "key": art["key"], "item_id": art["item_id"], "name": item.name if item else art["key"],
        "name_ja": item.name_ja if item else "", "lore": item.lore if item else "",
        "ability": art["ability"], "theme": art["theme"], "tier": art["tier"], "effect": art["effect"], "target": art["target"],
        "duration_sec": art["duration_sec"], "cooldown_sec": art["cooldown_sec"], "player_usable": art["player_usable"],
        "equip_passive": art["equip_passive"], "transfer_rules": art["transfer_rules"], "audit_rules": art["audit_rules"],
        "visual": item.visual if item else {}, "animation": item.animation if item else None, "sound": item.sound if item else None,
        "is_active": art["is_active"],
    }


async def _active_grant(db: AsyncSession, instance_id: int) -> AdminGrant | None:
    g = (await db.execute(select(AdminGrant).where(AdminGrant.instance_id == instance_id, AdminGrant.action == "grant")
                          .order_by(AdminGrant.id.desc()).limit(1).with_for_update())).scalar_one_or_none()
    if g is None or not g.can_use:
        return None
    if g.expires_at is not None and g.expires_at <= utcnow():
        return None
    if g.uses_remaining is not None and g.uses_remaining <= 0:
        return None
    return g


async def use(db: AsyncSession, principal: Principal, instance_id: int, params: dict[str, Any], user_agent: str | None = None) -> dict[str, Any]:
    snap = get_registry().snap
    user = await users_svc.lock_user(db, principal.user.id)
    inst = (await db.execute(select(ItemInstance).where(ItemInstance.id == instance_id, ItemInstance.owner_id == user.id)
                             .with_for_update())).scalar_one_or_none()
    if inst is None:
        raise NotFound("アイテムが見つかりません")
    art = snap.artifacts_by_item.get(inst.item_id)
    if art is None:
        raise AppError("Admin Artifactではありません", code="not_artifact")
    if not art["is_active"]:
        raise Forbidden("このArtifactは現在封印されています", code="artifact_disabled")
    admin_use = principal.admin_mode
    grant = None
    if not admin_use:
        if not art["player_usable"]:
            raise Forbidden("このArtifactは管理者のみ使用できます", code="admin_only")
        grant = await _active_grant(db, inst.id)
        if grant is None:
            raise Forbidden("このArtifactの使用権限がありません", code="no_use_permission")
    now = utcnow()
    cd = await db.get(ArtifactCooldown, (user.id, art["key"]))
    if not admin_use and cd and cd.ready_at > now:
        raise AppError("クールダウン中です", code="artifact_cooldown", status_code=429,
                       data={"ready_at": cd.ready_at.isoformat()})
    effect = dict(art["effect"] or {})
    result = await _apply(db, principal, user, art, effect, params, admin_use)
    if cd is None:
        db.add(ArtifactCooldown(user_id=user.id, artifact_key=art["key"], ready_at=now + timedelta(seconds=art["cooldown_sec"]), uses=1))
    else:
        cd.ready_at = now + timedelta(seconds=art["cooldown_sec"])
        cd.uses += 1
    if grant is not None and grant.uses_remaining is not None:
        grant.uses_remaining -= 1
    stats = await users_svc.lock_stats(db, user.id)
    stats.artifacts_used += 1
    await audit.record(db, principal, "artifact_use", target_user_id=result.get("target_user_id", user.id), entity_type="artifact",
                       entity_id=art["key"], new={"params": params, "result": {k: v for k, v in result.items() if k != "burst"}},
                       reason=str(params.get("reason", ""))[:500], user_agent=user_agent)
    item = snap.items.get(art["item_id"])
    cinematic = {"artifact": art["key"], "name": item.name if item else art["key"],
                 "name_ja": item.name_ja if item else "", "theme": art["theme"], "tier": art["tier"],
                 "visual": item.visual if item else {}, "user": users_svc.user_brief(user), "target": art["target"]}
    if art["target"] == "global":
        queue_event(db, "all", "admin_event", {"kind": "artifact", **cinematic, "result": result.get("public")})
        await feed_svc.add_world_event(db, "admin_artifact", user, {"artifact": art["key"], "name": cinematic["name"],
                                                                     "theme": art["theme"], "ability": art["ability"]}, True)
    if art["audit_rules"].get("notify_admins"):
        await feed_svc.notify_admins(db, "artifact_use", f"{user.display_name} が {cinematic['name']} を使用",
                                     art["ability"], {"artifact": art["key"], "user_id": user.id, "admin_use": admin_use})
    return {"ok": True, "cinematic": cinematic, "result": result, "stardust": user.stardust}


async def _apply(db: AsyncSession, principal: Principal, user: User, art: dict[str, Any], effect: dict[str, Any],
                 params: dict[str, Any], admin_use: bool) -> dict[str, Any]:
    snap = get_registry().snap
    t = effect.get("type")
    duration = art.get("duration_sec")
    item = snap.items.get(art["item_id"])
    name = item.name if item else art["key"]

    async def add(uid: int, etype: str, **kw: Any) -> ActiveEffect:
        return await effects_svc.add_effect(db, uid, source_type="artifact", source_key=art["key"], name=name, effect_type=etype,
                                            granted_by=principal.user.id, **kw)

    if t == "multi":
        out: dict[str, Any] = {"parts": []}
        for sub in effect.get("effects", []):
            out["parts"].append(await _apply(db, principal, user, art, {**sub}, params, admin_use))
        return out
    if t == "luck_mult":
        rolls = effect.get("rolls")
        await add(user.id, "luck_mult", value=float(effect["value"]), stack_mode="multiply", rolls=rolls,
                  duration_sec=None if rolls else duration)
        return {"effect": "luck_mult", "value": effect["value"]}
    if t == "cooldown_mult":
        await add(user.id, "cooldown_mult", value=float(effect["value"]), stack_mode="multiply", duration_sec=duration)
        return {"effect": "cooldown_mult", "value": effect["value"]}
    if t in ("force_biome", "choose_biome"):
        key = effect.get("biome") if t == "force_biome" else str(params.get("biome_key", ""))
        b = snap.biomes.get(key or "")
        if b is None or (t == "choose_biome" and b.kind != "natural"):
            raise AppError("Biomeを指定してください（自然Biomeのみ）", code="invalid_biome")
        ctx = await biomes_svc.force_biome(db, user, b.key, duration, principal.user.id, allow_admin=True)
        queue_event(db, "user", "biome", biomes_svc.public_state(ctx.row), user_id=user.id)
        stats = await users_svc.lock_stats(db, user.id)
        await progress_svc.record_biome_seen(stats, b.key)
        return {"effect": "biome", "biome": b.key}
    if t == "table_flatten":
        await add(user.id, "table_flatten", value=float(effect.get("value", 0.6)), stack_mode="highest", rolls=int(effect.get("rolls", 10)))
        return {"effect": "table_flatten"}
    if t == "forge_item":
        slot = snap.items_by_key.get("stellar_artifact")
        if slot is None:
            raise AppError("鍛造スロットが存在しません", code="forge_unavailable")
        spec = generate(slot, snap, system_rng(), min_odds=float(effect.get("min_odds", 100000)))
        spec.name = f"Star-Forged {spec.name}"[:128]
        spec.key = (spec.key + ":forged")[:128]
        from .rolls import collection_upsert, materialize_generated

        row, _ = await materialize_generated(db, snap, slot, spec)
        rar = snap.rarities.get(row.rarity_key)
        ids = await inv_svc.create_instances(db, user.id, row.id, 1, "admin", tier=rar.tier if rar else 5,
                                             meta={"star_forged": True, "forged_by": principal.user.id})
        await collection_upsert(db, user.id, row.id, 1)
        return {"effect": "forge_item", "item": inv_svc._item_row_public(row), "instance_id": ids[0]}  # noqa: SLF001
    if t == "force_item":
        if admin_use and params.get("item_key"):
            target_item = snap.items_by_key.get(str(params["item_key"]))
            if target_item is None or target_item.kind == "admin_artifact" or not target_item.rollable:
                raise AppError("指定アイテムが不正です", code="invalid_item")
            await add(user.id, "force_item", rolls=1, params={"item_key": target_item.key})
            return {"effect": "force_item", "item_key": target_item.key}
        await add(user.id, "force_item", rolls=1, params={"tier": "legendary"})
        return {"effect": "force_item", "tier": "legendary"}
    if t == "reveal_rng":
        await add(user.id, "reveal_rng", duration_sec=duration)
        return {"effect": "reveal_rng"}
    if t == "best_of":
        await add(user.id, "best_of", value=float(effect.get("value", 2)), stack_mode="highest", rolls=int(effect.get("rolls", 10)))
        return {"effect": "best_of"}
    if t == "compounding_luck":
        await add(user.id, "compounding_luck", value=float(effect.get("value", 0.25)), stack_mode="multiply",
                  rolls=int(effect.get("rolls", 30)), params={"count": 0})
        return {"effect": "compounding_luck"}
    if t == "burst_roll":
        from . import rolls as rolls_svc

        env = await rolls_svc.load_env(db, user, utcnow())
        summary = await rolls_svc.burst_roll(db, env, int(effect.get("count", 100)))
        await rolls_svc._finish_progress(db, env)  # noqa: SLF001
        return {"effect": "burst_roll", "burst": summary, "progress": env.progress.public()}
    if t == "preview":
        await add(user.id, "preview", params={"charges": int(effect.get("charges", 3)), "u": system_rng().random()})
        return {"effect": "preview", "charges": int(effect.get("charges", 3))}
    if t == "grant_stardust":
        amount = int(effect.get("amount", 0))
        user.stardust += amount
        return {"effect": "grant_stardust", "amount": amount}
    if t == "lock_biome":
        await biomes_svc.lock_biome(db, user, int(duration or 1800))
        return {"effect": "lock_biome", "seconds": duration}
    if t == "global_event":
        now = utcnow()
        ev = GameEvent(key=f"artifact_{art['key']}_{int(now.timestamp())}", name=name, description=art["ability"], type="luck_multiplier",
                       params={"mult": float(effect.get("mult", 2.0)), "source": art["key"]}, starts_at=now,
                       ends_at=now + timedelta(seconds=int(duration or 600)), created_by=principal.user.id)
        db.add(ev)
        await db.flush()
        await bump_content_version(db, principal.user.id)
        return {"effect": "global_event", "event_id": ev.id, "public": {"mult": effect.get("mult"), "ends_at": ev.ends_at.isoformat()}}
    if t == "global_boost":
        cutoff = utcnow() - timedelta(minutes=3)
        uids = [r[0] for r in (await db.execute(select(User.id).where(User.last_seen_at > cutoff, User.status == "active"))).all()]
        for uid in uids[:5000]:
            await effects_svc.add_effect(db, uid, source_type="artifact", source_key=art["key"], name=name, effect_type="luck",
                                         value=float(effect.get("value", 500)), stack_mode="add", rolls=int(effect.get("rolls", 1)),
                                         granted_by=principal.user.id)
            queue_event(db, "user", "effects_changed", {"source": art["key"]}, user_id=uid)
        return {"effect": "global_boost", "recipients": len(uids), "public": {"recipients": len(uids)}}
    if t == "min_rarity":
        await add(user.id, "min_rarity", stack_mode="highest", rolls=int(effect.get("rolls", 1)), params={"tier": effect.get("tier", "epic")})
        return {"effect": "min_rarity"}
    if t == "duplicate":
        await add(user.id, "duplicate", rolls=int(effect.get("rolls", 1)))
        return {"effect": "duplicate"}
    if t in ("bless", "target_min_rarity"):
        target = await _target_user(db, params)
        if t == "bless":
            await progress_svc.grant_cosmetic(db, target.id, str(effect.get("title", "t_blessed")), "artifact")
            await effects_svc.add_effect(db, target.id, source_type="artifact", source_key=art["key"], name=name, effect_type="luck",
                                         value=float(effect.get("value", 100)), duration_sec=duration, granted_by=principal.user.id)
        else:
            await effects_svc.add_effect(db, target.id, source_type="artifact", source_key=art["key"], name=name,
                                         effect_type="target_min_rarity", rolls=1, stack_mode="highest",
                                         params={"tier": effect.get("tier", "secret")}, granted_by=principal.user.id)
        await feed_svc.notify_user(db, target.id, "artifact_blessing", f"{name} の力があなたに宿った", art["ability"], {"artifact": art["key"]})
        queue_event(db, "user", "admin_event", {"kind": "blessing", "artifact": art["key"], "name": name, "theme": art["theme"],
                                                "tier": art["tier"]}, user_id=target.id)
        return {"effect": t, "target_user_id": target.id}
    if t == "reveal_secrets":
        await add(user.id, "reveal_secrets", duration_sec=duration)
        return {"effect": "reveal_secrets"}
    if t == "random_luck":
        await add(user.id, "random_luck", stack_mode="multiply", rolls=int(effect.get("rolls", 5)),
                  params={"min": effect.get("min", 1), "max": effect.get("max", 1e6)})
        return {"effect": "random_luck"}
    if t == "upgrade_equipment":
        eid = int(params.get("equipment_id") or 0)
        inst = await equipment_svc.upgrade_to_god(db, user.id, eid)
        return {"effect": "upgrade_equipment", "equipment": equipment_svc.public(inst)}
    if t == "extend_effects":
        seconds = int(effect.get("seconds", 3600))
        now = utcnow()
        rows = (await db.execute(select(ActiveEffect).where(ActiveEffect.user_id == user.id, ActiveEffect.expires_at > now))).scalars().all()
        for r in rows:
            r.expires_at = r.expires_at + timedelta(seconds=seconds)  # type: ignore[operator]
        return {"effect": "extend_effects", "extended": len(rows)}
    if t == "luck_boost":
        await add(user.id, "luck", value=float(effect.get("value", 300)), stack_mode="add", duration_sec=duration)
        return {"effect": "luck_boost"}
    if t == "biome_chance":
        await add(user.id, "biome_chance", value=float(effect.get("value", 10)), stack_mode="highest", duration_sec=duration)
        return {"effect": "biome_chance"}
    raise AppError(f"未実装の効果タイプ: {t}", code="unknown_effect")


async def _target_user(db: AsyncSession, params: dict[str, Any]) -> User:
    tid = params.get("target_user_id")
    if not tid:
        raise AppError("対象プレイヤーを指定してください", code="target_required")
    u = await db.get(User, int(tid))
    if u is None:
        raise NotFound("対象プレイヤーが見つかりません")
    return u


async def grant(db: AsyncSession, principal: Principal, target_user_id: int, artifact_key: str, *, can_use: bool, uses: int | None,
                hours: float | None, reason: str) -> dict[str, Any]:
    snap = get_registry().snap
    art = snap.artifacts.get(artifact_key)
    if art is None:
        raise NotFound("Artifactが見つかりません")
    target = await db.get(User, target_user_id)
    if target is None:
        raise NotFound("ユーザーが見つかりません")
    expires = utcnow() + timedelta(hours=hours) if hours else None
    ids = await inv_svc.create_instances(db, target.id, art["item_id"], 1, "admin", tier=8,
                                         meta={"granted_by": principal.user.id, "reason": reason[:200]})
    g = AdminGrant(artifact_key=art["key"], instance_id=ids[0], admin_id=principal.user.id, target_user_id=target.id, action="grant",
                   can_use=can_use and art["player_usable"], uses_remaining=uses, expires_at=expires, reason=reason)
    db.add(g)
    from .rolls import collection_upsert

    await collection_upsert(db, target.id, art["item_id"], 1)
    await audit.record(db, principal, "artifact_grant", target_user_id=target.id, entity_type="artifact", entity_id=art["key"],
                       new={"instance_id": ids[0], "can_use": g.can_use, "uses": uses, "expires_at": expires}, reason=reason)
    item = snap.items.get(art["item_id"])
    if target.id != principal.user.id:
        await feed_svc.notify_user(db, target.id, "artifact_granted", f"Admin Artifact「{item.name if item else art['key']}」を授かった",
                                   art["ability"], {"artifact": art["key"], "instance_id": ids[0]})
        tuser = await users_svc.lock_user(db, target.id)
        await progress_svc.trigger(db, tuser, "admin_grant")
    queue_event(db, "user", "admin_event", {"kind": "granted", "artifact": art["key"], "name": item.name if item else art["key"],
                                            "theme": art["theme"], "tier": art["tier"], "visual": item.visual if item else {}},
                user_id=target.id)
    return {"ok": True, "instance_id": ids[0], "grant_id": g.id}


async def recall(db: AsyncSession, principal: Principal, instance_id: int, reason: str) -> dict[str, Any]:
    snap = get_registry().snap
    inst = (await db.execute(select(ItemInstance).where(ItemInstance.id == instance_id).with_for_update())).scalar_one_or_none()
    if inst is None or inst.item_id not in snap.artifacts_by_item:
        raise NotFound("Artifactインスタンスが見つかりません")
    art = snap.artifacts_by_item[inst.item_id]
    if not art["transfer_rules"].get("recallable", True):
        raise Forbidden("このArtifactは回収できません", code="not_recallable")
    prev_owner = inst.owner_id
    inst.owner_id = principal.user.id
    inst.state = "owned"
    inst.meta = {**(inst.meta or {}), "recalled_from": prev_owner, "recalled_by": principal.user.id}
    await db.execute(update(AdminGrant).where(AdminGrant.instance_id == inst.id, AdminGrant.action == "grant").values(can_use=False))
    db.add(AdminGrant(artifact_key=art["key"], instance_id=inst.id, admin_id=principal.user.id, target_user_id=prev_owner,
                      action="recall", reason=reason))
    await audit.record(db, principal, "artifact_recall", target_user_id=prev_owner, entity_type="artifact", entity_id=art["key"],
                       old={"owner_id": prev_owner}, new={"owner_id": principal.user.id, "instance_id": inst.id}, reason=reason)
    if prev_owner != principal.user.id:
        await feed_svc.notify_user(db, prev_owner, "artifact_recalled", "Admin Artifactが管理者により回収されました", reason, {"artifact": art["key"]})
    return {"ok": True}


async def my_artifacts(db: AsyncSession, user_id: int, is_admin_mode: bool) -> list[dict[str, Any]]:
    snap = get_registry().snap
    ids = list(snap.artifacts_by_item.keys())
    if not ids:
        return []
    rows = (await db.execute(select(ItemInstance).where(ItemInstance.owner_id == user_id, ItemInstance.item_id.in_(ids))
                             .order_by(ItemInstance.id))).scalars().all()
    now = utcnow()
    cds = {c.artifact_key: c for c in (await db.execute(select(ArtifactCooldown).where(ArtifactCooldown.user_id == user_id))).scalars().all()}
    out = []
    for r in rows:
        art = snap.artifacts_by_item[r.item_id]
        grant = await _active_grant(db, r.id) if not is_admin_mode else None
        cd = cds.get(art["key"])
        out.append({
            "instance_id": r.id, "state": r.state, "serial": r.serial, "obtained_at": r.obtained_at.isoformat(), "meta": r.meta,
            "artifact": artifact_public(art),
            "usable": is_admin_mode or (art["player_usable"] and grant is not None),
            "uses_remaining": grant.uses_remaining if grant else None,
            "ready_at": cd.ready_at.isoformat() if cd and cd.ready_at > now and not is_admin_mode else None,
        })
    return out


def tier_key(tier: int) -> str:
    return next((k for k, v in TIER_BY_KEY.items() if v == tier), "common")
