"""Initial item content. Seeded into the DB once; afterwards the DB (admin panel) is authoritative.

Visual descriptor (rendered client-side as layered SVG, and server-side as PNG
for Discord):  {shape, colors: [primary, secondary, accent], glow, fx}
"""
from __future__ import annotations

from typing import Any

RARITIES: list[dict[str, Any]] = [
    {"id": 1, "key": "common", "name": "Common", "tier": 1, "min_odds": 1, "color": "#c3cee0", "color2": "#7d8aa0",
     "luck_exponent": 1.0, "xp": 1, "season_points": 1, "cutscene": "common", "announce": False},
    {"id": 2, "key": "rare", "name": "Rare", "tier": 2, "min_odds": 100, "color": "#56c8ff", "color2": "#2a63ff",
     "luck_exponent": 1.0, "xp": 5, "season_points": 5, "cutscene": "rare", "announce": False},
    {"id": 3, "key": "epic", "name": "Epic", "tier": 3, "min_odds": 1000, "color": "#c07bff", "color2": "#ff4fd8",
     "luck_exponent": 1.0, "xp": 20, "season_points": 25, "cutscene": "epic", "announce": False},
    {"id": 4, "key": "legendary", "name": "Legendary", "tier": 4, "min_odds": 10000, "color": "#ffd05a", "color2": "#ff7a1a",
     "luck_exponent": 0.97, "xp": 80, "season_points": 150, "cutscene": "legendary", "announce": True},
    {"id": 5, "key": "secret", "name": "Secret", "tier": 5, "min_odds": 100000, "color": "#4dffb8", "color2": "#00a6ff",
     "luck_exponent": 0.93, "xp": 300, "season_points": 1000, "cutscene": "secret", "announce": True},
    {"id": 6, "key": "ultra_secret", "name": "Ultra Secret", "tier": 6, "min_odds": 1000000, "color": "#ff5c8a", "color2": "#7a2cff",
     "luck_exponent": 0.88, "xp": 1200, "season_points": 10000, "cutscene": "ultra", "announce": True},
    {"id": 7, "key": "mythic", "name": "???", "tier": 7, "min_odds": 100000000, "color": "#ffffff", "color2": "#8a7bff",
     "luck_exponent": 0.8, "xp": 5000, "season_points": 100000, "cutscene": "mythic", "announce": True},
    {"id": 8, "key": "admin", "name": "Admin Artifact", "tier": 8, "min_odds": 1, "color": "#ffe9a8", "color2": "#ff3cac",
     "luck_exponent": 1.0, "xp": 0, "season_points": 0, "cutscene": "admin", "announce": True},
]


def tier_for_odds(odds: float) -> str:
    key = "common"
    for r in RARITIES:
        if r["key"] == "admin":
            continue
        if odds >= r["min_odds"]:
            key = r["key"]
    return key


def sell_for_odds(odds: float) -> int:
    return max(1, round(odds ** 0.75))


def V(shape: str, c1: str, c2: str, c3: str | None = None, glow: str | None = None, fx: str = "none") -> dict[str, Any]:
    return {"shape": shape, "colors": [c1, c2, c3 or c1], "glow": glow or c1, "fx": fx}


def I(
    key: str,
    name: str,
    odds: float,
    desc: str,
    visual: dict[str, Any],
    *,
    lore: str = "",
    biomes: list[str] | None = None,
    min_luck: float | None = None,
    conditions: dict[str, Any] | None = None,
    display_odds: str | None = None,
    hidden: bool = False,
    kind: str | None = None,
    rarity: str | None = None,
    animation: str | None = None,
    sound: str | None = None,
    luck_curve: dict[str, Any] | None = None,
    excluded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "odds": float(odds),
        "description": desc,
        "lore": lore,
        "visual": visual,
        "biome_keys": biomes or [],
        "excluded_biome_keys": excluded or [],
        "min_luck": min_luck,
        "conditions": conditions or {},
        "display_odds": display_odds,
        "hidden": hidden,
        "kind": kind or ("biome" if biomes else "standard"),
        "rarity_key": rarity or tier_for_odds(odds),
        "sell_value": sell_for_odds(odds),
        "animation": animation,
        "sound": sound,
        "luck_curve": luck_curve,
        "rollable": True,
    }


