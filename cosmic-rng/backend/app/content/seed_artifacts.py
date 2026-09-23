"""Admin Artifacts — never obtainable from rolls; granted only by administrators.

Each artifact is both an item (kind=admin_artifact, so it can be held, shown,
equipped) and an admin_artifacts row describing its ability.

effect.type values are implemented in services/artifacts.py.
tier 1..5 selects the cinematic scale (5 = ~30s "world alteration" sequence).
"""
from __future__ import annotations

from typing import Any

DEFAULT_TRANSFER = {"sellable": False, "tradeable": False, "giftable": False, "marketable": False, "recallable": True}
DEFAULT_AUDIT = {"log_use": True, "log_grant": True, "log_recall": True, "notify_admins": True}


def AA(key: str, name: str, theme: str, tier: int, lore: str, ability: str, effect: dict[str, Any], *,
       target: str = "self", duration: int | None = None, cooldown: int = 300, player_usable: bool = False,
       passive: dict[str, Any] | None = None, shape: str = "sigil", colors: list[str] | None = None) -> dict[str, Any]:
    palette = colors or {
        "cosmic": ["#b8a0ff", "#5ce1ff", "#ffffff"],
        "divine": ["#fff4c0", "#ffd24d", "#ffffff"],
        "void": ["#1a0a33", "#8a2cff", "#ff2e8a"],
        "reality": ["#ff5cf0", "#5ce1ff", "#ffffff"],
        "system": ["#7affc0", "#0a1a14", "#ffffff"],
        "time": ["#bfe0ff", "#ffd27a", "#ffffff"],
        "space": ["#5c8aff", "#0a0a2a", "#ffcf7a"],
    }[theme]
    return {
        "key": key,
        "item": {
            "key": f"aa_{key}", "name": name, "description": ability, "lore": lore, "kind": "admin_artifact", "rarity_key": "admin",
            "odds": None, "rollable": False, "tradeable": False, "sell_value": 0,
            "visual": {"shape": shape, "colors": palette, "glow": palette[0], "fx": "artifact", "theme": theme, "tier": tier},
            "animation": f"artifact_{theme}", "sound": f"artifact_{theme}",
        },
        "ability": ability, "theme": theme, "tier": tier, "effect": effect, "target": target, "duration_sec": duration,
        "cooldown_sec": cooldown, "player_usable": player_usable, "equip_passive": passive or {},
        "transfer_rules": dict(DEFAULT_TRANSFER), "audit_rules": dict(DEFAULT_AUDIT),
    }


