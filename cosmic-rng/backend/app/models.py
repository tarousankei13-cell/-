"""ORM models.

Design notes
------------
* Content tables (items, biomes, equipment, ...) use an integer surrogate id plus
  a stable string ``key``. Game logic references content by key so content can be
  re-seeded / edited without breaking references.
* High volume tables (rolls, item_instances) use BIGINT identity keys and compact
  columns. Aggregate item statistics are updated in batches (see services.stats).
* Flexible, evolving structures (visual descriptors, conditions, effect params,
  user settings) are JSONB and validated at the application layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

TS = DateTime(timezone=True)


def now_col(**kw: Any) -> Mapped[datetime]:
    return mapped_column(TS, server_default=func.now(), nullable=False, **kw)


def jsonb(default: str = "'{}'::jsonb", nullable: bool = False) -> Mapped[Any]:
    return mapped_column(JSONB, server_default=text(default), nullable=nullable)


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("stardust >= 0", name="stardust_nonneg"),
        Index("ix_users_last_seen", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), server_default="player", nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_until: Mapped[datetime | None] = mapped_column(TS)

    stardust: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    xp: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    base_luck: Mapped[float] = mapped_column(Double, server_default="1.0", nullable=False)
    roll_counter: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)

    next_roll_at: Mapped[datetime | None] = mapped_column(TS)
    last_roll_at: Mapped[datetime | None] = mapped_column(TS)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)
    auto_roll_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    auto_roll_since: Mapped[datetime | None] = mapped_column(TS)
    offline_processed_until: Mapped[datetime | None] = mapped_column(TS)

    title_key: Mapped[str | None] = mapped_column(String(64))
    badge_keys: Mapped[list[str]] = jsonb("'[]'::jsonb")
    profile_background: Mapped[str | None] = mapped_column(String(64))
    bio: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_mode: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    revoked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    created_at: Mapped[datetime] = now_col()
    last_used_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict[str, Any]] = jsonb()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class UserStats(Base):
    __tablename__ = "user_stats"
    __table_args__ = (
        Index("ix_user_stats_rolls", "total_rolls"),
        Index("ix_user_stats_best", "best_odds"),
        Index("ix_user_stats_discovered", "discovered_count"),
        Index("ix_user_stats_achievements", "achievements_count"),
        Index("ix_user_stats_worth", "net_worth"),
        Index("ix_user_stats_firsts", "first_discoveries"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    total_rolls: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    offline_rolls: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    special_rolls: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    items_obtained: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    items_auto_deleted: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    best_odds: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    best_item_id: Mapped[int | None] = mapped_column(Integer)
    discovered_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    stardust_earned: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    items_sold: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    crafts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    trades: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    market_sales: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    market_buys: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    gifts_sent: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    first_discoveries: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    boosts_used: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    quests_completed: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    achievements_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    artifacts_used: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    max_luck: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    net_worth: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    rarity_counts: Mapped[dict[str, int]] = jsonb()
    biomes_seen: Mapped[list[str]] = jsonb("'[]'::jsonb")
    counters: Mapped[dict[str, Any]] = jsonb()  # misc trackers (streaks, hidden quest state)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
class Rarity(Base):
    __tablename__ = "rarities"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    min_odds: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False)
    color2: Mapped[str] = mapped_column(String(16), nullable=False)
    luck_exponent: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    xp: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    season_points: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    cutscene: Mapped[str] = mapped_column(String(32), nullable=False)
    announce: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    extra: Mapped[dict[str, Any]] = jsonb()


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_kind", "kind"),
        Index("ix_items_rarity", "rarity_key"),
        CheckConstraint("odds IS NULL OR odds >= 1", name="odds_min"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    lore: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), server_default="standard", nullable=False)
    rarity_key: Mapped[str] = mapped_column(ForeignKey("rarities.key", onupdate="CASCADE"), nullable=False)
    odds: Mapped[float | None] = mapped_column(Double)
    display_odds: Mapped[str | None] = mapped_column(String(48))
    rollable: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    sell_value: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    market_value: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    biome_keys: Mapped[list[str]] = jsonb("'[]'::jsonb")
    excluded_biome_keys: Mapped[list[str]] = jsonb("'[]'::jsonb")
    min_luck: Mapped[float | None] = mapped_column(Double)
    conditions: Mapped[dict[str, Any]] = jsonb()
    luck_curve: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    effects: Mapped[dict[str, Any]] = jsonb()
    visual: Mapped[dict[str, Any]] = jsonb()
    animation: Mapped[str | None] = mapped_column(String(32))
    sound: Mapped[str | None] = mapped_column(String(32))
    tradeable: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    procedural: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    first_discoverer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    first_discovered_at: Mapped[datetime | None] = mapped_column(TS)
    fastest_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    fastest_roll_count: Mapped[int | None] = mapped_column(BigInteger)
    discovery_count: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    owner_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    trade_count: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    serial_counter: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ItemRevision(Base):
    """History of item balance changes — keeps past rolls interpretable (RNG versioning)."""

    __tablename__ = "item_revisions"
    __table_args__ = (Index("ix_item_revisions_item", "item_id", "content_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = jsonb()
    changed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = now_col()


class ItemPart(Base):
    """Building blocks for procedurally generated items."""

    __tablename__ = "item_parts"
    __table_args__ = (UniqueConstraint("part_type", "key", name="uq_item_parts_type_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_type: Mapped[str] = mapped_column(String(16), nullable=False)  # material/shape/effect/modifier
    key: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    value_mult: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    visual: Mapped[dict[str, Any]] = jsonb()
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class ItemInstance(Base):
    __tablename__ = "item_instances"
    __table_args__ = (
        Index("ix_instances_owner_item", "owner_id", "item_id"),
        Index("ix_instances_owner_obtained", "owner_id", "obtained_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    original_owner_id: Mapped[int | None] = mapped_column(BigInteger)
    serial: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), server_default="owned", nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    roll_id: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict[str, Any]] = jsonb()
    obtained_at: Mapped[datetime] = now_col()


class UserItemPref(Base):
    """Per item-type preferences (favorite protects future drops from auto delete)."""

    __tablename__ = "user_item_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    favorite: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (Index("ix_collections_item", "item_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    first_obtained_at: Mapped[datetime] = now_col()
    times_obtained: Mapped[int] = mapped_column(BigInteger, server_default="1", nullable=False)


class Biome(Base):
    __tablename__ = "biomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), server_default="natural", nullable=False)  # default/natural/admin
    odds_per_sec: Mapped[float | None] = mapped_column(Double)  # 1 in N chance per second
    duration_sec: Mapped[int] = mapped_column(Integer, server_default="120", nullable=False)
    luck_mult: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    item_boosts: Mapped[dict[str, float]] = jsonb()
    theme: Mapped[dict[str, Any]] = jsonb()
    special_states: Mapped[list[dict[str, Any]]] = jsonb("'[]'::jsonb")
    announce: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserBiome(Base):
    __tablename__ = "user_biomes"
    __table_args__ = (Index("ix_user_biomes_next", "next_eval_at"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    biome_key: Mapped[str] = mapped_column(String(48), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(TS)
    next_eval_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    next_biome_key: Mapped[str | None] = mapped_column(String(48))
    state_key: Mapped[str | None] = mapped_column(String(48))
    state_ends_at: Mapped[datetime | None] = mapped_column(TS)
    next_state_at: Mapped[datetime | None] = mapped_column(TS)
    next_state_key: Mapped[str | None] = mapped_column(String(48))
    forced: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    forced_by: Mapped[int | None] = mapped_column(BigInteger)
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    sample_sig: Mapped[str] = mapped_column(String(64), server_default="", nullable=False)
    evaluated_at: Mapped[datetime] = now_col()


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    slot: Mapped[str] = mapped_column(String(16), nullable=False)
    rarity_key: Mapped[str] = mapped_column(ForeignKey("rarities.key", onupdate="CASCADE"), nullable=False)
    luck_bonus: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    speed_bonus: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    passives: Mapped[list[dict[str, Any]]] = jsonb("'[]'::jsonb")
    visual: Mapped[dict[str, Any]] = jsonb()
    sell_value: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserEquipment(Base):
    __tablename__ = "user_equipment"
    __table_args__ = (
        Index("ix_user_equipment_user", "user_id"),
        Index(
            "uq_user_equipment_slot",
            "user_id",
            "equipped_slot",
            unique=True,
            postgresql_where=text("equipped_slot IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), nullable=False)
    quality: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    quality_tier: Mapped[str] = mapped_column(String(16), server_default="normal", nullable=False)
    luck_bonus: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    speed_bonus: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    equipped_slot: Mapped[str | None] = mapped_column(String(16))
    locked: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    source: Mapped[str] = mapped_column(String(16), server_default="craft", nullable=False)
    meta: Mapped[dict[str, Any]] = jsonb()
    obtained_at: Mapped[datetime] = now_col()


class Boost(Base):
    __tablename__ = "boosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    effect_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    rolls: Mapped[int | None] = mapped_column(Integer)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    stack_mode: Mapped[str] = mapped_column(String(16), server_default="add", nullable=False)
    biome_keys: Mapped[list[str]] = jsonb("'[]'::jsonb")
    params: Mapped[dict[str, Any]] = jsonb()
    rarity_key: Mapped[str] = mapped_column(ForeignKey("rarities.key", onupdate="CASCADE"), nullable=False)
    visual: Mapped[dict[str, Any]] = jsonb()
    sell_value: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class Inventory(Base):
    """Stackable consumables (boost items). Unique items live in item_instances."""

    __tablename__ = "inventory"
    __table_args__ = (CheckConstraint("quantity >= 0", name="qty_nonneg"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    boost_id: Mapped[int] = mapped_column(ForeignKey("boosts.id", ondelete="CASCADE"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ActiveEffect(Base):
    __tablename__ = "active_effects"
    __table_args__ = (
        Index("ix_active_effects_user", "user_id"),
        Index("ix_active_effects_expires", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)  # boost/artifact/admin/system/event
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    effect_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    stack_mode: Mapped[str] = mapped_column(String(16), server_default="add", nullable=False)
    remaining_rolls: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    biome_keys: Mapped[list[str]] = jsonb("'[]'::jsonb")
    params: Mapped[dict[str, Any]] = jsonb()
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = now_col()


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    ingredients: Mapped[list[dict[str, Any]]] = jsonb("'[]'::jsonb")
    stardust_cost: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    outputs: Mapped[list[dict[str, Any]]] = jsonb("'[]'::jsonb")
    hidden: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    hint: Mapped[str | None] = mapped_column(Text)
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserRecipe(Base):
    __tablename__ = "user_recipes"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True)
    crafted_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    discovered_at: Mapped[datetime] = now_col()


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    biome_key: Mapped[str | None] = mapped_column(String(48))
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class ShopItem(Base):
    __tablename__ = "shop_items"
    __table_args__ = (CheckConstraint("price >= 0", name="price_nonneg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    shop_key: Mapped[str] = mapped_column(ForeignKey("shops.key", onupdate="CASCADE", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False)  # boost/equipment/unlock/item
    product_key: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_count: Mapped[int | None] = mapped_column(Integer)
    limit_period: Mapped[str | None] = mapped_column(String(16))  # daily/lifetime
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    requires_unlock: Mapped[str | None] = mapped_column(String(64))
    biome_key: Mapped[str | None] = mapped_column(String(48))
    visual: Mapped[dict[str, Any]] = jsonb()
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class ShopPurchase(Base):
    __tablename__ = "shop_purchases"
    __table_args__ = (Index("ix_shop_purchases_user_item", "user_id", "shop_item_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    shop_item_id: Mapped[int] = mapped_column(ForeignKey("shop_items.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = now_col()


class UserUnlock(Base):
    __tablename__ = "user_unlocks"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    unlock_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = jsonb()
    created_at: Mapped[datetime] = now_col()


class Cosmetic(Base):
    __tablename__ = "cosmetics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # title/badge/background
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    rarity_key: Mapped[str] = mapped_column(ForeignKey("rarities.key", onupdate="CASCADE"), nullable=False)
    visual: Mapped[dict[str, Any]] = jsonb()
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserCosmetic(Base):
    __tablename__ = "user_cosmetics"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    cosmetic_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), server_default="achievement", nullable=False)
    obtained_at: Mapped[datetime] = now_col()


class Showcase(Base):
    __tablename__ = "showcases"
    __table_args__ = (CheckConstraint("slot >= 0 AND slot < 6", name="slot_range"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    slot: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("item_instances.id", ondelete="CASCADE"), nullable=False)


# ---------------------------------------------------------------------------
# Rolls
# ---------------------------------------------------------------------------
class Roll(Base):
    __tablename__ = "rolls"
    __table_args__ = (
        Index("ix_rolls_user_id", "user_id", "id"),
        Index("ix_rolls_created", "created_at"),
        Index("ix_rolls_item", "item_id"),
        Index("ix_rolls_tier_created", "tier", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = now_col()
    roll_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    biome_key: Mapped[str] = mapped_column(String(48), nullable=False)
    biome_state: Mapped[str | None] = mapped_column(String(48))
    luck: Mapped[float] = mapped_column(Double, nullable=False)
    base_odds: Mapped[float] = mapped_column(Double, nullable=False)
    final_chance: Mapped[float] = mapped_column(Double, nullable=False)
    equipment_mult: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    boost_mult: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    flags: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    rng_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[int | None] = mapped_column(BigInteger)
    instance_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RollBatch(Base):
    """Offline / burst roll batches. Individual notable rolls are also stored in rolls."""

    __tablename__ = "roll_batches"
    __table_args__ = (Index("ix_roll_batches_user", "user_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # offline/burst
    window_start: Mapped[datetime] = mapped_column(TS, nullable=False)
    window_end: Mapped[datetime] = mapped_column(TS, nullable=False)
    roll_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = jsonb()
    rng_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    seen: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    created_at: Mapped[datetime] = now_col()


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------
class MarketListing(Base):
    __tablename__ = "market_listings"
    __table_args__ = (
        Index("ix_market_status_item", "status", "item_id"),
        Index("ix_market_item_sold", "item_id", "sold_at"),
        Index("ix_market_seller", "seller_id", "status"),
        Index(
            "uq_market_active_instance",
            "instance_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint("price > 0", name="price_pos"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    buyer_id: Mapped[int | None] = mapped_column(BigInteger)
    fee: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    flag_reason: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = jsonb()
    created_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    sold_at: Mapped[datetime | None] = mapped_column(TS)
    closed_at: Mapped[datetime | None] = mapped_column(TS)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_from", "from_user_id", "status"),
        Index("ix_trades_to", "to_user_id", "status"),
        CheckConstraint("offer_stardust >= 0 AND request_stardust >= 0", name="stardust_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    offer_items: Mapped[list[int]] = jsonb("'[]'::jsonb")
    request_items: Mapped[list[int]] = jsonb("'[]'::jsonb")
    offer_stardust: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    request_stardust: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    message: Mapped[str | None] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    snapshot: Mapped[dict[str, Any]] = jsonb()
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TS)


class TradeItem(Base):
    """Denormalised index of items in trades — enables admin search by item/instance."""

    __tablename__ = "trade_items"
    __table_args__ = (Index("ix_trade_items_instance", "instance_id"), Index("ix_trade_items_item", "item_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # offer/request
    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)


class Gift(Base):
    __tablename__ = "gifts"
    __table_args__ = (Index("ix_gifts_from", "from_user_id", "created_at"), Index("ix_gifts_to", "to_user_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[int | None] = mapped_column(BigInteger)
    item_id: Mapped[int | None] = mapped_column(Integer)
    stardust: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    message: Mapped[str | None] = mapped_column(String(200))
    snapshot: Mapped[dict[str, Any]] = jsonb()
    created_at: Mapped[datetime] = now_col()


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------
class Quest(Base):
    __tablename__ = "quests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # daily/chain/hidden
    chain_key: Mapped[str | None] = mapped_column(String(48))
    chain_step: Mapped[int | None] = mapped_column(Integer)
    objective: Mapped[dict[str, Any]] = jsonb()
    rewards: Mapped[dict[str, Any]] = jsonb()
    hint: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Double, server_default="1", nullable=False)
    min_level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserQuest(Base):
    __tablename__ = "user_quests"
    __table_args__ = (
        UniqueConstraint("user_id", "quest_id", "period_key", name="uq_user_quests_period"),
        Index("ix_user_quests_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[dict[str, Any]] = jsonb()
    progress: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    target: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rewards: Mapped[dict[str, Any]] = jsonb()
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    created_at: Mapped[datetime] = now_col()
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    claimed_at: Mapped[datetime | None] = mapped_column(TS)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), server_default="bronze", nullable=False)
    condition: Mapped[dict[str, Any]] = jsonb()
    rewards: Mapped[dict[str, Any]] = jsonb()
    hidden: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    hint: Mapped[str | None] = mapped_column(Text)
    first_achiever_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    first_achieved_at: Mapped[datetime | None] = mapped_column(TS)
    achiever_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class UserAchievement(Base):
    __tablename__ = "user_achievements"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    achievement_id: Mapped[int] = mapped_column(ForeignKey("achievements.id", ondelete="CASCADE"), primary_key=True)
    achieved_at: Mapped[datetime] = now_col()
    world_first: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    starts_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="scheduled", nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = now_col()


class SeasonStats(Base):
    __tablename__ = "season_stats"
    __table_args__ = (Index("ix_season_stats_points", "season_id", "points"),)

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    rolls: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    points: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    best_odds: Mapped[float] = mapped_column(Double, server_default="0", nullable=False)
    first_discoveries: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class Ranking(Base):
    """Archived leaderboard results (season finals, periodic snapshots)."""

    __tablename__ = "rankings"
    __table_args__ = (Index("ix_rankings_board", "board", "season_id", "rank"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    board: Mapped[str] = mapped_column(String(32), nullable=False)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    snapshot: Mapped[dict[str, Any]] = jsonb()
    created_at: Mapped[datetime] = now_col()


# ---------------------------------------------------------------------------
# World / notifications / events
# ---------------------------------------------------------------------------
class WorldEvent(Base):
    """World feed entries (rare drops, first discoveries, world firsts...)."""

    __tablename__ = "world_events"
    __table_args__ = (Index("ix_world_events_created", "created_at"), Index("ix_world_events_type", "type", "created_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    public: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb()
    created_at: Mapped[datetime] = now_col()


class GameEvent(Base):
    """Admin configured live events (global luck, biome surge, item event conditions)."""

    __tablename__ = "game_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = jsonb()
    starts_at: Mapped[datetime | None] = mapped_column(TS)
    ends_at: Mapped[datetime | None] = mapped_column(TS)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = now_col()


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user", "user_id", "read", "created_at"), Index("ix_notifications_admin", "for_admins", "created_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    for_admins: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    data: Mapped[dict[str, Any]] = jsonb()
    read: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    created_at: Mapped[datetime] = now_col()


class DiscordOutbox(Base):
    __tablename__ = "discord_outbox"
    __table_args__ = (Index("ix_discord_outbox_status", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb()
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = now_col()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = now_col()
    sent_at: Mapped[datetime | None] = mapped_column(TS)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
class AdminArtifact(Base):
    __tablename__ = "admin_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), unique=True, nullable=False)
    ability: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    theme: Mapped[str] = mapped_column(String(16), nullable=False)
    effect: Mapped[dict[str, Any]] = jsonb()
    target: Mapped[str] = mapped_column(String(16), server_default="self", nullable=False)  # self/user/global
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    cooldown_sec: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    tier: Mapped[int] = mapped_column(SmallInteger, server_default="1", nullable=False)
    player_usable: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    equip_passive: Mapped[dict[str, Any]] = jsonb()
    transfer_rules: Mapped[dict[str, Any]] = jsonb()
    audit_rules: Mapped[dict[str, Any]] = jsonb()
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)


class AdminGrant(Base):
    __tablename__ = "admin_grants"
    __table_args__ = (Index("ix_admin_grants_target", "target_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artifact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # grant/recall
    can_use: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    uses_remaining: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    reason: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    created_at: Mapped[datetime] = now_col()


class ArtifactCooldown(Base):
    __tablename__ = "artifact_cooldowns"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    artifact_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    ready_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    uses: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_admin", "admin_id", "created_at"),
        Index("ix_audit_target", "target_user_id", "created_at"),
        Index("ix_audit_action", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    admin_mode: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    session_hash: Mapped[str | None] = mapped_column(String(16))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = now_col()


class GameSetting(Base):
    __tablename__ = "game_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = now_col(onupdate=func.now())


class ContentOverride(Base):
    """Temporary content changes that automatically revert at expires_at."""

    __tablename__ = "content_overrides"
    __table_args__ = (Index("ix_content_overrides_active", "active", "expires_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    patch: Mapped[dict[str, Any]] = jsonb()
    reason: Mapped[str] = mapped_column(Text, server_default="", nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    starts_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    created_at: Mapped[datetime] = now_col()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_created", "created_at"),)

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = now_col()


class ErrorLog(Base):
    __tablename__ = "error_logs"
    __table_args__ = (Index("ix_error_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    error_id: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    exc_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = now_col()


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), server_default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="running", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = now_col()
    finished_at: Mapped[datetime | None] = mapped_column(TS)