# ---------------------------------------------------------------------------
# General pool — obtainable in every natural biome
# ---------------------------------------------------------------------------
GENERAL_ITEMS: list[dict[str, Any]] = [
    I("cosmic_dust", "Cosmic Dust", 1, "宇宙を漂うありふれた塵。すべての星はここから始まった。", V("dust", "#8a94b8", "#4b5378", "#c9d2f0")),
    I("stardust_mote", "Stardust Mote", 2, "かすかに光る星屑の粒。", V("dust", "#d9e4ff", "#7f93d9", "#ffffff")),
    I("space_pebble", "Space Pebble", 3, "無重力を転がり続けた小石。", V("shard", "#9aa3b5", "#555e70")),
    I("ion_spark", "Ion Spark", 4, "帯電した微粒子。触れるとぱちりと弾ける。", V("bolt", "#7ff3ff", "#2b8cff", "#ffffff")),
    I("frozen_vapor", "Frozen Vapor", 5, "彗星から剥がれ落ちた凍った蒸気。", V("snowflake", "#d0f4ff", "#7cc4e8")),
    I("lunar_sand", "Lunar Sand", 6, "月面の砂。わずかに銀色を帯びる。", V("dust", "#e8e2cf", "#a39c86", "#fff8dd")),
    I("orbit_fragment", "Orbit Fragment", 8, "軌道上で砕けた人工物のかけら。", V("ring", "#a8b6d6", "#5e6f96")),
    I("nebula_wisp", "Nebula Wisp", 10, "星雲からこぼれた一筋のガス。", V("spiral", "#ff9ee6", "#8f6bff", "#ffd9f5")),
    I("photon_bead", "Photon Bead", 12, "光子を閉じ込めた小さなビーズ。", V("orb", "#fff6b0", "#ffcc33", "#ffffff")),
    I("quartz_meteorite", "Quartz Meteorite", 16, "石英を多く含む隕石。", V("crystal", "#e6ecff", "#a4b3e6")),
    I("solar_ember", "Solar Ember", 20, "太陽風に乗って届いた残り火。", V("flame", "#ffb347", "#ff5e1a", "#fff1b0")),
    I("comet_tail", "Comet Tail", 25, "彗星の尾の切れ端。まだ冷たく輝いている。", V("comet", "#b5f0ff", "#4fa8ff", "#ffffff")),
    I("gravity_pearl", "Gravity Pearl", 32, "重力が凝縮してできた真珠。見た目よりずっと重い。", V("orb", "#c9b8ff", "#5a3fc0", "#f0e8ff")),
    I("aurora_thread", "Aurora Thread", 40, "オーロラを紡いだ糸。", V("feather", "#8dffcf", "#3fd0ff", "#e0fff4")),
    I("void_pebble", "Void Pebble", 50, "光を吸い込む黒い小石。", V("shard", "#3a2d5c", "#0d0819", "#a47bff")),
    I("starlit_feather", "Starlit Feather", 64, "星明かりをまとった羽根。どこの鳥のものかは誰も知らない。", V("feather", "#fff4d6", "#ffc7f0", "#ffffff")),
    I("plasma_orb", "Plasma Orb", 80, "揺らめくプラズマの球体。", V("orb", "#ff7bf2", "#7b2cff", "#ffffff", fx="pulse")),
    # Rare
    I("celestial_prism", "Celestial Prism", 100, "天上の光を七色に分けるプリズム。", V("prism", "#a8f0ff", "#b58cff", "#ffffff", fx="sparkle")),
    I("moonstone_heart", "Moonstone Heart", 128, "月長石でできた心臓。静かに脈打つ。", V("heart", "#e8f0ff", "#8fa8ff", "#ffffff", fx="pulse")),
    I("ringed_crystal", "Ringed Crystal", 150, "土星の環のような光輪をもつ結晶。", V("planet", "#ffe7b0", "#c79a4f", "#fff8e0")),
    I("pulsar_core", "Pulsar Core", 200, "規則正しく明滅するパルサーの核。", V("atom", "#7fe8ff", "#2a5cff", "#ffffff", fx="pulse")),
    I("cosmic_lotus", "Cosmic Lotus", 250, "真空に咲く蓮。花弁の一枚一枚に星が宿る。", V("lotus", "#ffb3e6", "#ff5fb0", "#fff0fa", fx="sparkle")),
    I("quasar_shard", "Quasar Shard", 333, "クエーサーの輝きを宿した破片。", V("shard", "#fff1a8", "#ff9b3d", "#ffffff", fx="sparkle")),
    I("starforged_key", "Starforged Key", 400, "星の炉で鍛えられた鍵。何を開けるのかは不明。", V("key", "#ffd98a", "#c77b2a", "#fff5d6")),
    I("eclipse_ring", "Eclipse Ring", 500, "日食の瞬間を閉じ込めた指輪。", V("ring", "#1a1030", "#ffcf5a", "#fff0b3", fx="spin")),
    I("magnetar_eye", "Magnetar Eye", 666, "強磁場星の瞳。見つめ返してくる。", V("eye", "#b57bff", "#3a1aff", "#ff7bf2", fx="pulse")),
    I("chrono_sand", "Chrono Sand", 777, "時間を刻む砂。落ちる速さが一定ではない。", V("hourglass", "#ffe2a8", "#b8894a", "#fff8e6")),
    I("galactic_compass", "Galactic Compass", 888, "銀河の中心を指し示す羅針盤。", V("compass", "#cfe8ff", "#4a7bd6", "#ffd87a", fx="spin")),
    I("weekend_nebula", "Weekend Nebula", 400, "週末にだけ観測される不思議な星雲。", V("spiral", "#ffd0a8", "#ff7ab8", "#fff0e0"),
      conditions={"time": {"tz": "Asia/Tokyo", "weekdays": [5, 6]}}, hidden=True),
    # Epic
    I("supernova_remnant", "Supernova Remnant", 1000, "超新星爆発の残骸。今もなお膨張を続ける。", V("sun", "#ff9b5a", "#ff3d7f", "#fff0c0", fx="pulse")),
    I("dark_matter_cube", "Dark Matter Cube", 1250, "暗黒物質を立方体に閉じ込めたもの。重さを感じない。", V("cube", "#2d1a4f", "#7b3cff", "#c9a8ff", fx="glitch")),
    I("event_horizon_tear", "Event Horizon Tear", 1500, "事象の地平面からこぼれた一滴。", V("tear", "#1b0f33", "#8a4dff", "#ffb3ff", fx="orbit")),
    I("astral_crown", "Astral Crown", 2000, "星々が王と認めた者の冠。", V("crown", "#ffe08a", "#b57bff", "#ffffff", fx="sparkle")),
    I("resonance_shard", "Resonance Shard", 2000, "Special Rollの共鳴でのみ結晶化する欠片。", V("crystal", "#7affd4", "#3a8cff", "#ffffff", fx="pulse"),
      conditions={"special_only": True}, hidden=True),
    I("binary_star_pendant", "Binary Star Pendant", 2500, "互いを回り続ける二つの星のペンダント。", V("atom", "#ffe7a0", "#7fd4ff", "#ffffff", fx="orbit")),
    I("midnight_comet", "Midnight Comet", 3000, "深夜0時から3時の間だけ空を横切る彗星。", V("comet", "#6b7bff", "#1a1a5c", "#e0e6ff", fx="sparkle"),
      conditions={"time": {"tz": "Asia/Tokyo", "hours": [0, 3]}}, hidden=True),
    I("wormhole_gate", "Wormhole Gate", 3000, "時空の二点を結ぶ小さな門。", V("gate", "#5ce1ff", "#6a2cff", "#ffffff", fx="spin")),
    I("cosmic_phoenix_feather", "Cosmic Phoenix Feather", 4000, "星の死と再生を司る不死鳥の羽根。", V("wing", "#ff8a3d", "#ff2e63", "#fff0a0", fx="flame")),
    I("stellar_nursery", "Stellar Nursery", 5000, "新たな星が生まれる揺りかご。", V("galaxy", "#ff9ed8", "#7b6bff", "#fff5ff", fx="spin")),
    I("nova_sigil", "Nova Sigil", 6666, "新星の力を刻んだ紋章。", V("sigil", "#ffe066", "#ff4f9a", "#ffffff", fx="spin")),
    I("zenith_hourglass", "Zenith Hourglass", 8000, "天頂の時を測る砂時計。", V("hourglass", "#b8f0ff", "#6a8cff", "#ffffff", fx="sparkle")),
    # Legendary
    I("galaxy_in_a_bottle", "Galaxy in a Bottle", 10000, "小瓶の中で渦巻くひとつの銀河。", V("galaxy", "#b58cff", "#3a5cff", "#ffe8ff", fx="spin"),
      lore="ある天文学者が一生をかけて捕まえたと言われている。"),
    I("leviathan_scale", "Celestial Leviathan Scale", 15000, "銀河を泳ぐ巨獣の鱗。", V("shell", "#5cffd6", "#1a6bff", "#e0fff8", fx="sparkle")),
    I("starborn_halo", "Starborn Halo", 20000, "星から生まれた者だけが戴く光輪。", V("ring", "#fff4c2", "#ffc14d", "#ffffff", fx="spin")),
    I("chronos_gear", "Chronos Gear", 25000, "宇宙の時計を動かす歯車のひとつ。", V("compass", "#ffd27a", "#9c6b2a", "#fff5dc", fx="spin")),
    I("fortunes_edge", "Fortune's Edge", 30000, "Luckが10を超える者の前にのみ姿を現す刃。", V("bolt", "#fff08a", "#ff9e1a", "#ffffff", fx="sparkle"),
      min_luck=10, hidden=True),
    I("andromeda_tear", "Andromeda Tear", 33333, "アンドロメダが流した涙。", V("tear", "#a8d8ff", "#6a4dff", "#ffffff", fx="pulse")),
    I("quasar_heart", "Quasar Heart", 50000, "クエーサーの中心で脈打つ心臓。", V("heart", "#fff1a0", "#ff6a3d", "#ffffff", fx="pulse")),
    I("dyson_fragment", "Dyson Sphere Fragment", 75000, "恒星を覆う巨大構造物の破片。", V("cube", "#ffcf7a", "#5a4a8a", "#fff0c0", fx="orbit")),
    I("lucky_seven_relic", "Lucky Seven Relic", 77777, "七が七百七十七重なる時にだけ現れる遺物。", V("star", "#7aff9e", "#ffd84d", "#ffffff", fx="rainbow"),
      conditions={"hidden_special": "lucky_seven"}, hidden=True),
    # Secret
    I("primordial_star", "Primordial Star", 100000, "宇宙で最初に灯った星のひとつ。", V("star", "#ffffff", "#9ee8ff", "#fff8d0", fx="rainbow"),
      lore="その光は138億年を旅してきた。"),
    I("the_last_light", "The Last Light", 250000, "宇宙が終わる時、最後に消える光。", V("sun", "#fff6e0", "#ffb86b", "#ffffff", fx="pulse")),
    I("crown_of_the_cosmos", "Crown of the Cosmos", 500000, "宇宙そのものが戴く王冠。", V("crown", "#ffffff", "#ffcc4d", "#b57bff", fx="rainbow")),
    I("multiverse_key", "Multiverse Key", 777777, "無数の宇宙へ通じる鍵。", V("key", "#b3fff0", "#7b4dff", "#ffffff", fx="glitch")),
    I("luck_incarnate", "Luck Incarnate", 1500000, "Luck 100以上の者だけが触れられる幸運の化身。", V("lotus", "#fff8a0", "#7affc0", "#ffffff", fx="rainbow"),
      min_luck=100, hidden=True),
    # Ultra secret
    I("big_bang_echo", "Big Bang Echo", 1000000, "宇宙誕生の瞬間の残響。耳を澄ますと今も聞こえる。", V("galaxy", "#ffffff", "#ff7bd5", "#7be8ff", fx="rainbow")),
    I("infinity_ouroboros", "Infinity Ouroboros", 5000000, "自らの尾を喰らう無限の蛇。始まりも終わりもない。", V("ring", "#7affd4", "#b44dff", "#ffffff", fx="spin")),
    I("eye_of_the_universe", "Eye of the Universe", 20000000, "宇宙が自らを観測するための瞳。", V("eye", "#ffffff", "#5ce1ff", "#ff5cf0", fx="rainbow")),
    I("god_particle", "God Particle", 50000000, "万物に質量を与えた粒子。", V("atom", "#fff6c9", "#ffd24d", "#ffffff", fx="rainbow")),
    # ???
    I("the_origin", "The Origin", 100000000, "すべての始まり。それ以上は語られていない。", V("blackhole", "#ffffff", "#000000", "#ffd0ff", fx="void"),
      display_odds="1 / ???"),
    I("genesis_codex", "Genesis Codex", 250000000, "宇宙の法則が記された原典。読める者はいない。", V("rune", "#ffffff", "#ffb8f0", "#7be8ff", fx="rainbow"),
      display_odds="1 / ???"),
    I("heavens_gate", "Heaven's Gate", 200000000, "Luck 1000を超えた魂にのみ開かれる門。", V("gate", "#ffffff", "#ffe27a", "#b8f0ff", fx="rainbow"),
      min_luck=1000, hidden=True, display_odds="1 / ???"),
    I("omega", "OMEGA", 1000000000, "終わりの先にあるもの。", V("sigil", "#000000", "#ffffff", "#ff2e63", fx="void"),
      display_odds="1 / 1,000,000,000", animation="omega"),
]