ARTIFACTS: list[dict[str, Any]] = [
    AA("infinite_luck", "INFINITE LUCK", "cosmic", 4, "無限の幸運は、確率という概念そのものを嘲笑う。",
       "60秒間、Luckを×1,000,000にする。", {"type": "luck_mult", "value": 1_000_000}, duration=60, shape="lotus"),
    AA("time_breaker", "TIME BREAKER", "time", 3, "時は流れるものではない。砕くものだ。",
       "120秒間、Roll間隔を1/20にする。", {"type": "cooldown_mult", "value": 0.05}, duration=120, shape="hourglass"),
    AA("void_key", "VOID KEY", "void", 4, "開けてはならない扉のための、唯一の鍵。",
       "Void Sanctum(Luck×25)を300秒間開く。", {"type": "force_biome", "biome": "void_sanctum"}, duration=300, shape="key"),
    AA("reality_shift", "REALITY SHIFT", "reality", 4, "確率表は現実の一側面にすぎない。",
       "15 Rollの間、RNGテーブルを平坦化し希少アイテムの確率を大幅に引き上げる。", {"type": "table_flatten", "value": 0.6, "rolls": 15}, shape="prism"),
    AA("star_forger", "STAR FORGER", "cosmic", 3, "星を鍛える槌。打たれた星は二度と同じ形にならない。",
       "Secret級以上の特殊な自動生成アイテムを鍛造する。", {"type": "forge_item", "min_odds": 100000}, cooldown=600, shape="star"),
    AA("system_override", "SYSTEM OVERRIDE", "system", 5, "> sudo rewrite fate --force",
       "次のRoll結果を任意のアイテムに書き換える(プレイヤー使用時は最低Legendary保証)。", {"type": "force_item"}, cooldown=900, shape="rune"),
    AA("cosmic_eye", "COSMIC EYE", "cosmic", 2, "見えざるものを見る眼。数字の裏の真実を映す。",
       "600秒間、全アイテムの最終確率と次のBiome変化までの時間を表示する。", {"type": "reveal_rng"}, duration=600, shape="eye", player_usable=True),
    AA("world_fracture", "WORLD FRACTURE", "reality", 4, "世界は薄い硝子だった。",
       "個人Biomeを240秒間Fractured Reality(Luck×15)へ変化させる。", {"type": "force_biome", "biome": "fractured_reality"}, duration=240, shape="shard"),
    AA("chrono_loop", "CHRONO LOOP", "time", 3, "同じ瞬間を二度生き、より良い未来を選ぶ。",
       "10 Rollの間、各Rollを2回行いより希少な結果を採用する。", {"type": "best_of", "value": 2, "rolls": 10}, shape="ring", player_usable=True),
    AA("entropy_crown", "ENTROPY CROWN", "divine", 3, "秩序は崩れ、幸運は雪崩のように増えていく。",
       "30 Rollの間、Rollごとに Luck +25% が累積する。", {"type": "compounding_luck", "value": 0.25, "rolls": 30}, shape="crown"),
    AA("genesis_seed", "GENESIS SEED", "divine", 5, "宇宙の種。植えれば世界がもう一度始まる。",
       "最も希少な自然Biome「Genesis」を360秒間発生させる。", {"type": "force_biome", "biome": "genesis"}, duration=360, cooldown=1800, shape="flower"),
    AA("singularity_engine", "SINGULARITY ENGINE", "space", 4, "100の運命を一点に圧縮する機関。",
       "100回分のRollを一瞬で実行する。", {"type": "burst_roll", "count": 100}, cooldown=600, shape="blackhole"),
    AA("oracle_lens", "ORACLE LENS", "divine", 2, "未来を覗くレンズ。ただし覗いた未来は壊れやすい。",
       "次のRoll結果を事前に表示し、受け入れるか破棄して引き直すかを選べる(5回)。", {"type": "preview", "charges": 5}, shape="eye", player_usable=True),
    AA("aether_tap", "AETHER TAP", "cosmic", 1, "宇宙の根源エネルギーを直接汲み上げる蛇口。",
       "1,000,000 Stardustを生成する。", {"type": "grant_stardust", "amount": 1_000_000}, cooldown=3600, shape="tear"),
    AA("biome_conductor", "BIOME CONDUCTOR", "space", 3, "宇宙の気候を指揮するタクト。",
       "任意の自然Biomeを600秒間呼び出す。", {"type": "choose_biome"}, duration=600, shape="compass"),
    AA("probability_anchor", "PROBABILITY ANCHOR", "time", 2, "揺らぐ世界を一点に繋ぎ止める錨。",
       "現在のBiomeを30分間固定する。", {"type": "lock_biome"}, duration=1800, shape="anchor", player_usable=True),
    AA("luck_singularity", "LUCK SINGULARITY", "void", 5, "幸運が崩壊し、ひとつの点になった。",
       "次の1 RollのLuckを×1,000,000,000にする。", {"type": "luck_mult", "value": 1_000_000_000, "rolls": 1}, cooldown=1800, shape="blackhole"),
    AA("event_horizon", "EVENT HORIZON", "void", 5, "ここを越えれば、全ての者の運命が変わる。",
       "【全体】600秒間、全プレイヤーのLuckを×2にするワールドイベントを発生させる。", {"type": "global_event", "mult": 2.0},
       target="global", duration=600, cooldown=3600, shape="blackhole"),
    AA("starfall_call", "STARFALL CALL", "cosmic", 4, "星よ、降れ。全ての旅人の上に。",
       "【全体】オンラインの全プレイヤーに「次の1 Roll Luck +500%」を付与する。", {"type": "global_boost", "value": 500, "rolls": 1},
       target="global", cooldown=1800, shape="comet"),
    AA("null_codex", "NULL CODEX", "system", 3, "存在しないページを記した法典。下位の運命を無効化する。",
       "20 Rollの間、Epic未満のアイテムが出なくなる。", {"type": "min_rarity", "tier": "epic", "rolls": 20}, shape="rune"),
    AA("mirror_of_echoes", "MIRROR OF ECHOES", "reality", 2, "映したものを現実に写し取る鏡。",
       "次に獲得する3個のアイテムが複製される。", {"type": "duplicate", "rolls": 3}, shape="prism", player_usable=True),
    AA("divine_decree", "DIVINE DECREE", "divine", 3, "創造主の勅令。祝福は取り消せない。",
       "指定プレイヤーに称号「Blessed by the Architect」と1時間のLuck +100%を与える。", {"type": "bless", "title": "t_blessed", "value": 100},
       target="user", duration=3600, shape="sun"),
    AA("astral_compass", "ASTRAL COMPASS", "space", 2, "隠されたものの方角を指す羅針盤。",
       "1時間、隠しクエストの条件と秘密のレシピを表示する。", {"type": "reveal_secrets"}, duration=3600, shape="compass", player_usable=True),
    AA("quantum_dice", "QUANTUM DICE", "reality", 3, "振るまで出目が決まらないサイコロ。",
       "次の5 Rollの間、Luckが×1〜×1,000,000のランダム倍率になる。", {"type": "random_luck", "min": 1, "max": 1_000_000, "rolls": 5},
       shape="cube", player_usable=True),
    AA("celestial_anvil", "CELESTIAL ANVIL", "divine", 3, "神々が武具を鍛えた金床。",
       "指定した装備をGOD ROLL品質に鍛え直す。", {"type": "upgrade_equipment"}, cooldown=1800, shape="cube"),
    AA("eternity_glass", "ETERNITY GLASS", "time", 2, "永遠を閉じ込めた砂時計。",
       "有効中の時間制Boostの残り時間を全て+1時間延長する。", {"type": "extend_effects", "seconds": 3600}, shape="hourglass", player_usable=True),
    AA("halo_of_first_light", "HALO OF THE FIRST LIGHT", "divine", 3, "宇宙で最初に生まれた光でできた光輪。",
       "装備中 Luck×2。使用すると30分間 Luck +300%。", {"type": "luck_boost", "value": 300}, duration=1800, shape="ring",
       passive={"luck_mult": 2.0, "aura": "divine"}),
    AA("omega_protocol", "OMEGA PROTOCOL", "system", 5, "世界を終わらせ、そして再起動する最終手順。",
       "120秒間 The Sourceへ移動し、Luck×1兆・Roll間隔1/10となる究極プロトコル。",
       {"type": "multi", "effects": [{"type": "force_biome", "biome": "the_source"}, {"type": "luck_mult", "value": 1e12},
                                     {"type": "cooldown_mult", "value": 0.1}]},
       duration=120, cooldown=7200, shape="sigil", colors=["#ffffff", "#ff2e63", "#7affc0"]),
    AA("phantom_thread", "PHANTOM THREAD", "reality", 3, "運命の糸に一本、見えない糸を編み込む。",
       "指定プレイヤーの次のRollを最低Secret以上にする。", {"type": "target_min_rarity", "tier": "secret"}, target="user", cooldown=600, shape="spiral"),
    AA("architects_sigil", "ARCHITECT'S SIGIL", "system", 3, "世界の設計者の印章。",
       "Architect's Domainへ900秒間移動する。装備中 Luck×1.5 と設計者のオーラ。", {"type": "force_biome", "biome": "architects_domain"},
       duration=900, shape="sigil", passive={"luck_mult": 1.5, "aura": "system"}),
    AA("gravity_well", "GRAVITY WELL", "space", 2, "重力の井戸はBiomeさえも引き寄せる。",
       "600秒間 Biome出現率 ×50。", {"type": "biome_chance", "value": 50}, duration=600, shape="planet"),
    AA("temporal_key", "TEMPORAL KEY", "time", 3, "時の金庫を開く鍵。",
       "Chrono Vault(Luck×8)へ600秒間移動し、Roll間隔を1/2にする。",
       {"type": "multi", "effects": [{"type": "force_biome", "biome": "chrono_vault"}, {"type": "cooldown_mult", "value": 0.5}]},
       duration=600, shape="key"),
]
for i, a in enumerate(ARTIFACTS):
    a["sort_order"] = i
