"""Crafting from rolled items. Deterministic by default; random-output recipes and
secret recipes discovered through Experimental Fusion."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..db import upsert as insert
from ..core.errors import AppError, Forbidden, NotFound
from ..core.timeutil import utcnow
from ..models import ActiveEffect, User, UserRecipe
from ..rng.engine import system_rng
from . import effects as effects_svc
from . import equipment as equipment_svc
from . import inventory as inv_svc
from . import progress as progress_svc
from . import users as users_svc
from .progress import ProgressEvent


def _requirements(recipe: dict[str, Any], times: int = 1) -> dict[int, int]:
    snap = get_registry().snap
    req: dict[int, int] = {}
    for ing in recipe["ingredients"]:
        item = snap.items_by_key.get(ing["item_key"])
        if item is None:
            raise AppError(f"レシピの素材 {ing['item_key']} が存在しません", code="recipe_broken")
        req[item.id] = req.get(item.id, 0) + int(ing["qty"]) * times
    return req


async def _reveal_secrets(db: AsyncSession, user_id: int) -> bool:
    now = utcnow()
    rows = (await db.execute(select(ActiveEffect).where(ActiveEffect.user_id == user_id, ActiveEffect.effect_type == "reveal_secrets"))).scalars().all()
    return any(r.expires_at is None or r.expires_at > now for r in rows)


def _output_info(o: dict[str, Any]) -> dict[str, Any]:
    snap = get_registry().snap
    if o["type"] == "equipment":
        e = snap.equipment.get(o["key"]) or {}
        return {"type": "equipment", "key": o["key"], "name": e.get("name", o["key"]), "rarity": e.get("rarity_key", "common"),
                "visual": e.get("visual", {}), "qty": o.get("qty", 1), "weight": o.get("weight", 1)}
    if o["type"] == "boost":
        b = snap.boosts.get(o["key"]) or {}
        return {"type": "boost", "key": o["key"], "name": b.get("name", o["key"]), "rarity": b.get("rarity_key", "common"),
                "visual": b.get("visual", {}), "qty": o.get("qty", 1), "weight": o.get("weight", 1)}
    it = snap.items_by_key.get(o["key"])
    return {"type": "item", "key": o["key"], "name": it.name if it else o["key"], "rarity": it.rarity_key if it else "common",
            "visual": it.visual if it else {}, "qty": o.get("qty", 1), "weight": o.get("weight", 1)}


async def list_recipes(db: AsyncSession, user: User) -> dict[str, Any]:
    snap = get_registry().snap
    discovered = {r[0]: r[1] for r in (await db.execute(select(UserRecipe.recipe_id, UserRecipe.crafted_count).where(UserRecipe.user_id == user.id))).all()}
    reveal = await _reveal_secrets(db, user.id)
    all_ids = [snap.items_by_key[i["item_key"]].id for r in snap.recipes.values() for i in r["ingredients"] if i["item_key"] in snap.items_by_key]
    owned = await inv_svc.owned_counts(db, user.id, list(set(all_ids)))
    out = []
    for r in sorted(snap.recipes.values(), key=lambda x: x["sort_order"]):
        if not r["is_active"]:
            continue
        known = (not r["hidden"]) or r["id"] in discovered
        if not known and not reveal:
            out.append({"key": None, "hidden": True, "hint": r["hint"], "name": "???", "min_level": r["min_level"]})
            continue
        ings = []
        for ing in r["ingredients"]:
            it = snap.items_by_key.get(ing["item_key"])
            ings.append({"item_key": ing["item_key"], "name": it.name if it else ing["item_key"], "rarity": it.rarity_key if it else "common",
                         "visual": it.visual if it else {}, "qty": ing["qty"], "owned": owned.get(it.id, 0) if it else 0})
        outputs = [_output_info(o) for o in r["outputs"]]
        total_w = sum(float(o.get("weight", 1)) for o in r["outputs"]) or 1.0
        for o in outputs:
            o["chance"] = float(o["weight"]) / total_w
        out.append({
            "key": r["key"], "name": r["name"], "description": r["description"], "hidden": r["hidden"], "revealed_by_artifact": not known,
            "ingredients": ings, "stardust_cost": r["stardust_cost"], "outputs": outputs, "random": len(outputs) > 1,
            "min_level": r["min_level"], "craftable": all(i["owned"] >= i["qty"] for i in ings) and user.level >= r["min_level"],
            "crafted": discovered.get(r["id"], 0),
        })
    return {"recipes": out}


async def _deliver(db: AsyncSession, user: User, output: dict[str, Any]) -> dict[str, Any]:
    snap = get_registry().snap
    qty = int(output.get("qty", 1))
    if output["type"] == "equipment":
        insts = [equipment_svc.public(await equipment_svc.create_instance(db, user.id, output["key"], "craft")) for _ in range(qty)]
        return {**_output_info(output), "instances": insts}
    if output["type"] == "boost":
        await effects_svc.grant_boost_items(db, user.id, output["key"], qty)
        return _output_info(output)
    item = snap.items_by_key.get(output["key"])
    if item is None:
        raise AppError("出力アイテムが存在しません", code="recipe_broken")
    ids = await inv_svc.create_instances(db, user.id, item.id, qty, "craft", tier=item.tier)
    from .rolls import collection_upsert

    if await collection_upsert(db, user.id, item.id, qty):
        stats = await users_svc.lock_stats(db, user.id)
        if item.id in snap.collectible_ids:
            stats.discovered_count += 1
    return {**_output_info(output), "instance_ids": ids}


def _pick_output(recipe: dict[str, Any]) -> dict[str, Any]:
    outs = recipe["outputs"]
    if len(outs) == 1:
        return outs[0]
    total = sum(max(float(o.get("weight", 1)), 0.0) for o in outs)
    u = system_rng().random() * total
    acc = 0.0
    for o in outs:
        acc += max(float(o.get("weight", 1)), 0.0)
        if u < acc:
            return o
    return outs[-1]


async def _craft_once(db: AsyncSession, user: User, recipe: dict[str, Any], times: int) -> list[dict[str, Any]]:
    cost = int(recipe["stardust_cost"]) * times
    if user.stardust < cost:
        raise AppError("Stardustが足りません", code="insufficient_funds")
    await inv_svc.consume_items(db, user.id, _requirements(recipe, times))
    user.stardust -= cost
    results = [await _deliver(db, user, _pick_output(recipe)) for _ in range(times)]
    await db.execute(
        insert(UserRecipe).values(user_id=user.id, recipe_id=recipe["id"], crafted_count=times)
        .on_conflict_do_update(index_elements=["user_id", "recipe_id"], set_={"crafted_count": UserRecipe.crafted_count + times})
    )
    return results


async def craft(db: AsyncSession, user_id: int, recipe_key: str, times: int) -> dict[str, Any]:
    snap = get_registry().snap
    if times < 1 or times > 20:
        raise AppError("回数が不正です", code="invalid_quantity")
    user = await users_svc.lock_user(db, user_id)
    users_svc.require_feature(user, "crafting", "features.crafting_enabled")
    recipe = snap.recipes.get(recipe_key)
    if recipe is None or not recipe["is_active"]:
        raise NotFound("レシピが見つかりません")
    if user.level < recipe["min_level"]:
        raise Forbidden(f"レベル{recipe['min_level']}が必要です", code="level_required")
    if recipe["hidden"]:
        known = await db.get(UserRecipe, (user.id, recipe["id"]))
        if known is None:
            raise NotFound("レシピが見つかりません")
    results = await _craft_once(db, user, recipe, times)
    stats = await users_svc.lock_stats(db, user.id)
    stats.crafts += times
    res = await progress_svc.handle_events(db, user, stats, [ProgressEvent("craft", count=times)] +
                                           [ProgressEvent("obtain", params={"tier": snap.tier_of(r["rarity"]), "item_key": r["key"]})
                                            for r in results if r["type"] == "item"])
    return {"results": results, "stardust": user.stardust, "progress": res.public()}


async def experiment(db: AsyncSession, user_id: int, ingredients: list[dict[str, Any]]) -> dict[str, Any]:
    """Experimental Fusion: if the offered ingredients match a secret recipe, it is discovered and crafted.

    Nothing is consumed when no recipe matches.
    """
    snap = get_registry().snap
    user = await users_svc.lock_user(db, user_id)
    users_svc.require_feature(user, "crafting", "features.crafting_enabled")
    if not ingredients or len(ingredients) > 8:
        raise AppError("素材は1〜8種類で指定してください", code="invalid_ingredients")
    offered: dict[str, int] = {}
    for ing in ingredients:
        key = str(ing.get("item_key", ""))
        qty = int(ing.get("qty", 1))
        if key not in snap.items_by_key or qty < 1 or qty > 999:
            raise AppError("素材の指定が不正です", code="invalid_ingredients")
        offered[key] = offered.get(key, 0) + qty
    owned = await inv_svc.owned_counts(db, user.id, [snap.items_by_key[k].id for k in offered])
    for k, q in offered.items():
        if owned.get(snap.items_by_key[k].id, 0) < q:
            raise AppError("指定した素材を所持していません", code="not_enough_materials")
    for r in snap.recipes.values():
        if not r["is_active"] or not r["hidden"]:
            continue
        need = {i["item_key"]: int(i["qty"]) for i in r["ingredients"]}
        if set(need) != set(offered) or any(offered[k] < need[k] for k in need):
            continue
        if user.level < r["min_level"]:
            return {"result": "unstable", "message": "素材が共鳴しているが、まだ扱いきれない…（レベル不足）"}
        first_time = await db.get(UserRecipe, (user.id, r["id"])) is None
        results = await _craft_once(db, user, r, 1)
        stats = await users_svc.lock_stats(db, user.id)
        stats.crafts += 1
        events = [ProgressEvent("craft")] + [ProgressEvent("obtain", params={"tier": snap.tier_of(x["rarity"]), "item_key": x["key"]})
                                              for x in results if x["type"] == "item"]
        res = await progress_svc.handle_events(db, user, stats, events)
        return {"result": "discovered" if first_time else "crafted", "recipe": {"key": r["key"], "name": r["name"]},
                "results": results, "stardust": user.stardust, "progress": res.public()}
    return {"result": "nothing", "message": "何も起こらなかった…。素材は失われていない。"}