# ---------------------------------------------------------------------------
# Biome exclusive items
# ---------------------------------------------------------------------------
BIOME_ITEMS: list[dict[str, Any]] = [
    # Nebula Bloom
    I("bloom_petal", "Bloom Petal", 60, "星雲に咲いた花の花弁。", V("flower", "#ffb8e8", "#ff6bc4"), biomes=["nebula_bloom"]),
    I("nebula_rose", "Nebula Rose", 900, "星雲の色をそのまま映した薔薇。", V("flower", "#ff7bd0", "#8f4dff", "#ffe0f5", fx="sparkle"), biomes=["nebula_bloom"]),
    I("pink_quasar", "Pink Quasar", 12000, "桃色に輝く稀有なクエーサー。", V("sun", "#ff9ee8", "#ff3da8", "#ffffff", fx="pulse"), biomes=["nebula_bloom"]),
    # Solar Flare
    I("flare_cinder", "Flare Cinder", 50, "太陽フレアの燃えさし。", V("flame", "#ffa04d", "#ff4d1a"), biomes=["solar_flare"]),
    I("corona_crown", "Corona Crown", 1500, "コロナを冠にしたもの。", V("crown", "#ffcf4d", "#ff6a1a", "#fff4c0", fx="flame"), biomes=["solar_flare"]),
    I("heart_of_the_sun", "Heart of the Sun", 40000, "太陽の中心核。直視してはいけない。", V("sun", "#fff4a0", "#ff7a1a", "#ffffff", fx="flame"), biomes=["solar_flare"]),
    # Meteor Storm
    I("meteor_chunk", "Meteor Chunk", 40, "降り注いだ隕石の塊。", V("shard", "#b8a08a", "#6a4d3a", "#ffb86b"), biomes=["meteor_storm"]),
    I("impact_crystal", "Impact Crystal", 700, "衝突の熱で生まれた結晶。", V("crystal", "#ffd0a0", "#ff6b3d", "#ffffff", fx="sparkle"), biomes=["meteor_storm"]),
    I("shooting_star_core", "Shooting Star Core", 9000, "流れ星の核。願いがひとつ込められている。", V("comet", "#fff4c0", "#ffb03d", "#ffffff", fx="sparkle"), biomes=["meteor_storm"]),
    # Aurora Veil
    I("aurora_silk", "Aurora Silk", 80, "オーロラを織った絹。", V("feather", "#8dffcf", "#3fd0ff"), biomes=["aurora_veil"]),
    I("borealis_lantern", "Borealis Lantern", 2500, "極光を灯すランタン。", V("bell", "#8dffd6", "#3a8cff", "#ffffff", fx="pulse"), biomes=["aurora_veil"]),
    I("veil_of_lights", "Veil of Lights", 60000, "天をまたぐ光のヴェール。", V("wing", "#9dffe0", "#b07bff", "#ffffff", fx="rainbow"), biomes=["aurora_veil"]),
    # Frozen Comet
    I("comet_ice", "Comet Ice", 45, "彗星の核から削れた氷。", V("snowflake", "#d8f6ff", "#7ac8ff"), biomes=["frozen_comet"]),
    I("glacial_heart", "Glacial Heart", 3000, "絶対に溶けない氷の心臓。", V("heart", "#e0faff", "#5cb8ff", "#ffffff", fx="sparkle"), biomes=["frozen_comet"]),
    I("absolute_frost", "Absolute Frost", 90000, "絶対零度に達した一片。", V("snowflake", "#ffffff", "#8ad8ff", "#c0f0ff", fx="pulse"), biomes=["frozen_comet"]),
    # Crimson Eclipse
    I("blood_moon_shard", "Blood Moon Shard", 300, "赤く染まった月の破片。", V("moon", "#ff5c5c", "#7a0f1f"), biomes=["crimson_eclipse"]),
    I("crimson_halo", "Crimson Halo", 8000, "紅い日食の光輪。", V("ring", "#ff3d5c", "#2a0510", "#ffb0b0", fx="spin"), biomes=["crimson_eclipse"]),
    I("red_giants_tear", "Red Giant's Tear", 150000, "赤色巨星が最期に流した涙。", V("tear", "#ff4d4d", "#ff9e3d", "#ffffff", fx="pulse"), biomes=["crimson_eclipse"]),
    # Quantum Foam
    I("qubit_bubble", "Qubit Bubble", 150, "0と1が同時に存在する泡。", V("orb", "#7afff0", "#3a7bff", "#ffffff", fx="glitch"), biomes=["quantum_foam"]),
    I("entangled_pair", "Entangled Pair", 4000, "どれだけ離れても繋がり合う二粒。", V("atom", "#7afff0", "#ff7bf0", "#ffffff", fx="orbit"), biomes=["quantum_foam"]),
    I("schrodingers_box", "Schrödinger's Box", 20000, "開けるまで中身が確定しない箱。", V("cube", "#b0fff4", "#6a4dff", "#ffffff", fx="glitch"), biomes=["quantum_foam"]),
    # Starfall
    I("fallen_star", "Fallen Star", 200, "地に落ちた小さな星。", V("star", "#ffe27a", "#ffb03d"), biomes=["starfall"]),
    I("wishing_star", "Wishing Star", 10000, "願いを叶えると言われる星。", V("star", "#fff6c0", "#ff9ee0", "#ffffff", fx="sparkle"), biomes=["starfall"]),
    I("star_sovereign", "Star Sovereign", 400000, "星々を統べる君主の証。", V("crown", "#fff6c0", "#ffd24d", "#ffffff", fx="rainbow"), biomes=["starfall"]),
    # Void Rift
    I("void_shard", "Void Shard", 100, "虚無の裂け目から零れた欠片。", V("shard", "#2a1a4f", "#0a0514", "#b07bff", fx="glitch"), biomes=["void_rift"]),
    I("rift_walker_mask", "Rift Walker's Mask", 25000, "裂け目を渡る者の仮面。", V("mask", "#1a1030", "#8a4dff", "#e0c0ff", fx="glitch"), biomes=["void_rift"]),
    I("null_heart", "Null Heart", 800000, "何も無いことを証明する心臓。", V("heart", "#0a0514", "#6a2cff", "#ff5cf0", fx="void"), biomes=["void_rift"]),
    I("abyssal_eye", "Abyssal Eye", 3000000, "深淵を覗く者を覗き返す眼。", V("eye", "#000000", "#8a2cff", "#ff2e8a", fx="void"), biomes=["void_rift"],
      conditions={"biome_state": "abyss_gaze"}, hidden=True),
    # Singularity
    I("compressed_star", "Compressed Star", 500, "特異点に圧縮された星。", V("orb", "#ffe0a0", "#6a2cff", "#ffffff", fx="orbit"), biomes=["singularity"]),
    I("spaghettified_light", "Spaghettified Light", 60000, "引き伸ばされた光の糸。", V("spiral", "#ffffff", "#8a4dff", "#5ce1ff", fx="spin"), biomes=["singularity"]),
    I("hawking_radiance", "Hawking Radiance", 2000000, "ブラックホールが放つ微かな輝き。", V("blackhole", "#1a0a33", "#ffcf7a", "#ffffff", fx="void"), biomes=["singularity"]),
    I("event_horizon_crown", "Event Horizon Crown", 10000000, "事象の地平面を戴く冠。", V("crown", "#000000", "#ffb84d", "#b07bff", fx="void"), biomes=["singularity"],
      conditions={"biome_state": "event_horizon"}, hidden=True),
    # Genesis
    I("genesis_spark", "Genesis Spark", 1000, "創世の火花。", V("bolt", "#ffffff", "#ffe27a", "#b8f0ff", fx="sparkle"), biomes=["genesis"]),
    I("first_word", "The First Word", 5000000, "宇宙が最初に発した言葉。", V("rune", "#ffffff", "#ffd0f0", "#b8f0ff", fx="rainbow"), biomes=["genesis"]),
    I("creators_fingerprint", "Creator's Fingerprint", 300000000, "創造主の指紋。", V("spiral", "#ffffff", "#ffe8a0", "#ffb8f0", fx="rainbow"), biomes=["genesis"],
      conditions={"biome_state": "big_bang"}, hidden=True, display_odds="1 / ???"),
    # Admin biomes
    I("architects_blueprint", "Architect's Blueprint", 5000, "世界の設計図の一枚。", V("compass", "#ffe9a8", "#5ce1ff", "#ffffff", fx="spin"), biomes=["architects_domain"]),
    I("golden_ratio", "Golden Ratio", 500000, "美の比率そのもの。", V("spiral", "#ffe27a", "#b8860b", "#ffffff", fx="rainbow"), biomes=["architects_domain"]),
    I("sanctum_relic", "Sanctum Relic", 50000, "虚無の聖域に祀られた遺物。", V("sigil", "#1a0a33", "#b07bff", "#ffffff", fx="void"), biomes=["void_sanctum"]),
    I("shattered_mirror", "Shattered Mirror", 2000, "砕けた現実の鏡。", V("prism", "#c0c0ff", "#ff5cf0", "#5ce1ff", fx="glitch"), biomes=["fractured_reality"]),
    I("paradox_shard", "Paradox Shard", 700000, "存在と非存在を同時に満たす欠片。", V("shard", "#ffffff", "#ff2e8a", "#5ce1ff", fx="glitch"), biomes=["fractured_reality"]),
    I("source_code_fragment", "Source Code Fragment", 1000000, "世界を記述するコードの断片。", V("rune", "#7affc0", "#000000", "#ffffff", fx="glitch"), biomes=["the_source"]),
    I("frozen_second", "Frozen Second", 10000, "永遠に止まった一秒。", V("hourglass", "#e0f0ff", "#5c8aff", "#ffffff", fx="pulse"), biomes=["chrono_vault"]),
]

