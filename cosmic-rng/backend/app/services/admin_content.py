"""Generic, metadata-driven content manager for the admin panel.

Each content type declares its editable fields (type, constraints, help). The
admin UI renders forms from this metadata, and every write is validated here,
audited, versioned (item balance revisions) and propagated to all workers via
the content version. Temporary changes are stored as overrides with an
automatic expiry instead of modifying the base row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models as m
from ..content.registry import bump_content_version, get_registry
from ..core.errors import AppError, Conflict, NotFound
from ..core.security import Principal
from ..core.timeutil import utcnow
from . import audit


@dataclass(frozen=True)
class F:
    name: str
    type: str  # str/text/int/float/bool/json/enum/datetime
    label: str = ""
    required: bool = False
    choices: tuple[str, ...] = ()
    readonly: bool = False
    help: str = ""
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class ContentType:
    key: str
    label: str
    model: Any
    entity: str
    fields: tuple[F, ...]
    key_field: str = "key"
    search: tuple[str, ...] = ("key", "name")
    soft_delete: bool = True
    overridable: bool = True
    extra_filter: Any = None
    list_fields: tuple[str, ...] = ("key", "name")


TIERS = ("common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic", "admin")
STACK = ("add", "multiply", "queue", "highest")
EFFECTS = ("luck", "luck_mult", "min_rarity", "special_boost", "roll_speed", "cooldown_mult", "biome_chance", "table_flatten",
           "force_item", "best_of", "compounding_luck", "random_luck", "item_chance", "rarity_chance", "xp_mult", "duplicate",
           "reveal_rng", "reveal_secrets")

TYPES: dict[str, ContentType] = {
    "items": ContentType("items", "Items", m.Item, "item", (
        F("key", "str", "Key", True, help="英数字と_ (変更不可)"), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("lore", "text", "Lore"), F("kind", "enum", "種類", True, ("standard", "biome", "procedural_slot", "commemorative", "craft", "generated")),
        F("rarity_key", "enum", "レア度", True, TIERS), F("odds", "float", "基礎確率 (1/N のN)", min=1, help="空欄=Roll不可"),
        F("display_odds", "str", "表示確率", help="例: 1 / ???"), F("rollable", "bool", "Roll対象"),
        F("sell_value", "int", "売却価格", min=0), F("biome_keys", "json", "Biome限定", help='["void_rift"] 空=全Biome'),
        F("excluded_biome_keys", "json", "除外Biome"), F("min_luck", "float", "最低Luck条件", min=0),
        F("conditions", "json", "特殊条件", help='{"time":{"tz":"Asia/Tokyo","hours":[0,3]},"special_only":true,"biome_state":"x","event":"k"}'),
        F("luck_curve", "json", "Luck補正方式", help='{"mode":"power","exponent":0.8,"cap":1000} / linear / log / none'),
        F("effects", "json", "効果"), F("visual", "json", "ビジュアル", help='{"shape":"star","colors":["#fff","#aaa","#fff"],"fx":"sparkle"}'),
        F("animation", "str", "演出キー"), F("sound", "str", "サウンドキー"), F("tradeable", "bool", "取引可能"), F("hidden", "bool", "隠し"),
        F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
        F("discovery_count", "int", "発見回数", readonly=True), F("owner_count", "int", "所有者数", readonly=True),
        F("trade_count", "int", "取引回数", readonly=True), F("first_discoverer_id", "int", "世界初発見者ID", readonly=True),
    ), extra_filter=lambda q: q.where(m.Item.kind != "admin_artifact"), list_fields=("key", "name", "rarity_key", "odds", "kind", "is_active")),
    "biomes": ContentType("biomes", "Biomes", m.Biome, "biome", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("kind", "enum", "種類", True, ("default", "natural", "admin")), F("odds_per_sec", "float", "毎秒の出現確率 (1/N のN)", min=1),
        F("duration_sec", "int", "継続時間(秒)", min=1), F("luck_mult", "float", "Luck倍率", min=0), F("min_level", "int", "解放レベル", min=1),
        F("item_boosts", "json", "アイテム確率補正", help='{"item_key": 3}'), F("theme", "json", "テーマ/演出"),
        F("special_states", "json", "特殊状態", help='[{"key","name","odds_per_sec","duration_sec","luck_mult","theme"}]'),
        F("announce", "bool", "Feed通知"), F("hidden", "bool", "隠し"), F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "kind", "odds_per_sec", "luck_mult", "is_active")),
    "equipment": ContentType("equipment", "Equipment", m.Equipment, "equipment", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("slot", "enum", "スロット", True, ("gauntlet", "core", "relic")), F("rarity_key", "enum", "レア度", True, TIERS),
        F("luck_bonus", "float", "Luckボーナス (0.5=+50%)"), F("speed_bonus", "float", "Roll速度ボーナス"),
        F("passives", "json", "パッシブ", help='[{"type":"biome_luck","biome":"x","mult":1.5}]'), F("visual", "json", "ビジュアル"),
        F("sell_value", "int", "売却価格", min=0), F("min_level", "int", "必要レベル", min=1), F("sort_order", "int", "並び順"),
        F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "slot", "rarity_key", "luck_bonus", "is_active")),
    "boosts": ContentType("boosts", "Boosts", m.Boost, "boost", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("effect_type", "enum", "効果タイプ", True, EFFECTS), F("value", "float", "値 (luckは%)"),
        F("rolls", "int", "有効Roll数", min=1), F("duration_sec", "int", "有効時間(秒)", min=1), F("stack_mode", "enum", "重複時", True, STACK),
        F("biome_keys", "json", "発動Biome"), F("params", "json", "パラメータ"), F("rarity_key", "enum", "レア度", True, TIERS),
        F("visual", "json", "ビジュアル"), F("sell_value", "int", "価値", min=0), F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "effect_type", "value", "stack_mode", "is_active")),
    "recipes": ContentType("recipes", "Recipes", m.Recipe, "recipe", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("ingredients", "json", "素材", True, help='[{"item_key":"x","qty":3}]'), F("stardust_cost", "int", "費用", min=0),
        F("outputs", "json", "出力", True, help='[{"type":"equipment|boost|item","key":"x","qty":1,"weight":1}]'),
        F("hidden", "bool", "秘密レシピ"), F("hint", "text", "ヒント"), F("min_level", "int", "必要レベル", min=1),
        F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "hidden", "stardust_cost", "is_active")),
    "shops": ContentType("shops", "Shops", m.Shop, "shop", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("biome_key", "str", "Biome限定"), F("min_level", "int", "必要レベル", min=1), F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "biome_key", "is_active")),
    "shop_items": ContentType("shop_items", "Shop Items", m.ShopItem, "shop_item", (
        F("key", "str", "Key", True), F("shop_key", "str", "ショップKey", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("product_type", "enum", "商品種別", True, ("boost", "equipment", "unlock", "item")), F("product_key", "str", "商品Key", True),
        F("quantity", "int", "数量", min=1), F("price", "int", "価格", True, min=0), F("limit_count", "int", "購入上限", min=1),
        F("limit_period", "enum", "上限期間", choices=("daily", "lifetime")), F("min_level", "int", "必要レベル", min=1),
        F("requires_unlock", "str", "前提アンロック"), F("biome_key", "str", "Biome限定"), F("visual", "json", "ビジュアル"),
        F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "shop_key", "price", "is_active")),
    "achievements": ContentType("achievements", "Achievements", m.Achievement, "achievement", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"), F("category", "str", "カテゴリ", True),
        F("tier", "enum", "ティア", True, ("bronze", "silver", "gold", "cosmic")),
        F("condition", "json", "条件", True, help='{"stat":"total_rolls","gte":100} / {"rarity":"epic","gte":1} / {"item":"key"} / {"biome":"key"}'),
        F("rewards", "json", "報酬", help='{"stardust":100,"cosmetics":["t_x"],"items":[{"key":"x","qty":1}]}'),
        F("hidden", "bool", "隠し"), F("hint", "text", "ヒント"), F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
        F("achiever_count", "int", "達成者数", readonly=True),
    ), list_fields=("key", "name", "category", "tier", "is_active")),
    "quests": ContentType("quests", "Quests", m.Quest, "quest", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("kind", "enum", "種類", True, ("daily", "chain", "hidden")), F("chain_key", "str", "チェーンKey"), F("chain_step", "int", "ステップ", min=1),
        F("objective", "json", "目標", True, help='{"type":"roll","target":100,"per_level":5,"params":{}}'),
        F("rewards", "json", "報酬"), F("hint", "text", "ヒント"), F("weight", "float", "出現重み", min=0), F("min_level", "int", "必要レベル", min=1),
        F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "kind", "is_active")),
    "cosmetics": ContentType("cosmetics", "Cosmetics", m.Cosmetic, "cosmetic", (
        F("key", "str", "Key", True), F("kind", "enum", "種類", True, ("title", "badge", "background")), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"),
        F("description", "text", "説明"), F("rarity_key", "enum", "レア度", True, TIERS), F("visual", "json", "ビジュアル"), F("is_active", "bool", "有効"),
    ), list_fields=("key", "name", "kind", "rarity_key")),
    "artifacts": ContentType("artifacts", "Admin Artifacts", m.AdminArtifact, "artifact", (
        F("key", "str", "Key", True, readonly=True), F("ability", "text", "能力"), F("theme", "enum", "テーマ", True,
                                                                               ("cosmic", "divine", "void", "reality", "system", "time", "space")),
        F("effect", "json", "効果", True), F("target", "enum", "対象", True, ("self", "user", "global")),
        F("duration_sec", "int", "効果時間(秒)", min=1), F("cooldown_sec", "int", "クールダウン(秒)", min=0), F("tier", "int", "演出規模 1-5", min=1, max=5),
        F("player_usable", "bool", "プレイヤー使用可(付与時)"), F("equip_passive", "json", "装備時パッシブ"),
        F("transfer_rules", "json", "移動ルール"), F("audit_rules", "json", "監査ルール"), F("sort_order", "int", "並び順"), F("is_active", "bool", "有効"),
    ), search=("key", "ability"), list_fields=("key", "theme", "tier", "target", "is_active")),
    "events": ContentType("events", "Events", m.GameEvent, "event", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("type", "enum", "種類", True, ("luck_multiplier", "biome_chance", "item_event", "announcement")),
        F("params", "json", "パラメータ", help='{"mult":2}'), F("starts_at", "datetime", "開始"), F("ends_at", "datetime", "終了"),
        F("is_active", "bool", "有効"),
    ), overridable=False, list_fields=("key", "name", "type", "starts_at", "ends_at", "is_active")),
    "rarities": ContentType("rarities", "Rarities", m.Rarity, "rarity", (
        F("key", "str", "Key", True, readonly=True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("tier", "int", "ティア", readonly=True),
        F("min_odds", "float", "最低確率N", min=1), F("color", "str", "色1"), F("color2", "str", "色2"),
        F("luck_exponent", "float", "Luck指数", help="1未満でLuckが効きにくくなる", min=0.01, max=2), F("xp", "int", "XP", min=0),
        F("season_points", "int", "シーズンポイント", min=0), F("cutscene", "str", "演出"), F("announce", "bool", "通知"),
    ), soft_delete=False, list_fields=("key", "name", "tier", "min_odds", "luck_exponent")),
    "seasons": ContentType("seasons", "Seasons", m.Season, "season", (
        F("key", "str", "Key", True), F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("description", "text", "説明"),
        F("starts_at", "datetime", "開始", True), F("ends_at", "datetime", "終了", True),
        F("status", "enum", "状態", True, ("scheduled", "active", "ended")),
    ), soft_delete=False, overridable=False, list_fields=("key", "name", "starts_at", "ends_at", "status")),
    "parts": ContentType("parts", "Item Parts", m.ItemPart, "part", (
        F("part_type", "enum", "種類", True, ("material", "shape", "effect", "modifier")), F("key", "str", "Key", True),
        F("name", "str", "名前（英）", True), F("name_ja", "str", "名前（日）", help="英語名の隣に表示されます"), F("weight", "float", "重み", min=0.0001), F("value_mult", "float", "価値倍率", min=0),
        F("visual", "json", "ビジュアル"), F("is_active", "bool", "有効"),
    ), overridable=False, list_fields=("part_type", "key", "name", "weight", "is_active")),
}


def type_meta() -> list[dict[str, Any]]:
    return [{
        "key": t.key, "label": t.label, "overridable": t.overridable, "list_fields": list(t.list_fields),
        "fields": [{"name": f.name, "type": f.type, "label": f.label or f.name, "required": f.required, "choices": list(f.choices),
                    "readonly": f.readonly, "help": f.help, "min": f.min, "max": f.max} for f in t.fields],
    } for t in TYPES.values()]


def _ct(type_key: str) -> ContentType:
    t = TYPES.get(type_key)
    if t is None:
        raise NotFound("コンテンツタイプが見つかりません")
    return t


def _ser(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def row_dict(t: ContentType, obj: Any) -> dict[str, Any]:
    d = {c.key: _ser(getattr(obj, c.key)) for c in obj.__table__.columns}
    return d


def _coerce(f: F, v: Any) -> Any:
    if v is None or (isinstance(v, str) and v.strip() == "" and f.type not in ("str", "text")):
        if f.required:
            raise AppError(f"{f.label or f.name} は必須です", code="validation_error")
        return None
    try:
        if f.type in ("str", "text"):
            s = str(v)
            if f.type == "str" and len(s) > 200:
                raise ValueError("too long")
            if f.required and not s.strip():
                raise ValueError("required")
            return s
        if f.type == "int":
            if isinstance(v, bool):
                raise ValueError("int expected")
            out: Any = int(float(v))
        elif f.type == "float":
            out = float(v)
            if out != out or out in (float("inf"), float("-inf")):
                raise ValueError("finite number expected")
        elif f.type == "bool":
            if isinstance(v, bool):
                return v
            if str(v).lower() in ("true", "1", "yes", "on"):
                return True
            if str(v).lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError("bool expected")
        elif f.type == "enum":
            if str(v) not in f.choices:
                raise ValueError(f"one of {', '.join(f.choices)}")
            return str(v)
        elif f.type == "json":
            if isinstance(v, str):
                import json

                v = json.loads(v)
            if not isinstance(v, (dict, list)):
                raise ValueError("JSON object/array expected")
            return v
        elif f.type == "datetime":
            if isinstance(v, datetime):
                return v
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                from datetime import timezone

                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        else:
            raise ValueError("unknown type")
    except (TypeError, ValueError) as e:
        raise AppError(f"{f.label or f.name}: {e}", code="validation_error") from e
    if f.min is not None and out < f.min:
        raise AppError(f"{f.label or f.name}: {f.min}以上", code="validation_error")
    if f.max is not None and out > f.max:
        raise AppError(f"{f.label or f.name}: {f.max}以下", code="validation_error")
    return out


def validate(t: ContentType, data: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fields = {f.name: f for f in t.fields}
    for k, v in data.items():
        f = fields.get(k)
        if f is None or f.readonly:
            if f is not None and f.readonly and creating and f.name == t.key_field:
                out[k] = _coerce(f, v)
            continue
        out[k] = _coerce(f, v)
    if creating:
        for f in t.fields:
            if f.required and f.name not in out and not f.readonly:
                raise AppError(f"{f.label or f.name} は必須です", code="validation_error")
    key = out.get(t.key_field)
    if key is not None:
        import re

        if not re.match(r"^[a-z0-9_:\-]{2,64}$", str(key)):
            raise AppError("Keyは小文字英数字・_・-・: の2〜64文字です", code="validation_error")
    _semantic_checks(t, out)
    return out


def _semantic_checks(t: ContentType, d: dict[str, Any]) -> None:
    snap = get_registry().snap
    if t.key == "items":
        for b in (d.get("biome_keys") or []) + (d.get("excluded_biome_keys") or []):
            if b not in snap.biomes:
                raise AppError(f"未知のBiome: {b}", code="validation_error")
        if d.get("rollable") and d.get("odds") is None and "odds" in d:
            raise AppError("Roll対象には確率が必要です", code="validation_error")
    if t.key == "recipes":
        for ing in d.get("ingredients") or []:
            if not isinstance(ing, dict) or ing.get("item_key") not in snap.items_by_key or int(ing.get("qty", 0)) < 1:
                raise AppError(f"素材が不正です: {ing}", code="validation_error")
        for o in d.get("outputs") or []:
            if not isinstance(o, dict) or o.get("type") not in ("equipment", "boost", "item"):
                raise AppError(f"出力が不正です: {o}", code="validation_error")
    if t.key == "shop_items" and d.get("shop_key") and d["shop_key"] not in snap.shops:
        raise AppError("未知のショップです", code="validation_error")
    if t.key == "events" and d.get("starts_at") and d.get("ends_at") and d["ends_at"] <= d["starts_at"]:
        raise AppError("終了は開始より後にしてください", code="validation_error")
    if t.key == "seasons" and d.get("starts_at") and d.get("ends_at") and d["ends_at"] <= d["starts_at"]:
        raise AppError("終了は開始より後にしてください", code="validation_error")


async def list_rows(db: AsyncSession, type_key: str, q: str = "", page: int = 1, per_page: int = 50) -> dict[str, Any]:
    t = _ct(type_key)
    stmt = select(t.model)
    if t.extra_filter is not None:
        stmt = t.extra_filter(stmt)
    if q:
        conds = [getattr(t.model, f).ilike(f"%{q[:64]}%") for f in t.search if hasattr(t.model, f)]
        if conds:
            stmt = stmt.where(or_(*conds))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    order = getattr(t.model, "sort_order", None) or getattr(t.model, "id", None) or getattr(t.model, t.key_field)
    rows = (await db.execute(stmt.order_by(order, getattr(t.model, t.key_field)).limit(min(per_page, 200))
                             .offset((max(1, page) - 1) * per_page))).scalars().all()
    overrides = await active_overrides(db, t.entity)
    return {"rows": [{**row_dict(t, r), "_override": overrides.get(str(getattr(r, t.key_field)))} for r in rows], "total": total}


async def _get(db: AsyncSession, t: ContentType, key: str, lock: bool = False) -> Any:
    stmt = select(t.model).where(getattr(t.model, t.key_field) == key)
    if t.key == "parts" and ":" in key:
        ptype, pkey = key.split(":", 1)
        stmt = select(t.model).where(m.ItemPart.part_type == ptype, m.ItemPart.key == pkey)
    if lock:
        stmt = stmt.with_for_update()
    obj = (await db.execute(stmt)).scalar_one_or_none()
    if obj is None:
        raise NotFound("見つかりません")
    return obj


async def get_row(db: AsyncSession, type_key: str, key: str) -> dict[str, Any]:
    t = _ct(type_key)
    obj = await _get(db, t, key)
    overrides = await active_overrides(db, t.entity)
    return {**row_dict(t, obj), "_override": overrides.get(key)}


async def create_row(db: AsyncSession, principal: Principal, type_key: str, data: dict[str, Any], reason: str) -> dict[str, Any]:
    t = _ct(type_key)
    vals = validate(t, data, creating=True)
    exists = (await db.execute(select(t.model).where(getattr(t.model, t.key_field) == vals.get(t.key_field)))).scalar_one_or_none()
    if exists is not None and t.key != "parts":
        raise Conflict("同じKeyが既に存在します", code="duplicate_key")
    if t.key == "rarities":
        raise AppError("レア度の追加はできません（編集のみ）", code="not_supported")
    obj = t.model(**vals)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    await bump_content_version(db, principal.user.id)
    await audit.record(db, principal, "content_create", entity_type=t.entity, entity_id=vals.get(t.key_field), new=row_dict(t, obj), reason=reason)
    return row_dict(t, obj)


async def update_row(db: AsyncSession, principal: Principal, type_key: str, key: str, data: dict[str, Any], reason: str,
                     temporary_hours: float | None = None) -> dict[str, Any]:
    t = _ct(type_key)
    obj = await _get(db, t, key, lock=True)
    vals = validate(t, data, creating=False)
    vals.pop(t.key_field, None)
    if not vals:
        raise AppError("変更がありません", code="no_changes")
    old = {k: _ser(getattr(obj, k)) for k in vals}
    if temporary_hours:
        if not t.overridable:
            raise AppError("このコンテンツは一時変更に対応していません", code="not_overridable")
        if temporary_hours <= 0 or temporary_hours > 24 * 90:
            raise AppError("一時変更の期間が不正です", code="validation_error")
        expires = utcnow() + timedelta(hours=temporary_hours)
        ov = m.ContentOverride(entity_type=t.entity, entity_key=key, patch={k: _ser(v) for k, v in vals.items()}, reason=reason,
                               created_by=principal.user.id, expires_at=expires)
        db.add(ov)
        await db.flush()
        await bump_content_version(db, principal.user.id)
        await audit.record(db, principal, "content_override", entity_type=t.entity, entity_id=key, old=old,
                           new={"patch": ov.patch, "expires_at": expires}, reason=reason)
        return {**row_dict(t, obj), "_override": {"id": ov.id, "patch": ov.patch, "expires_at": expires.isoformat()}}
    for k, v in vals.items():
        setattr(obj, k, v)
    version = await bump_content_version(db, principal.user.id)
    if t.key == "items":
        db.add(m.ItemRevision(item_id=obj.id, content_version=version, snapshot={"old": old, "new": {k: _ser(v) for k, v in vals.items()}},
                              changed_by=principal.user.id))
    await audit.record(db, principal, "content_update", entity_type=t.entity, entity_id=key, old=old,
                       new={k: _ser(v) for k, v in vals.items()}, reason=reason)
    await db.flush()
    await db.refresh(obj)
    return row_dict(t, obj)


async def delete_row(db: AsyncSession, principal: Principal, type_key: str, key: str, reason: str) -> dict[str, Any]:
    t = _ct(type_key)
    obj = await _get(db, t, key, lock=True)
    before = row_dict(t, obj)
    if t.key == "rarities":
        raise AppError("レア度は削除できません", code="not_supported")
    if t.key == "biomes" and obj.kind == "default":
        raise AppError("デフォルトBiomeは削除できません", code="not_supported")
    if t.soft_delete and hasattr(obj, "is_active") and t.key not in ("events",):
        obj.is_active = False
        mode = "deactivated"
    else:
        await db.delete(obj)
        mode = "deleted"
    await bump_content_version(db, principal.user.id)
    await audit.record(db, principal, "content_delete", entity_type=t.entity, entity_id=key, old=before, new={"mode": mode}, reason=reason)
    return {"ok": True, "mode": mode}


async def active_overrides(db: AsyncSession, entity: str) -> dict[str, dict[str, Any]]:
    now = utcnow()
    rows = (await db.execute(select(m.ContentOverride).where(m.ContentOverride.entity_type == entity, m.ContentOverride.active.is_(True),
                                                             m.ContentOverride.expires_at > now))).scalars().all()
    return {r.entity_key: {"id": r.id, "patch": r.patch, "expires_at": r.expires_at.isoformat(), "reason": r.reason} for r in rows}


async def list_overrides(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(m.ContentOverride).order_by(m.ContentOverride.id.desc()).limit(200))).scalars().all()
    now = utcnow()
    return [{"id": r.id, "entity_type": r.entity_type, "entity_key": r.entity_key, "patch": r.patch, "reason": r.reason,
             "active": r.active and r.expires_at > now, "expires_at": r.expires_at.isoformat(), "created_at": r.created_at.isoformat(),
             "created_by": r.created_by} for r in rows]


async def revoke_override(db: AsyncSession, principal: Principal, override_id: int, reason: str) -> None:
    ov = await db.get(m.ContentOverride, override_id)
    if ov is None:
        raise NotFound("一時変更が見つかりません")
    ov.active = False
    await bump_content_version(db, principal.user.id)
    await audit.record(db, principal, "content_override_revoke", entity_type=ov.entity_type, entity_id=ov.entity_key, old=ov.patch, reason=reason)


async def expire_overrides(db: AsyncSession) -> int:
    """Called by the scheduler: deactivate expired overrides and bump the version so snapshots revert."""
    now = utcnow()
    rows = (await db.execute(select(m.ContentOverride).where(m.ContentOverride.active.is_(True), m.ContentOverride.expires_at <= now)
                             .with_for_update(skip_locked=True))).scalars().all()
    for r in rows:
        r.active = False
    if rows:
        await bump_content_version(db)
    await db.commit()
    return len(rows)

