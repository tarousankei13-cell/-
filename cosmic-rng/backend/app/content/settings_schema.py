"""Typed definitions for admin-editable game settings.

Every tunable balance/feature value lives here with its type, default,
constraints and a description. Values are stored in ``game_settings`` (JSONB)
and validated against these definitions when edited from the admin panel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import AppError

TIERS = ["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic"]


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # int/float/bool/str/enum/json/tier
    default: Any
    group: str
    label: str
    description: str = ""
    min: float | None = None
    max: float | None = None
    choices: list[str] = field(default_factory=list)
    dangerous: bool = False


S = SettingDef

DEFINITIONS: list[SettingDef] = [
    # --- features ---------------------------------------------------------
    S("features.maintenance_mode", "bool", False, "features", "メンテナンスモード", "有効中は管理者以外の書き込み操作を停止", dangerous=True),
    S("features.maintenance_message", "str", "現在メンテナンス中です。しばらくお待ちください。", "features", "メンテナンスメッセージ"),
    S("features.registration_open", "bool", True, "features", "新規登録受付", "無効にすると新規ユーザーはログインできません"),
    S("features.market_enabled", "bool", True, "features", "Market有効"),
    S("features.trade_enabled", "bool", True, "features", "Trade有効"),
    S("features.gift_enabled", "bool", True, "features", "Gift有効"),
    S("features.crafting_enabled", "bool", True, "features", "Crafting有効"),
    S("features.shop_enabled", "bool", True, "features", "Shop有効"),
    S("features.auto_roll_enabled", "bool", True, "features", "Auto Roll有効"),
    S("features.offline_roll_enabled", "bool", True, "features", "オフラインRoll有効"),
    S("features.biome_enabled", "bool", True, "features", "Biome変化有効"),
    S("features.world_feed_enabled", "bool", True, "features", "World Feed有効"),
    S("features.guest_rolls", "bool", True, "features", "お試しRoll有効", "未登録の訪問者が10回まで無料でRollできる"),
    # --- roll ---------------------------------------------------------------
    S("roll.base_seconds", "float", 1.0, "roll", "基本Roll間隔(秒)", "Roll Speed 100%時のクールダウン", 0.05, 60),
    S("roll.min_seconds", "float", 0.08, "roll", "最短Roll間隔(秒)", "装備・効果による短縮の下限", 0.01, 60),
    S("roll.tolerance_ms", "int", 120, "roll", "クールダウン許容誤差(ms)", "通信遅延の吸収", 0, 2000),
    S("roll.global_luck_mult", "float", 1.0, "roll", "全体Luck倍率", "全プレイヤーに適用", 0.01, 1e6),
    S("roll.special_interval", "int", 10, "roll", "Special Roll間隔", "N回に1回Special Roll", 2, 1000),
    S("roll.special_luck_mult", "float", 1.2, "roll", "Special Roll Luck倍率", "", 1, 100),
    S("roll.hidden_specials", "json", [{"key": "lucky_seven", "every": 777, "luck_mult": 2.0, "name": "Lucky Seven"}],
      "roll", "隠しSpecial Roll", "[{key, every, luck_mult, name}] — 公開しない特殊Roll"),
    S("roll.fallback_item", "str", "cosmic_dust", "roll", "フォールバックアイテム", "どの判定にも当たらなかった時のアイテムkey"),
    # --- luck ---------------------------------------------------------------
    S("luck.equipment_mode", "enum", "additive", "luck", "装備Luck合成方式", "additive: 1+Σbonus / multiplicative: Π(1+bonus)",
      choices=["additive", "multiplicative"]),
    # --- offline ------------------------------------------------------------
    S("offline.min_gap_seconds", "int", 45, "offline", "オフライン判定の最小間隔(秒)", "", 10, 3600),
    S("offline.max_hours", "float", 8, "offline", "オフライン最大時間(時間)", "アップグレードで延長可能", 0.1, 168),
    S("offline.efficiency", "float", 0.6, "offline", "オフライン効率", "オンライン時のRoll数に対する割合", 0.01, 1),
    S("offline.max_rolls", "int", 40000, "offline", "1回の最大オフラインRoll数", "", 1, 1000000),
    S("offline.log_min_tier", "tier", "epic", "offline", "個別ログ保存の最低レア度", "", choices=TIERS),
    S("offline.reveal_min_tier", "tier", "legendary", "offline", "復帰時演出の最低レア度", "", choices=TIERS),
    # --- biome --------------------------------------------------------------
    S("biome.global_chance_mult", "float", 1.0, "biome", "Biome出現率倍率(全体)", "", 0, 1e6),
    S("biome.announce_min_odds", "float", 50000, "biome", "Biome発生をFeed通知する最低レア度(1/N秒)", "", 1, 1e12),
    # --- inventory ----------------------------------------------------------
    S("inventory.capacity", "int", 3000, "inventory", "インベントリ容量", "超過分は自動売却(保護レア度以上は除く)", 50, 1_000_000),
    S("inventory.overflow_protect_tier", "tier", "legendary", "inventory", "容量超過でも保持する最低レア度", "", choices=TIERS),
    S("inventory.protect_new_default", "bool", True, "inventory", "未発見アイテムの自動削除保護(初期値)", ""),
    # --- feed / notify -------------------------------------------------------
    S("feed.min_tier", "tier", "legendary", "feed", "World Feed通知の最低レア度", "", choices=TIERS),
    S("feed.size", "int", 50, "feed", "World Feed表示件数", "", 10, 200),
    S("feed.procedural_first_min_tier", "tier", "legendary", "feed", "自動生成アイテムの世界初発見を通知する最低レア度", "", choices=TIERS),
    S("discord.enabled", "bool", False, "discord", "Discord DM通知", "DISCORD_BOT_TOKEN が必要"),
    S("discord.dm_targets", "json", [], "discord", "DM通知先Discord ID", "[\"123...\", ...]"),
    S("discord.min_tier", "tier", "secret", "discord", "Discord通知の最低レア度", "", choices=TIERS),
    S("discord.first_discovery_always", "bool", True, "discord", "世界初発見は常に通知", ""),
    S("discord.market_alerts", "bool", True, "discord", "Market異常をDM通知", ""),
    S("discord.fields", "json", {"image": True, "player": True, "odds": True, "biome": True, "luck": True, "time": True,
                                 "rarity": True, "first": True},
      "discord", "通知項目", "各項目のON/OFF"),
    # --- market --------------------------------------------------------------
    S("market.fee_pct", "float", 5.0, "market", "Market手数料(%)", "", 0, 50),
    S("market.max_listings", "int", 25, "market", "1人あたり最大出品数", "", 1, 1000),
    S("market.listing_days", "int", 7, "market", "出品期間(日)", "", 1, 60),
    S("market.max_price", "int", 1_000_000_000_000, "market", "最大価格", "", 1, 9e15),
    S("market.anomaly_high", "float", 80.0, "market", "異常高値判定倍率", "参照価格の何倍以上で検知", 1.5, 1e6),
    S("market.anomaly_low", "float", 0.02, "market", "異常安値判定倍率", "参照価格の何倍以下で検知", 0, 1),
    S("market.wash_threshold", "int", 5, "market", "同一ペア取引の検知回数(24h)", "", 2, 1000),
    S("market.daily_buy_limit", "int", 120, "market", "1日のMarket購入上限", "1人が1日に購入できる件数", 1, 100000),
    # --- trade / gift ---------------------------------------------------------
    S("trade.expiry_hours", "int", 24, "trade", "Trade有効期限(時間)", "", 1, 720),
    S("trade.max_items", "int", 20, "trade", "片側の最大アイテム数", "", 1, 200),
    S("trade.max_pending", "int", 10, "trade", "送信中Tradeの上限", "", 1, 100),
    S("trade.daily_limit", "int", 40, "trade", "1日のTrade成立上限", "1人が1日に成立できるTrade数", 1, 10000),
    S("trade.pair_daily_limit", "int", 8, "trade", "同一ペアの1日成立上限", "同じ相手との1日のTrade成立数", 1, 1000),
    S("gift.cooldown_seconds", "int", 60, "gift", "Giftクールダウン(秒)", "", 0, 86400),
    S("gift.daily_limit", "int", 20, "gift", "1日のGift上限", "", 1, 1000),
    # --- progression ------------------------------------------------------------
    S("progression.unlocks", "json", {
        "inventory": 1, "collection": 1, "biome": 1, "profile": 1, "ranking": 1, "achievements": 1,
        "shop": 2, "auto_delete": 2, "equipment": 3, "crafting": 4, "auto_roll": 3, "quests": 5,
        "market": 8, "trade": 10, "gift": 10, "advanced_rng": 15, "relic_slot": 20,
    }, "progression", "機能解放レベル", "{feature: level}"),
    S("progression.xp_base", "int", 60, "progression", "レベル曲線係数", "level = floor(sqrt(xp / base)) + 1", 1, 100000),
    S("progression.max_level", "int", 200, "progression", "最大レベル", "", 1, 10000),
    # --- quests ---------------------------------------------------------------------
    S("quests.daily_count", "int", 4, "quests", "デイリークエスト数", "", 0, 20),
    S("quests.reset_timezone", "str", "Asia/Tokyo", "quests", "デイリーリセットのタイムゾーン"),
    # --- gameplay economy -----------------------------------------------------------
    S("economy.starting_stardust", "int", 250, "economy", "初期Stardust", "", 0, 1e12),
    S("economy.sell_mult", "float", 1.0, "economy", "売却価格倍率", "", 0, 1000),
    # --- retention ----------------------------------------------------------------
    S("retention.roll_log_days", "int", 21, "retention", "Rollログ保存日数", "保護レア度未満のRollログ", 1, 3650),
    S("backup.auto_enabled", "bool", True, "retention", "自動バックアップ", "1日1回、スケジューラが自動でバックアップを取得"),
    S("retention.roll_log_keep_tier", "tier", "epic", "retention", "永久保存するRollの最低レア度", "", choices=TIERS),
    S("retention.feed_days", "int", 60, "retention", "Feed保存日数", "", 1, 3650),
    # --- security ------------------------------------------------------------------
    S("security.max_ws_per_user", "int", 5, "security", "ユーザーあたり最大WebSocket接続数", "", 1, 50),
]

DEF_MAP: dict[str, SettingDef] = {d.key: d for d in DEFINITIONS}
DEFAULTS: dict[str, Any] = {d.key: d.default for d in DEFINITIONS}


def validate_setting(key: str, value: Any) -> Any:
    d = DEF_MAP.get(key)
    if d is None:
        raise AppError(f"未知の設定です: {key}", code="unknown_setting")
    t = d.type
    try:
        if t == "bool":
            if not isinstance(value, bool):
                raise ValueError("boolean expected")
            return value
        if t == "int":
            if isinstance(value, bool):
                raise ValueError("integer expected")
            v: Any = int(value)
            if float(v) != float(value):
                raise ValueError("integer expected")
        elif t == "float":
            if isinstance(value, bool):
                raise ValueError("number expected")
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError("finite number expected")
        elif t in ("str",):
            v = str(value)
            if len(v) > 2000:
                raise ValueError("too long")
            return v
        elif t in ("enum", "tier"):
            v = str(value)
            if v not in d.choices:
                raise ValueError(f"must be one of {d.choices}")
            return v
        elif t == "json":
            if not isinstance(value, (dict, list)):
                raise ValueError("object or array expected")
            return value
        else:  # pragma: no cover
            raise ValueError("unknown type")
    except (TypeError, ValueError) as e:
        raise AppError(f"{d.label}: {e}", code="invalid_setting") from e
    if d.min is not None and v < d.min:
        raise AppError(f"{d.label}: {d.min} 以上にしてください", code="invalid_setting")
    if d.max is not None and v > d.max:
        raise AppError(f"{d.label}: {d.max} 以下にしてください", code="invalid_setting")
    return v


def definitions_public() -> list[dict[str, Any]]:
    return [
        {
            "key": d.key, "type": d.type, "default": d.default, "group": d.group, "label": d.label,
            "description": d.description, "min": d.min, "max": d.max, "choices": d.choices, "dangerous": d.dangerous,
        }
        for d in DEFINITIONS
    ]