# Procedural slots: when hit, a composite item is generated from item_parts.
PROCEDURAL_SLOTS: list[dict[str, Any]] = [
    {**I("forged_relic", "Forged Relic", 300, "素材・形状・効果が組み合わさって生まれる遺物。", V("rune", "#c0c8e0", "#7080a8")),
     "kind": "procedural_slot", "procedural": {"parts": ["effect", "material", "shape"], "base_value": 40}},
    {**I("stellar_artifact", "Stellar Artifact", 12000, "星の力を帯びた特別な遺物。", V("rune", "#ffe0a0", "#b07bff", fx="sparkle")),
     "kind": "procedural_slot", "procedural": {"parts": ["effect", "material", "shape", "modifier"], "base_value": 900, "prefix": "Stellar"}},
]

ITEM_PARTS: list[dict[str, Any]] = [
    # materials
    *[{"part_type": "material", "key": k, "name": n, "weight": w, "value_mult": vm, "visual": {"colors": c}} for k, n, w, vm, c in [
        ("iron", "Iron", 40, 1.0, ["#a8b0c0", "#5a6070"]),
        ("copper", "Copper", 30, 1.1, ["#e0a070", "#8a4a2a"]),
        ("silver", "Silver", 20, 1.3, ["#e8eef8", "#8a96b0"]),
        ("gold", "Gold", 10, 1.8, ["#ffe07a", "#b8860b"]),
        ("obsidian", "Obsidian", 5, 2.2, ["#3a2d5c", "#0d0819"]),
        ("moonsilver", "Moonsilver", 6, 2.0, ["#e8f0ff", "#8fa8ff"]),
        ("starsteel", "Starsteel", 4, 2.6, ["#c0e0ff", "#4a6aff"]),
        ("nebulite", "Nebulite", 3, 3.2, ["#ff9ee6", "#8f6bff"]),
        ("orichalcum", "Orichalcum", 1.5, 4.5, ["#ffb86b", "#ff5c8a"]),
        ("voidglass", "Voidglass", 0.8, 6.0, ["#2a1a4f", "#b07bff"]),
        ("aetherium", "Aetherium", 0.3, 10.0, ["#ffffff", "#7be8ff"]),
        ("neutronium", "Neutronium", 0.1, 18.0, ["#fff6c0", "#6a2cff"]),
    ]],
    # shapes
    *[{"part_type": "shape", "key": k, "name": n, "weight": w, "value_mult": vm, "visual": {"shape": s}} for k, n, w, vm, s in [
        ("ring", "Ring", 30, 1.0, "ring"), ("shard", "Shard", 30, 1.0, "shard"), ("orb", "Orb", 25, 1.1, "orb"),
        ("idol", "Idol", 15, 1.3, "rune"), ("prism", "Prism", 12, 1.4, "prism"), ("chalice", "Chalice", 8, 1.7, "bell"),
        ("sigil", "Sigil", 5, 2.0, "sigil"), ("crown", "Crown", 4, 2.4, "crown"), ("mask", "Mask", 3, 2.8, "mask"),
        ("astrolabe", "Astrolabe", 1.5, 4.0, "compass"),
    ]],
    # effects
    *[{"part_type": "effect", "key": k, "name": n, "weight": w, "value_mult": vm, "visual": {"fx": fx}} for k, n, w, vm, fx in [
        ("glowing", "Glowing", 30, 1.0, "none"), ("humming", "Humming", 25, 1.05, "pulse"), ("shimmering", "Shimmering", 12, 1.3, "sparkle"),
        ("radiant", "Radiant", 15, 1.25, "pulse"), ("resonant", "Resonant", 10, 1.5, "pulse"), ("frozen", "Frozen", 8, 1.6, "sparkle"),
        ("burning", "Burning", 8, 1.6, "flame"), ("cursed", "Cursed", 4, 2.2, "glitch"), ("blessed", "Blessed", 3, 2.6, "sparkle"),
        ("eternal", "Eternal", 1, 5.0, "rainbow"), ("paradoxical", "Paradoxical", 0.4, 9.0, "glitch"),
    ]],
    # modifiers
    *[{"part_type": "modifier", "key": k, "name": n, "weight": w, "value_mult": vm, "visual": {}} for k, n, w, vm in [
        ("dawn", "of Dawn", 20, 1.0), ("silence", "of Silence", 12, 1.2), ("void", "of the Void", 10, 1.4),
        ("andromeda", "of Andromeda", 6, 1.8), ("suns", "of Ten Thousand Suns", 3, 2.8), ("first_star", "of the First Star", 1, 6.0),
        ("infinity", "of Infinity", 0.5, 10.0),
    ]],
]

