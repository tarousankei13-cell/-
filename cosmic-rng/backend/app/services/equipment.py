"""Equipment instances, quality rolls ("God Roll"), equip slots and artifact auras."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..content.registry import get_registry
from ..core.errors import AppError, Forbidden, NotFound
from ..models import ItemInstance, User, UserEquipment
from ..rng.engine import RandomSource, system_rng
from ..rng.modifiers import EquipData
from . import users as users_svc

SLOTS = ["gauntlet", "core", "relic"]
ARTIFACT_SLOT = "artifact"

QUALITY_TABLE: list[tuple[str, float, float, float]] = [
    # tier, probability, min, max
    ("normal", 0.60, 0.85, 1.00),
    ("fine", 0.25, 1.00, 1.10),
    ("superior", 0.11, 1.10, 1.25),
    ("perfect", 0.035, 1.25, 1.40),
    ("god", 0.005, 1.60, 2.00),
]
QUALITY_NAMES = {"normal": "Normal", "fine": "Fine", "superior": "Superior", "perfect": "Perfect", "god": "GOD ROLL"}


def roll_quality(rng: RandomSource) -> tuple[str, float]:
    u = rng.random()
    acc = 0.0
    for tier, p, lo, hi in QUALITY_TABLE:
        acc += p
        if u < acc:
            return tier, round(rng.uniform(lo, hi), 4)
    tier, _, lo, hi = QUALITY_TABLE[-1]
    return tier, round(rng.uniform(lo, hi), 4)


async def create_instance(db: AsyncSession, user_id: int, equipment_key: str, source: str,
                          quality: tuple[str, float] | None = None) -> UserEquipment:
    snap = get_registry().snap
    eq = snap.equipment.get(equipment_key)
    if eq is None:
        raise NotFound(f"装備 {equipment_key} が見つかりません")
    tier, q = quality or roll_quality(system_rng())
    inst = UserEquipment(
        user_id=user_id, equipment_id=eq["id"], quality=q, quality_tier=tier,
        luck_bonus=round(eq["luck_bonus"] * q, 6), speed_bonus=round(eq["speed_bonus"] * q, 6), source=source,
    )
    db.add(inst)
    await db.flush()
    return inst


def to_equip_data(inst: UserEquipment) -> EquipData | None:
    eq = get_registry().snap.equipment_by_id.get(inst.equipment_id)
    if eq is None or not eq.get("is_active", True):
        return None
    return EquipData(key=eq["key"], slot=inst.equipped_slot or eq["slot"], luck_bonus=inst.luck_bonus,
                     speed_bonus=inst.speed_bonus, passives=list(eq.get("passives") or []), name=eq["name"])


async def load_equipped(db: AsyncSession, user_id: int) -> tuple[list[EquipData], list[dict[str, Any]]]:
    """Returns (equip data for the RNG, visual descriptors for the client)."""
    snap = get_registry().snap
    rows = (await db.execute(select(UserEquipment).where(UserEquipment.user_id == user_id,
                                                          UserEquipment.equipped_slot.is_not(None)))).scalars().all()
    data: list[EquipData] = []
    visuals: list[dict[str, Any]] = []
    for r in rows:
        d = to_equip_data(r)
        if d:
            data.append(d)
            eq = snap.equipment_by_id[r.equipment_id]
            visuals.append({"slot": r.equipped_slot, "key": eq["key"], "name": eq["name"], "rarity": eq["rarity_key"],
                            "visual": eq["visual"], "quality_tier": r.quality_tier})
    # Equipped admin artifact → passive aura
    art_inst = (await db.execute(select(ItemInstance).where(ItemInstance.owner_id == user_id, ItemInstance.state == "equipped")
                                 .limit(1))).scalar_one_or_none()
    if art_inst is not None:
        art = snap.artifacts_by_item.get(art_inst.item_id)
        item = snap.items.get(art_inst.item_id)
        if art and item:
            passive = art.get("equip_passive") or {}
            passives = []
            if passive.get("luck_mult"):
                passives.append({"type": "luck_mult", "mult": float(passive["luck_mult"])})
            data.append(EquipData(key=art["key"], slot=ARTIFACT_SLOT, luck_bonus=0.0, speed_bonus=0.0, passives=passives, name=item.name))
            visuals.append({"slot": ARTIFACT_SLOT, "key": item.key, "name": item.name, "name_ja": item.name_ja, "rarity": "admin", "visual": item.visual,
                            "instance_id": art_inst.id, "aura": passive.get("aura") or art["theme"]})
    return data, visuals


def public(inst: UserEquipment) -> dict[str, Any]:
    eq = get_registry().snap.equipment_by_id.get(inst.equipment_id) or {}
    return {
        "id": inst.id, "key": eq.get("key"), "name": eq.get("name"), "description": eq.get("description"), "slot": eq.get("slot"),
        "rarity": eq.get("rarity_key"), "visual": eq.get("visual", {}), "passives": eq.get("passives", []),
        "base_luck": eq.get("luck_bonus"), "base_speed": eq.get("speed_bonus"), "luck_bonus": inst.luck_bonus,
        "speed_bonus": inst.speed_bonus, "quality": inst.quality, "quality_tier": inst.quality_tier,
        "quality_name": QUALITY_NAMES.get(inst.quality_tier, inst.quality_tier), "equipped_slot": inst.equipped_slot,
        "locked": inst.locked, "source": inst.source, "obtained_at": inst.obtained_at.isoformat() if inst.obtained_at else None,
        "sell_value": int(round((eq.get("sell_value") or 0) * inst.quality)),
    }


async def list_for_user(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = (await db.execute(select(UserEquipment).where(UserEquipment.user_id == user_id).order_by(UserEquipment.id.desc()))).scalars().all()
    return [public(r) for r in rows]


async def get_owned(db: AsyncSession, user_id: int, inst_id: int, lock: bool = True) -> UserEquipment:
    q = select(UserEquipment).where(UserEquipment.id == inst_id, UserEquipment.user_id == user_id)
    if lock:
        q = q.with_for_update()
    inst = (await db.execute(q)).scalar_one_or_none()
    if inst is None:
        raise NotFound("装備が見つかりません")
    return inst


async def equip(db: AsyncSession, user: User, inst_id: int) -> UserEquipment:
    users_svc.require_feature(user, "equipment")
    snap = get_registry().snap
    inst = await get_owned(db, user.id, inst_id)
    eq = snap.equipment_by_id.get(inst.equipment_id)
    if eq is None or not eq.get("is_active", True):
        raise AppError("この装備は使用できません", code="equipment_inactive")
    slot = eq["slot"]
    if slot == "relic":
        users_svc.require_feature(user, "relic_slot")
    if user.level < int(eq.get("min_level", 1)):
        raise AppError(f"装備にはレベル{eq['min_level']}が必要です", code="level_required")
    await db.execute(update(UserEquipment).where(UserEquipment.user_id == user.id, UserEquipment.equipped_slot == slot)
                     .values(equipped_slot=None))
    inst.equipped_slot = slot
    await db.flush()
    return inst


async def unequip(db: AsyncSession, user: User, slot: str) -> None:
    if slot == ARTIFACT_SLOT:
        await db.execute(update(ItemInstance).where(ItemInstance.owner_id == user.id, ItemInstance.state == "equipped")
                         .values(state="owned"))
        return
    if slot not in SLOTS:
        raise AppError("不正なスロットです", code="invalid_slot")
    await db.execute(update(UserEquipment).where(UserEquipment.user_id == user.id, UserEquipment.equipped_slot == slot)
                     .values(equipped_slot=None))


async def equip_artifact(db: AsyncSession, user: User, instance_id: int) -> ItemInstance:
    snap = get_registry().snap
    inst = (await db.execute(select(ItemInstance).where(ItemInstance.id == instance_id, ItemInstance.owner_id == user.id)
                             .with_for_update())).scalar_one_or_none()
    if inst is None:
        raise NotFound("アイテムが見つかりません")
    if inst.item_id not in snap.artifacts_by_item:
        raise AppError("Admin Artifactのみ装備できます", code="not_artifact")
    if inst.state not in ("owned", "equipped"):
        raise AppError("このアイテムは現在装備できません", code="instance_busy")
    await db.execute(update(ItemInstance).where(ItemInstance.owner_id == user.id, ItemInstance.state == "equipped")
                     .values(state="owned"))
    inst.state = "equipped"
    await db.flush()
    return inst


async def sell(db: AsyncSession, user: User, inst_ids: list[int]) -> int:
    if not inst_ids or len(inst_ids) > 200:
        raise AppError("売却対象が不正です", code="invalid_selection")
    rows = (await db.execute(select(UserEquipment).where(UserEquipment.id.in_(inst_ids), UserEquipment.user_id == user.id)
                             .with_for_update())).scalars().all()
    if len(rows) != len(set(inst_ids)):
        raise NotFound("装備が見つかりません")
    total = 0
    for r in rows:
        if r.locked:
            raise Forbidden("ロック中の装備は売却できません", code="locked")
        if r.equipped_slot:
            raise AppError("装備中のアイテムは売却できません", code="equipped")
        total += public(r)["sell_value"]
        await db.delete(r)
    return total


async def upgrade_to_god(db: AsyncSession, user_id: int, inst_id: int) -> UserEquipment:
    inst = await get_owned(db, user_id, inst_id)
    eq = get_registry().snap.equipment_by_id[inst.equipment_id]
    q = max(inst.quality, 2.0)
    inst.quality = q
    inst.quality_tier = "god"
    inst.luck_bonus = round(eq["luck_bonus"] * q, 6)
    inst.speed_bonus = round(eq["speed_bonus"] * q, 6)
    inst.meta = {**(inst.meta or {}), "celestial_anvil": True}
    return inst