# Non-rollable special items
SPECIAL_ITEMS: list[dict[str, Any]] = [
    {**I("laurel_of_firsts", "Laurel of Firsts", 1, "実績を世界で最初に達成した者に贈られる記念の月桂冠。", V("crown", "#fff0a8", "#7affb0", "#ffffff", fx="sparkle")),
     "kind": "commemorative", "rarity_key": "legendary", "rollable": False, "sell_value": 0, "odds": None},
    {**I("pioneer_starmedal", "Pioneer's Starmedal", 1, "レアアイテムを世界で最初に発見した開拓者の勲章。", V("star", "#ffe27a", "#5ce1ff", "#ffffff", fx="rainbow")),
     "kind": "commemorative", "rarity_key": "secret", "rollable": False, "sell_value": 0, "odds": None},
    {**I("everbloom", "Everbloom", 1, "秘密のレシピでのみ生まれる、永遠に枯れない星の花。", V("lotus", "#ffb8f0", "#7affd4", "#ffffff", fx="rainbow")),
     "kind": "craft", "rarity_key": "legendary", "rollable": False, "sell_value": 25000, "odds": None},
]


def all_items() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, it in enumerate(GENERAL_ITEMS + BIOME_ITEMS + PROCEDURAL_SLOTS + SPECIAL_ITEMS):
        it = dict(it)
        it["sort_order"] = i
        if not it.get("rollable", True):
            it["odds"] = None
        out.append(it)
    return out
