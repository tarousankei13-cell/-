"""Initial biomes, equipment, boosts, recipes, shops and cosmetics."""
from __future__ import annotations

from typing import Any


def T(bg: list[str], nebula: list[str], accent: str, accent2: str, particles: str, bgm: str, *,
      pcolor: str = "#ffffff", intensity: float = 0.5, fx: str = "none", vignette: float = 0.4) -> dict[str, Any]:
    return {"bg": bg, "nebula": nebula, "accent": accent, "accent2": accent2, "particles": particles,
            "particle_color": pcolor, "intensity": intensity, "bgm": bgm, "fx": fx, "vignette": vignette}


BIOMES: list[dict[str, Any]] = [
    {"key": "stellar_drift", "name": "Stellar Drift", "kind": "default", "odds_per_sec": None, "duration_sec": 0, "luck_mult": 1.0,
     "description": "穏やかな星々が漂う、いつもの宇宙。",
     "theme": T(["#03040c", "#070b24", "#140f3a"], ["#1d2a6b", "#4b2d8a", "#0f4a7a"], "#8ab4ff", "#b58cff", "stars", "drift", intensity=0.35)},
    {"key": "nebula_bloom", "name": "Nebula Bloom", "odds_per_sec": 400, "duration_sec": 150, "luck_mult": 1.1,
     "description": "星雲が花開き、宇宙が桃色に染まる。", "item_boosts": {"nebula_wisp": 4, "cosmic_lotus": 3},
     "theme": T(["#12051a", "#2a0b36", "#4a1250"], ["#ff5cc8", "#8f4dff", "#ff9ee6"], "#ff8ad8", "#b58cff", "petals", "bloom", pcolor="#ffc0ec", intensity=0.6)},
    {"key": "solar_flare", "name": "Solar Flare", "odds_per_sec": 600, "duration_sec": 120, "luck_mult": 1.15,
     "description": "恒星が荒れ狂い、灼熱の風が吹き荒れる。", "item_boosts": {"solar_ember": 5, "supernova_remnant": 2},
     "theme": T(["#140400", "#3a0f00", "#5c1a05"], ["#ff5e1a", "#ffb347", "#ff2e2e"], "#ffb347", "#ff5e3a", "embers", "solar", pcolor="#ffcf7a", intensity=0.7, fx="heat")},
    {"key": "meteor_storm", "name": "Meteor Storm", "odds_per_sec": 800, "duration_sec": 120, "luck_mult": 1.2,
     "description": "無数の流星が降り注ぐ嵐。", "item_boosts": {"space_pebble": 3, "quartz_meteorite": 4},
     "theme": T(["#05060e", "#12142a", "#241a2a"], ["#5c4a8a", "#ff8a3d", "#2a3d7a"], "#ffb86b", "#8ab4ff", "meteors", "storm", pcolor="#ffe0b0", intensity=0.8)},
    {"key": "aurora_veil", "name": "Aurora Veil", "odds_per_sec": 1200, "duration_sec": 180, "luck_mult": 1.25,
     "description": "宇宙にかかる極光のヴェール。", "item_boosts": {"aurora_thread": 5},
     "theme": T(["#010a0c", "#03202a", "#0a1a3a"], ["#3fffb0", "#3fd0ff", "#8a5cff"], "#6dffcf", "#5cc8ff", "aurora", "aurora", pcolor="#a0ffe0", intensity=0.6)},
    {"key": "frozen_comet", "name": "Frozen Comet", "odds_per_sec": 1500, "duration_sec": 150, "luck_mult": 1.2,
     "description": "巨大な彗星の氷片が静かに舞う。", "item_boosts": {"frozen_vapor": 5, "comet_tail": 3},
     "theme": T(["#030a14", "#0a1a30", "#16304a"], ["#8ad8ff", "#c0f0ff", "#4a7aff"], "#bfeaff", "#8ab4ff", "snow", "frost", pcolor="#e8f8ff", intensity=0.5)},
    {"key": "crimson_eclipse", "name": "Crimson Eclipse", "odds_per_sec": 3000, "duration_sec": 180, "luck_mult": 1.5, "min_level": 5,
     "description": "紅い日食が宇宙を覆う。不吉な力が満ちている。", "item_boosts": {"eclipse_ring": 4},
     "theme": T(["#0c0003", "#2a0008", "#3d0510"], ["#ff1a3d", "#5c0010", "#ff5c3d"], "#ff4d6a", "#ff9e5c", "ash", "eclipse", pcolor="#ff8a8a", intensity=0.7, vignette=0.7)},
    {"key": "quantum_foam", "name": "Quantum Foam", "odds_per_sec": 5000, "duration_sec": 150, "luck_mult": 1.6, "min_level": 8,
     "description": "時空が泡立ち、確率が揺らぐ。", "item_boosts": {"dark_matter_cube": 3},
     "theme": T(["#000a0c", "#002a30", "#0a1a3a"], ["#3afff0", "#3a7bff", "#ff5cf0"], "#5cfff0", "#ff7bf0", "bubbles", "quantum", pcolor="#b0fff8", intensity=0.7, fx="glitch")},
    {"key": "starfall", "name": "Starfall", "odds_per_sec": 8000, "duration_sec": 120, "luck_mult": 2.0, "min_level": 10,
     "description": "星が雨のように降る奇跡の夜。", "item_boosts": {"starlit_feather": 4, "primordial_star": 1.5},
     "theme": T(["#0a0800", "#1f1a05", "#2a1f0a"], ["#ffd24d", "#ffb03d", "#fff4c0"], "#ffe27a", "#ffb86b", "starfall", "starfall", pcolor="#fff0b0", intensity=0.9)},
    {"key": "void_rift", "name": "Void Rift", "odds_per_sec": 30000, "duration_sec": 240, "luck_mult": 3.0, "min_level": 15, "announce": True,
     "description": "宇宙に走る虚無の裂け目。深淵がこちらを見ている。", "item_boosts": {"void_pebble": 6, "event_horizon_tear": 3},
     "special_states": [{"key": "abyss_gaze", "name": "Abyss Gaze", "odds_per_sec": 90, "duration_sec": 20, "luck_mult": 2.0,
                         "description": "深淵が目を開いた。", "theme": {"fx": "invert", "accent": "#ff2e8a"}}],
     "theme": T(["#000000", "#0a0514", "#1a0a33"], ["#6a2cff", "#1a0a33", "#ff2e8a"], "#b07bff", "#ff5cf0", "glitch", "void", pcolor="#c0a0ff", intensity=0.9, fx="glitch", vignette=0.8)},
    {"key": "singularity", "name": "Singularity", "odds_per_sec": 150000, "duration_sec": 300, "luck_mult": 5.0, "min_level": 20, "announce": True,
     "description": "すべてが一点へ落ちていく。光さえも。",
     "special_states": [{"key": "event_horizon", "name": "Event Horizon", "odds_per_sec": 60, "duration_sec": 25, "luck_mult": 3.0,
                         "description": "事象の地平面を越えた。", "theme": {"fx": "warp", "accent": "#ffb84d"}}],
     "theme": T(["#000000", "#05000f", "#140a2a"], ["#ffb84d", "#6a2cff", "#000000"], "#ffcf7a", "#8a4dff", "vortex", "singularity", pcolor="#ffe0a0", intensity=1.0, fx="warp", vignette=0.85)},
    {"key": "genesis", "name": "Genesis", "odds_per_sec": 2000000, "duration_sec": 360, "luck_mult": 10.0, "min_level": 25, "announce": True, "hidden": True,
     "description": "宇宙が生まれ直す瞬間。",
     "special_states": [{"key": "big_bang", "name": "Big Bang", "odds_per_sec": 45, "duration_sec": 30, "luck_mult": 5.0,
                         "description": "始まりの爆発。", "theme": {"fx": "pulse", "accent": "#ffffff"}}],
     "theme": T(["#ffffff", "#fff4e0", "#ffd0f0"], ["#ffffff", "#ffd0f0", "#b8f0ff"], "#fff4c0", "#ffb8f0", "genesis", "genesis", pcolor="#ffffff", intensity=1.0, fx="pulse", vignette=0.2)},
    # Admin biomes — never occur naturally
    {"key": "architects_domain", "name": "Architect's Domain", "kind": "admin", "odds_per_sec": None, "duration_sec": 900, "luck_mult": 10.0, "announce": True,
     "description": "世界を設計する者の領域。黄金比の幾何学が空間を満たす。",
     "theme": T(["#0a0800", "#1a1405", "#2a2008"], ["#ffe27a", "#5ce1ff", "#ffffff"], "#ffe9a8", "#5ce1ff", "geometry", "architect", pcolor="#ffe9a8", intensity=0.9)},
    {"key": "void_sanctum", "name": "Void Sanctum", "kind": "admin", "odds_per_sec": None, "duration_sec": 300, "luck_mult": 25.0, "announce": True,
     "description": "VOID KEYでのみ開かれる、虚無の聖域。",
     "theme": T(["#000000", "#05000a", "#12001f"], ["#8a2cff", "#000000", "#ff2e8a"], "#c07bff", "#ff2e8a", "vortex", "sanctum", pcolor="#d0a0ff", intensity=1.0, fx="warp", vignette=0.9)},
    {"key": "fractured_reality", "name": "Fractured Reality", "kind": "admin", "odds_per_sec": None, "duration_sec": 240, "luck_mult": 15.0, "announce": True,
     "description": "現実が砕け、世界の継ぎ目が露わになった。",
     "theme": T(["#050005", "#14001a", "#001a1a"], ["#ff2e8a", "#5ce1ff", "#ffffff"], "#ff5cf0", "#5ce1ff", "glitch", "fracture", pcolor="#ffffff", intensity=1.0, fx="glitch", vignette=0.6)},
    {"key": "the_source", "name": "The Source", "kind": "admin", "odds_per_sec": None, "duration_sec": 120, "luck_mult": 100.0, "announce": True,
     "description": "すべてのコードが流れ出す場所。",
     "theme": T(["#000000", "#001a0a", "#000a05"], ["#7affc0", "#ffffff", "#5ce1ff"], "#7affc0", "#ffffff", "code", "source", pcolor="#7affc0", intensity=1.0, fx="pulse", vignette=0.5)},
    {"key": "chrono_vault", "name": "Chrono Vault", "kind": "admin", "odds_per_sec": None, "duration_sec": 600, "luck_mult": 8.0, "announce": True,
     "description": "時間が保管された金庫。秒針の音だけが響く。",
     "theme": T(["#02050a", "#0a1428", "#141e3a"], ["#5c8aff", "#e0f0ff", "#ffd27a"], "#bfe0ff", "#ffd27a", "clock", "chrono", pcolor="#e0f0ff", intensity=0.7)},
]

for i, b in enumerate(BIOMES):
    b.setdefault("kind", "natural")
    b.setdefault("min_level", 1)
    b.setdefault("item_boosts", {})
    b.setdefault("special_states", [])
    b.setdefault("announce", False)
    b.setdefault("hidden", False)
    b["sort_order"] = i


def E(key: str, name: str, slot: str, rarity: str, luck: float, speed: float, desc: str, visual: dict[str, Any],
      passives: list[dict[str, Any]] | None = None, min_level: int = 1, sell: int = 0) -> dict[str, Any]:
    return {"key": key, "name": name, "slot": slot, "rarity_key": rarity, "luck_bonus": luck, "speed_bonus": speed,
            "description": desc, "visual": visual, "passives": passives or [], "min_level": min_level, "sell_value": sell}


def EV(shape: str, c1: str, c2: str, *, aura: str | None = None, roll_effect: str | None = None, ui_theme: str | None = None,
       background: str | None = None, particles: str | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {"shape": shape, "colors": [c1, c2, c1], "glow": c1}
    if aura:
        v["aura"] = aura
    if roll_effect:
        v["roll_effect"] = roll_effect
    if ui_theme:
        v["ui_theme"] = ui_theme
    if background:
        v["background"] = background
    if particles:
        v["particles"] = particles
    return v


EQUIPMENT: list[dict[str, Any]] = [
    # Gauntlets — luck focused
    E("stargazer_glove", "Stargazer Glove", "gauntlet", "common", 0.10, 0.0, "星を見上げる者の手袋。", EV("glove", "#c0d0ff", "#6a7ab8"), sell=100),
    E("comet_gauntlet", "Comet Gauntlet", "gauntlet", "rare", 0.25, 0.05, "彗星の軌跡を宿す篭手。", EV("glove", "#8ad8ff", "#3a6aff", aura="#8ad8ff"), sell=800),
    E("nebula_grasp", "Nebula Grasp", "gauntlet", "epic", 0.60, 0.0, "星雲を掴む手。Nebula Bloom中はさらに輝く。",
      EV("glove", "#ff8ad8", "#8f4dff", aura="#ff8ad8", roll_effect="petals"),
      [{"type": "biome_luck", "biome": "nebula_bloom", "mult": 1.5}], sell=4000),
    E("solar_fist", "Solar Fist", "gauntlet", "epic", 0.50, 0.10, "太陽の拳。Solar Flare中はLuck +100%。",
      EV("glove", "#ffb347", "#ff4d1a", aura="#ffb347", roll_effect="embers"),
      [{"type": "biome_luck", "biome": "solar_flare", "mult": 2.0}], sell=4000),
    E("gravity_gauntlet", "Gravity Gauntlet", "gauntlet", "legendary", 1.20, 0.0, "重力を操る篭手。5回に1回Luck×1.3。",
      EV("glove", "#b58cff", "#3a1aff", aura="#b58cff", roll_effect="gravity"),
      [{"type": "nth_roll_luck", "every": 5, "mult": 1.3}], min_level=8, sell=25000),
    E("eclipse_hand", "Eclipse Hand", "gauntlet", "legendary", 1.50, 0.0, "日食の手。Special Rollの効果を強化する。",
      EV("glove", "#ffcf5a", "#1a1030", aura="#ffcf5a", roll_effect="eclipse"),
      [{"type": "special_bonus", "add": 0.3}], min_level=10, sell=30000),
    E("hand_of_the_cosmos", "Hand of the Cosmos", "gauntlet", "secret", 4.0, 0.20, "宇宙そのものの手。装備者の世界を銀河で満たす。",
      EV("glove", "#ffffff", "#7b4dff", aura="#b8a0ff", roll_effect="galaxy", ui_theme="cosmic", particles="galaxy"),
      [{"type": "biome_chance", "mult": 1.2}], min_level=18, sell=250000),
    E("genesis_gauntlet", "Genesis Gauntlet", "gauntlet", "ultra_secret", 10.0, 0.35, "創世の篭手。装備者の周囲で宇宙が生まれ続ける。",
      EV("glove", "#ffffff", "#ffd0f0", aura="#ffffff", roll_effect="genesis", ui_theme="genesis", background="genesis", particles="genesis"),
      [{"type": "biome_chance", "mult": 1.5}, {"type": "state_luck", "mult": 1.5}], min_level=25, sell=5000000),
    # Cores — roll speed focused
    E("ion_core", "Ion Core", "core", "common", 0.0, 0.10, "イオン推進のコア。Roll速度+10%。", EV("core", "#7ff3ff", "#2b8cff"), sell=80),
    E("plasma_core", "Plasma Core", "core", "rare", 0.05, 0.20, "プラズマを燃やすコア。", EV("core", "#ff7bf2", "#7b2cff", aura="#ff7bf2"), sell=700),
    E("pulsar_engine", "Pulsar Engine", "core", "epic", 0.0, 0.40, "パルサーの鼓動で駆動するエンジン。", EV("core", "#7fe8ff", "#2a5cff", aura="#7fe8ff", roll_effect="pulse"), sell=5000),
    E("quasar_reactor", "Quasar Reactor", "core", "legendary", 0.30, 0.70, "クエーサーを炉心にした反応炉。",
      EV("core", "#fff1a8", "#ff9b3d", aura="#fff1a8", roll_effect="quasar"), min_level=10, sell=35000),
    E("chrono_engine", "Chrono Engine", "core", "secret", 0.20, 1.20, "時間を加速させる機関。オフライン効率+20%。",
      EV("core", "#ffe2a8", "#6a8cff", aura="#ffe2a8", roll_effect="clock", particles="clock"),
      [{"type": "offline_efficiency", "add": 0.2}], min_level=15, sell=200000),
    E("singularity_drive", "Singularity Drive", "core", "ultra_secret", 1.0, 2.0, "特異点を推進力に変えるドライブ。",
      EV("core", "#ffcf7a", "#6a2cff", aura="#ffcf7a", roll_effect="vortex", ui_theme="singularity", background="singularity", particles="vortex"),
      [{"type": "state_luck", "mult": 1.3}], min_level=22, sell=2000000),
    # Relics — passives
    E("lucky_star_charm", "Lucky Star Charm", "relic", "rare", 0.15, 0.0, "幸運の星のお守り。Special Rollが9回に1回になる。",
      EV("star", "#7aff9e", "#ffd84d", aura="#7aff9e"), [{"type": "special_interval", "delta": -1}], sell=1500),
    E("biome_compass", "Biome Compass", "relic", "epic", 0.0, 0.0, "Biomeを引き寄せる羅針盤。Biome出現率×1.3。",
      EV("compass", "#cfe8ff", "#4a7bd6", aura="#cfe8ff"), [{"type": "biome_chance", "mult": 1.3}], sell=6000),
    E("auto_sifter", "Auto Sifter", "relic", "epic", 0.05, 0.0, "自動売却の価値+50%。", EV("prism", "#e6ecff", "#a4b3e6"),
      [{"type": "sell_bonus", "add": 0.5}], sell=4000),
    E("moonlit_amulet", "Moonlit Amulet", "relic", "epic", 0.30, 0.0, "夜(20時〜5時 JST)の間Luck×1.5。",
      EV("moon", "#e8f0ff", "#6a7bff", aura="#e8f0ff"), [{"type": "night_luck", "tz": "Asia/Tokyo", "hours": [20, 5], "mult": 1.5}], sell=8000),
    E("fortune_idol", "Fortune Idol", "relic", "legendary", 0.80, 0.0, "幸運の偶像。3回に1回Luck×1.15、獲得XP+25%。",
      EV("rune", "#ffe066", "#ff9e1a", aura="#ffe066"), [{"type": "nth_roll_luck", "every": 3, "mult": 1.15}, {"type": "xp_bonus", "add": 0.25}],
      min_level=12, sell=50000),
    E("eye_of_providence", "Eye of Providence", "relic", "secret", 2.0, 0.0, "全てを見通す眼。特殊状態中のLuck×1.5。",
      EV("eye", "#ffffff", "#ffd24d", aura="#ffe9a8", roll_effect="eye"), [{"type": "state_luck", "mult": 1.5}, {"type": "biome_chance", "mult": 1.2}],
      min_level=18, sell=300000),
    E("paradox_clock", "Paradox Clock", "relic", "secret", 1.0, 0.50, "過去と未来を同時に指す時計。オフライン効率+25%。",
      EV("hourglass", "#b8f0ff", "#ff5cf0", aura="#b8f0ff", roll_effect="clock"), [{"type": "offline_efficiency", "add": 0.25}],
      min_level=15, sell=400000),
    # --- second wave ------------------------------------------------------
    E("meteor_knuckle", "Meteor Knuckle", "gauntlet", "rare", 0.20, 0.12, "落下する星の重みを拳に乗せる。",
      EV("glove", "#ffb98a", "#8a3a1a", aura="#ffb98a"), sell=900),
    E("frostbite_grip", "Frostbite Grip", "gauntlet", "epic", 0.55, 0.0, "凍てつく握り。Frozen Comet中はLuck×1.8。",
      EV("glove", "#bff0ff", "#2a6aa8", aura="#bff0ff", roll_effect="frost"),
      [{"type": "biome_luck", "biome": "frozen_comet", "mult": 1.8}], sell=4200),
    E("aurora_palm", "Aurora Palm", "gauntlet", "legendary", 1.10, 0.15, "極光を編んだ手。Aurora Veil中はLuck×1.8、獲得XP+15%。",
      EV("glove", "#8affd8", "#3a8aff", aura="#8affd8", roll_effect="aurora"),
      [{"type": "biome_luck", "biome": "aurora_veil", "mult": 1.8}, {"type": "xp_bonus", "add": 0.15}], min_level=9, sell=28000),
    E("drift_core", "Drift Core", "core", "rare", 0.0, 0.16, "慣性だけで回り続ける静かなコア。", EV("core", "#cfe0ff", "#5a6ab8"), sell=750),
    E("storm_dynamo", "Storm Dynamo", "core", "epic", 0.10, 0.34, "Ion Storm中はLuck×1.6。雷を燃料にする発電機。",
      EV("core", "#a8e8ff", "#3a3aff", aura="#a8e8ff", roll_effect="spark"),
      [{"type": "biome_luck", "biome": "ion_storm", "mult": 1.6}], sell=5200),
    E("starfall_turbine", "Starfall Turbine", "core", "legendary", 0.25, 0.62, "降り注ぐ星を受けて回る羽根。自動売却の価値+40%。",
      EV("core", "#ffe6a8", "#ff7ab8", aura="#ffe6a8", roll_effect="starfall"),
      [{"type": "sell_bonus", "add": 0.4}], min_level=10, sell=33000),
    E("collectors_lens", "Collector's Lens", "relic", "rare", 0.08, 0.0, "収集家のレンズ。獲得XP+20%。",
      EV("prism", "#ffe9c0", "#b8874a"), [{"type": "xp_bonus", "add": 0.2}], sell=1600),
    E("echo_locket", "Echo Locket", "relic", "epic", 0.35, 0.0, "4回に1回Luck×1.2で反響する首飾り。",
      EV("moon", "#e0d8ff", "#7a5cff", aura="#e0d8ff"), [{"type": "nth_roll_luck", "every": 4, "mult": 1.2}], sell=7000),
    E("prism_sigil", "Prism Sigil", "relic", "legendary", 0.90, 0.0, "七色に割れる紋章。Biome出現率×1.4、Special Rollが1回早まる。",
      EV("rune", "#ffffff", "#7affd8", aura="#d8fff0", roll_effect="prism"),
      [{"type": "biome_chance", "mult": 1.4}, {"type": "special_interval", "delta": -1}], min_level=12, sell=60000),
]
for i, e in enumerate(EQUIPMENT):
    e["sort_order"] = i


def B(key: str, name: str, effect: str, value: float, desc: str, rarity: str, *, rolls: int | None = None,
      duration: int | None = None, stack: str = "add", biomes: list[str] | None = None, params: dict[str, Any] | None = None,
      visual: dict[str, Any] | None = None, sell: int = 0) -> dict[str, Any]:
    return {"key": key, "name": name, "effect_type": effect, "value": value, "description": desc, "rarity_key": rarity,
            "rolls": rolls, "duration_sec": duration, "stack_mode": stack, "biome_keys": biomes or [], "params": params or {},
            "visual": visual or {}, "sell_value": sell}


BOOSTS: list[dict[str, Any]] = [
    B("starlight_candle", "Starlight Candle", "luck", 20, "60分間 Luck +20%。", "common", duration=3600,
      visual={"shape": "flame", "colors": ["#fff4c0", "#ffb86b", "#ffffff"]}, sell=100),
    B("stellar_tonic", "Stellar Tonic", "luck", 50, "15分間 Luck +50%。", "rare", duration=900,
      visual={"shape": "tear", "colors": ["#8ad8ff", "#3a6aff", "#ffffff"]}, sell=500),
    B("fortune_elixir", "Fortune Elixir", "luck", 100, "30分間 Luck +100%。", "epic", duration=1800,
      visual={"shape": "tear", "colors": ["#ffe066", "#ff9e1a", "#ffffff"]}, sell=4000),
    B("next_roll_boost", "NEXT ROLL BOOST", "luck", 1000, "次の1 RollのみLuck +1000%。複数同時使用で加算。", "epic", rolls=1,
      visual={"shape": "bolt", "colors": ["#ff7bf2", "#7b2cff", "#ffffff"], "fx": "pulse"}, sell=1600),
    B("triple_charm", "Triple Charm", "luck", 300, "次の3 Roll Luck +300%。", "rare", rolls=3,
      visual={"shape": "star", "colors": ["#7aff9e", "#3ad0ff", "#ffffff"]}, sell=1200),
    B("cosmic_stock", "Cosmic Stock", "luck", 200, "次の5 Roll Luck +200%。ストック式: 複数使用すると順番に消費される。", "rare", rolls=5, stack="queue",
      visual={"shape": "cube", "colors": ["#b58cff", "#3a5cff", "#ffffff"]}, sell=2400),
    B("celestial_surge", "Celestial Surge", "luck", 10000, "次の1 RollのみLuck +10000%。倍率式: 他のBoostと掛け合わされる。", "legendary", rolls=1, stack="multiply",
      visual={"shape": "sun", "colors": ["#ffffff", "#ffd24d", "#ff7bf2"], "fx": "rainbow"}, sell=80000),
    B("rare_guarantee", "Rare Guarantee", "min_rarity", 0, "次の1 Rollは最低でもRare以上。", "rare", rolls=1, stack="highest", params={"tier": "rare"},
      visual={"shape": "diamond", "colors": ["#56c8ff", "#2a63ff", "#ffffff"]}, sell=600),
    B("epic_guarantee", "Epic Guarantee", "min_rarity", 0, "次の1 Rollは最低でもEpic以上。", "legendary", rolls=1, stack="highest", params={"tier": "epic"},
      visual={"shape": "diamond", "colors": ["#c07bff", "#ff4fd8", "#ffffff"], "fx": "sparkle"}, sell=30000),
    B("special_amplifier", "Special Amplifier", "special_boost", 2.0, "次のSpecial RollのLuck倍率 +2.0。Special Rollでのみ消費。", "rare", rolls=1, stack="queue",
      visual={"shape": "atom", "colors": ["#7affd4", "#3a8cff", "#ffffff"]}, sell=1000),
    B("overdrive_chip", "Overdrive Chip", "roll_speed", 50, "10分間 Roll速度 +50%。", "rare", duration=600, stack="multiply",
      visual={"shape": "core", "colors": ["#7ff3ff", "#2b8cff", "#ffffff"]}, sell=800),
    B("chrono_capsule", "Chrono Capsule", "roll_speed", 100, "5分間 Roll速度 +100%。", "epic", duration=300, stack="multiply",
      visual={"shape": "hourglass", "colors": ["#ffe2a8", "#6a8cff", "#ffffff"]}, sell=3000),
    B("biome_magnet", "Biome Magnet", "biome_chance", 3.0, "15分間 Biome出現率 ×3。", "epic", duration=900, stack="highest",
      visual={"shape": "compass", "colors": ["#cfe8ff", "#4a7bd6", "#ffffff"]}, sell=3000),
    B("nebula_incense", "Nebula Incense", "luck", 150, "Nebula Bloom中のみ、次の10 Roll Luck +150%。", "rare", rolls=10, biomes=["nebula_bloom"],
      visual={"shape": "flower", "colors": ["#ff8ad8", "#8f4dff", "#ffffff"]}, sell=1500),
    B("solar_lens", "Solar Lens", "luck", 300, "Solar Flare中のみ、次の5 Roll Luck +300%。", "rare", rolls=5, biomes=["solar_flare"],
      visual={"shape": "sun", "colors": ["#ffb347", "#ff4d1a", "#ffffff"]}, sell=2000),
    B("void_resonator", "Void Resonator", "luck", 800, "Void Rift / Singularity中のみ、次の3 Roll Luck +800%。", "legendary", rolls=3,
      biomes=["void_rift", "singularity"], visual={"shape": "blackhole", "colors": ["#2a1a4f", "#b07bff", "#ff5cf0"], "fx": "void"}, sell=10000),
]
for i, b in enumerate(BOOSTS):
    b["sort_order"] = i


def R(key: str, name: str, ingredients: list[tuple[str, int]], outputs: list[dict[str, Any]], cost: int = 0, *, desc: str = "",
      hidden: bool = False, hint: str | None = None, min_level: int = 1) -> dict[str, Any]:
    return {"key": key, "name": name, "description": desc, "ingredients": [{"item_key": k, "qty": q} for k, q in ingredients],
            "outputs": outputs, "stardust_cost": cost, "hidden": hidden, "hint": hint, "min_level": min_level}


def OUT(type_: str, key: str, qty: int = 1, weight: float = 1.0) -> dict[str, Any]:
    return {"type": type_, "key": key, "qty": qty, "weight": weight}


RECIPES: list[dict[str, Any]] = [
    R("r_stargazer_glove", "Stargazer Glove", [("stardust_mote", 10), ("space_pebble", 5)], [OUT("equipment", "stargazer_glove")], 100),
    R("r_ion_core", "Ion Core", [("ion_spark", 8), ("orbit_fragment", 3)], [OUT("equipment", "ion_core")], 50),
    R("r_comet_gauntlet", "Comet Gauntlet", [("comet_tail", 5), ("gravity_pearl", 3), ("celestial_prism", 1)], [OUT("equipment", "comet_gauntlet")], 1000),
    R("r_plasma_core", "Plasma Core", [("plasma_orb", 5), ("photon_bead", 3)], [OUT("equipment", "plasma_core")], 800),
    R("r_lucky_star_charm", "Lucky Star Charm", [("starlit_feather", 7), ("magnetar_eye", 1)], [OUT("equipment", "lucky_star_charm")], 2000),
    R("r_nebula_grasp", "Nebula Grasp", [("nebula_wisp", 10), ("cosmic_lotus", 2), ("bloom_petal", 3)], [OUT("equipment", "nebula_grasp")], 5000),
    R("r_solar_fist", "Solar Fist", [("solar_ember", 10), ("flare_cinder", 3), ("pulsar_core", 1)], [OUT("equipment", "solar_fist")], 5000),
    R("r_pulsar_engine", "Pulsar Engine", [("pulsar_core", 3), ("quasar_shard", 2)], [OUT("equipment", "pulsar_engine")], 6000),
    R("r_auto_sifter", "Auto Sifter", [("quartz_meteorite", 20), ("void_pebble", 5)], [OUT("equipment", "auto_sifter")], 5000),
    R("r_biome_compass", "Biome Compass", [("galactic_compass", 1), ("meteor_chunk", 3), ("flare_cinder", 3), ("aurora_silk", 3)],
      [OUT("equipment", "biome_compass")], 8000),
    R("r_moonlit_amulet", "Moonlit Amulet", [("moonstone_heart", 2), ("lunar_sand", 20)], [OUT("equipment", "moonlit_amulet")], 10000),
    R("r_gravity_gauntlet", "Gravity Gauntlet", [("gravity_pearl", 10), ("supernova_remnant", 1), ("dark_matter_cube", 1)],
      [OUT("equipment", "gravity_gauntlet")], 25000, min_level=8),
    R("r_eclipse_hand", "Eclipse Hand", [("eclipse_ring", 3), ("blood_moon_shard", 2), ("event_horizon_tear", 1)],
      [OUT("equipment", "eclipse_hand")], 30000, min_level=10),
    R("r_quasar_reactor", "Quasar Reactor", [("quasar_shard", 3), ("stellar_nursery", 1), ("zenith_hourglass", 1)],
      [OUT("equipment", "quasar_reactor")], 40000, min_level=10),
    R("r_fortune_idol", "Fortune Idol", [("fortunes_edge", 1), ("nova_sigil", 1), ("astral_crown", 1)], [OUT("equipment", "fortune_idol")], 60000, min_level=12),
    R("r_chrono_engine", "Chrono Engine", [("chronos_gear", 1), ("chrono_sand", 5), ("zenith_hourglass", 1)], [OUT("equipment", "chrono_engine")], 200000, min_level=15),
    R("r_eye_of_providence", "Eye of Providence", [("magnetar_eye", 3), ("andromeda_tear", 1), ("quasar_heart", 1)],
      [OUT("equipment", "eye_of_providence")], 300000, min_level=18),
    R("r_hand_of_the_cosmos", "Hand of the Cosmos", [("galaxy_in_a_bottle", 1), ("astral_crown", 1), ("wormhole_gate", 2)],
      [OUT("equipment", "hand_of_the_cosmos")], 250000, min_level=18),
    R("r_singularity_drive", "Singularity Drive", [("compressed_star", 3), ("spaghettified_light", 1), ("dyson_fragment", 1)],
      [OUT("equipment", "singularity_drive")], 2000000, min_level=22),
    R("r_genesis_gauntlet", "Genesis Gauntlet", [("primordial_star", 1), ("genesis_spark", 5), ("the_last_light", 1)],
      [OUT("equipment", "genesis_gauntlet")], 5000000, min_level=25),
    # Boost recipes
    R("r_next_roll_boost", "NEXT ROLL BOOST", [("celestial_prism", 1), ("aurora_thread", 5)], [OUT("boost", "next_roll_boost")], 0),
    R("r_rare_guarantee", "Rare Guarantee", [("plasma_orb", 3), ("gravity_pearl", 3)], [OUT("boost", "rare_guarantee")], 200),
    R("r_fortune_elixir", "Fortune Elixir", [("quasar_shard", 1), ("cosmic_lotus", 1), ("solar_ember", 10)], [OUT("boost", "fortune_elixir")], 1000),
    # Random recipes
    R("r_cosmic_gamble", "Cosmic Gamble", [("plasma_orb", 10)],
      [OUT("boost", "triple_charm", 1, 70), OUT("boost", "next_roll_boost", 1, 25), OUT("boost", "celestial_surge", 1, 5)], 1000,
      desc="結果はランダム。運が良ければCelestial Surge。"),
    R("r_stellar_fusion", "Stellar Fusion", [("supernova_remnant", 3)],
      [OUT("equipment", "nebula_grasp", 1, 50), OUT("equipment", "pulsar_engine", 1, 45), OUT("equipment", "gravity_gauntlet", 1, 5)], 10000,
      desc="超新星の残骸を融合させる。何が生まれるかは星次第。", min_level=6),
    # Secret recipes — discovered through Experimental Fusion
    R("r_everbloom", "Everbloom", [("nebula_rose", 1), ("cosmic_lotus", 1), ("moonstone_heart", 1)], [OUT("item", "everbloom")], 5000,
      hidden=True, hint="星雲の薔薇と宇宙の蓮、そして月の心臓。三つの花が出会う時…"),
    R("r_paradox_clock", "Paradox Clock", [("chrono_sand", 7), ("zenith_hourglass", 1), ("chronos_gear", 1)], [OUT("equipment", "paradox_clock")], 150000,
      hidden=True, hint="時を刻むものを三つ重ねよ。砂は七つ。", min_level=15),
    R("r_prism_cascade", "Prism Cascade", [("celestial_prism", 3), ("photon_bead", 7)], [OUT("boost", "celestial_surge")], 20000,
      hidden=True, hint="三つのプリズムに七つの光を通すと、光は天へ昇る。"),
]
for i, r in enumerate(RECIPES):
    r["sort_order"] = i


SHOPS: list[dict[str, Any]] = [
    {"key": "stellar_exchange", "name": "Stellar Exchange", "description": "星間交易所。基本的なBoostと装備を扱う。", "sort_order": 0},
    {"key": "filter_lab", "name": "Filter Lab", "description": "Roll体験を最適化するモジュールとアップグレード。", "sort_order": 1},
    {"key": "nebula_bazaar", "name": "Nebula Bazaar", "description": "Nebula Bloomの間だけ現れる露店。", "biome_key": "nebula_bloom", "sort_order": 2},
    {"key": "solar_forge", "name": "Solar Forge", "description": "Solar Flareの熱で動く鍛冶場。", "biome_key": "solar_flare", "sort_order": 3},
    {"key": "void_market", "name": "Void Market", "description": "虚無の裂け目の向こうの闇市。", "biome_key": "void_rift", "min_level": 15, "sort_order": 4},
    {"key": "cosmic_atelier", "name": "Cosmic Atelier", "description": "称号・バッジ・背景を仕立てる工房。毎日ひとつが目玉として割引される。", "sort_order": 5},
]


def P(key: str, shop: str, name: str, ptype: str, pkey: str, price: int, desc: str = "", *, qty: int = 1, limit: int | None = None,
      period: str | None = None, min_level: int = 1, requires: str | None = None, visual: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "shop_key": shop, "name": name, "product_type": ptype, "product_key": pkey, "price": price, "description": desc,
            "quantity": qty, "limit_count": limit, "limit_period": period, "min_level": min_level, "requires_unlock": requires,
            "visual": visual or {}}


SHOP_ITEMS: list[dict[str, Any]] = [
    P("s_starlight_candle", "stellar_exchange", "Starlight Candle", "boost", "starlight_candle", 500),
    P("s_stellar_tonic", "stellar_exchange", "Stellar Tonic", "boost", "stellar_tonic", 2500),
    P("s_triple_charm", "stellar_exchange", "Triple Charm", "boost", "triple_charm", 6000),
    P("s_next_roll_boost", "stellar_exchange", "NEXT ROLL BOOST", "boost", "next_roll_boost", 8000),
    P("s_cosmic_stock", "stellar_exchange", "Cosmic Stock", "boost", "cosmic_stock", 12000),
    P("s_rare_guarantee", "stellar_exchange", "Rare Guarantee", "boost", "rare_guarantee", 3000),
    P("s_special_amplifier", "stellar_exchange", "Special Amplifier", "boost", "special_amplifier", 5000),
    P("s_fortune_elixir", "stellar_exchange", "Fortune Elixir", "boost", "fortune_elixir", 20000, limit=3, period="daily"),
    P("s_overdrive_chip", "stellar_exchange", "Overdrive Chip", "boost", "overdrive_chip", 4000),
    P("s_biome_magnet", "stellar_exchange", "Biome Magnet", "boost", "biome_magnet", 15000, min_level=8),
    P("s_epic_guarantee", "stellar_exchange", "Epic Guarantee", "boost", "epic_guarantee", 150000, limit=1, period="daily", min_level=12),
    P("s_stargazer_glove", "stellar_exchange", "Stargazer Glove", "equipment", "stargazer_glove", 1500, limit=1, period="lifetime", min_level=3),
    P("s_ion_core", "stellar_exchange", "Ion Core", "equipment", "ion_core", 1000, limit=1, period="lifetime", min_level=3),
    # Filter lab — unlocks
    P("u_auto_skip_100", "filter_lab", "Auto Skip: 1/100", "unlock", "auto_skip_100", 2000,
      "1/100未満のアイテムの結果演出をスキップできるフィルター。", limit=1, period="lifetime"),
    P("u_auto_skip_1000", "filter_lab", "Auto Skip: 1/1,000", "unlock", "auto_skip_1000", 15000,
      "1/1,000未満の結果演出をスキップ。", limit=1, period="lifetime", requires="auto_skip_100"),
    P("u_auto_skip_10000", "filter_lab", "Auto Skip: 1/10,000", "unlock", "auto_skip_10000", 120000,
      "1/10,000未満の結果演出をスキップ。", limit=1, period="lifetime", requires="auto_skip_1000"),
    P("u_auto_skip_custom", "filter_lab", "Custom Filter License", "unlock", "auto_skip_custom", 500000,
      "スキップ閾値を自由に設定できるライセンス。", limit=1, period="lifetime", requires="auto_skip_10000"),
    P("u_fast_mode", "filter_lab", "Fast Mode Module", "unlock", "fast_mode", 3000, "Roll演出をFastに切り替え可能にする。", limit=1, period="lifetime"),
    P("u_ultra_fast", "filter_lab", "Ultra Fast Module", "unlock", "ultra_fast", 50000, "Roll演出をUltra Fastに切り替え可能にする。",
      limit=1, period="lifetime", requires="fast_mode"),
    P("u_offline_1", "filter_lab", "Offline Processor I", "unlock", "offline_processor_1", 80000, "オフラインRollの最大時間 +4時間。",
      limit=1, period="lifetime", min_level=5),
    P("u_offline_2", "filter_lab", "Offline Processor II", "unlock", "offline_processor_2", 600000, "オフラインRollの最大時間 さらに+8時間。",
      limit=1, period="lifetime", requires="offline_processor_1", min_level=12),
    P("u_inventory_exp", "filter_lab", "Inventory Expansion", "unlock", "inventory_expansion", 25000, "インベントリ容量 +1000(最大10回)。",
      limit=10, period="lifetime"),
    P("u_rng_analyzer", "filter_lab", "RNG Analyzer", "unlock", "rng_analyzer", 200000, "現在の状態での全アイテムの最終確率を表示する解析装置。",
      limit=1, period="lifetime", min_level=15),
    # Biome shops
    P("s_nebula_incense", "nebula_bazaar", "Nebula Incense", "boost", "nebula_incense", 3000),
    P("s_nebula_tonic", "nebula_bazaar", "Stellar Tonic (特価)", "boost", "stellar_tonic", 1500),
    P("s_solar_lens", "solar_forge", "Solar Lens", "boost", "solar_lens", 10000),
    P("s_solar_chip", "solar_forge", "Overdrive Chip (特価)", "boost", "overdrive_chip", 2500),
    P("s_chrono_capsule", "solar_forge", "Chrono Capsule", "boost", "chrono_capsule", 12000, limit=5, period="daily"),
    P("s_void_resonator", "void_market", "Void Resonator", "boost", "void_resonator", 50000),
    P("s_celestial_surge", "void_market", "Celestial Surge", "boost", "celestial_surge", 400000, limit=1, period="daily"),
    # Cosmic Atelier — cosmetics for Stardust (one per account)
    P("c_atelier_patron", "cosmic_atelier", "称号: Atelier Patron", "cosmetic", "t_atelier_patron", 20000,
      "工房の常連の証。", limit=1, period="lifetime"),
    P("c_quiet_orbit", "cosmic_atelier", "称号: Quiet Orbit", "cosmetic", "t_quiet_orbit", 35000, "静かな軌道。", limit=1, period="lifetime"),
    P("c_starlit", "cosmic_atelier", "称号: Starlit", "cosmetic", "t_starlit", 90000, "星明かりを纏う。", limit=1, period="lifetime", min_level=8),
    P("c_lantern_bearer", "cosmic_atelier", "称号: Lantern Bearer", "cosmetic", "t_lantern_bearer", 150000,
      "暗い宙で灯りを持つ者。", limit=1, period="lifetime", min_level=12),
    P("c_atelier_seal", "cosmic_atelier", "バッジ: Atelier Seal", "cosmetic", "b_atelier_seal", 25000, "", limit=1, period="lifetime"),
    P("c_paper_moon", "cosmic_atelier", "バッジ: Paper Moon", "cosmetic", "b_paper_moon", 40000, "", limit=1, period="lifetime"),
    P("c_comet_tail", "cosmic_atelier", "バッジ: Comet Tail", "cosmetic", "b_comet_tail", 120000, "", limit=1, period="lifetime", min_level=8),
    P("c_orbit_ring", "cosmic_atelier", "バッジ: Orbit Ring", "cosmetic", "b_orbit_ring", 180000, "", limit=1, period="lifetime", min_level=10),
    P("c_bg_quiet_dust", "cosmic_atelier", "背景: Quiet Dust", "cosmetic", "bg_quiet_dust", 60000, "", limit=1, period="lifetime"),
    P("c_bg_amber_drift", "cosmic_atelier", "背景: Amber Drift", "cosmetic", "bg_amber_drift", 200000, "", limit=1, period="lifetime", min_level=10),
    P("c_bg_glass_sea", "cosmic_atelier", "背景: Glass Sea", "cosmetic", "bg_glass_sea", 260000, "", limit=1, period="lifetime", min_level=12),
]
for i, p in enumerate(SHOP_ITEMS):
    p["sort_order"] = i


def C(key: str, kind: str, name: str, rarity: str, desc: str = "", visual: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"key": key, "kind": kind, "name": name, "rarity_key": rarity, "description": desc, "visual": visual or {}}


COSMETICS: list[dict[str, Any]] = [
    # Titles
    C("t_newcomer", "title", "Newcomer", "common", "宇宙へようこそ。"),
    C("t_stargazer", "title", "Stargazer", "common"),
    C("t_voyager", "title", "Voyager", "rare"),
    C("t_pathfinder", "title", "Pathfinder", "epic"),
    C("t_collector", "title", "Collector", "epic"),
    C("t_curator", "title", "Cosmic Curator", "legendary"),
    C("t_legend_seeker", "title", "Legend Seeker", "legendary"),
    C("t_secret_keeper", "title", "Secret Keeper", "secret"),
    C("t_beyond_reason", "title", "Beyond Reason", "ultra_secret"),
    C("t_unknown", "title", "???", "mythic"),
    C("t_pioneer", "title", "Pioneer", "legendary"),
    C("t_world_first", "title", "World First", "secret"),
    C("t_merchant", "title", "Star Merchant", "epic"),
    C("t_tycoon", "title", "Galactic Tycoon", "legendary"),
    C("t_artisan", "title", "Artisan", "epic"),
    C("t_luck_incarnate", "title", "Luck Incarnate", "secret"),
    C("t_void_walker", "title", "Void Walker", "legendary"),
    C("t_wanderer", "title", "Cosmic Wanderer", "epic"),
    C("t_devoted", "title", "The Devoted", "legendary"),
    C("t_eternal", "title", "Eternal Roller", "ultra_secret"),
    C("t_lucky_seven", "title", "Lucky Seven", "legendary"),
    C("t_blessed", "title", "Blessed by the Architect", "admin"),
    C("t_questor", "title", "Questor", "rare"),
    C("t_benefactor", "title", "Benefactor", "rare"),
    C("t_night_owl", "title", "Night Owl", "epic"),
    # Badges
    C("b_first_roll", "badge", "First Roll", "common", "初めてのRoll", {"shape": "dust", "colors": ["#d9e4ff", "#7f93d9"]}),
    C("b_rare", "badge", "Rare Hunter", "rare", "", {"shape": "diamond", "colors": ["#56c8ff", "#2a63ff"]}),
    C("b_epic", "badge", "Epic Hunter", "epic", "", {"shape": "diamond", "colors": ["#c07bff", "#ff4fd8"]}),
    C("b_legendary", "badge", "Legendary Hunter", "legendary", "", {"shape": "star", "colors": ["#ffd05a", "#ff7a1a"]}),
    C("b_secret", "badge", "Secret Hunter", "secret", "", {"shape": "eye", "colors": ["#4dffb8", "#00a6ff"]}),
    C("b_ultra", "badge", "Ultra Hunter", "ultra_secret", "", {"shape": "galaxy", "colors": ["#ff5c8a", "#7a2cff"]}),
    C("b_mythic", "badge", "???", "mythic", "", {"shape": "blackhole", "colors": ["#ffffff", "#8a7bff"]}),
    C("b_pioneer", "badge", "Pioneer", "legendary", "世界初発見者", {"shape": "compass", "colors": ["#ffe27a", "#5ce1ff"]}),
    C("b_world_first", "badge", "World First", "secret", "世界初達成者", {"shape": "crown", "colors": ["#fff0a8", "#7affb0"]}),
    C("b_roller_10k", "badge", "10K Rolls", "epic", "", {"shape": "ring", "colors": ["#8ab4ff", "#b58cff"]}),
    C("b_roller_1m", "badge", "1M Rolls", "ultra_secret", "", {"shape": "ring", "colors": ["#ffffff", "#ff5c8a"]}),
    C("b_biome_master", "badge", "Biome Master", "legendary", "", {"shape": "planet", "colors": ["#6dffcf", "#ff8ad8"]}),
    C("b_void", "badge", "Void Touched", "legendary", "", {"shape": "blackhole", "colors": ["#b07bff", "#ff2e8a"]}),
    C("b_trader", "badge", "Trader", "rare", "", {"shape": "key", "colors": ["#ffd98a", "#c77b2a"]}),
    C("b_artisan", "badge", "Artisan", "epic", "", {"shape": "compass", "colors": ["#ffe2a8", "#b8894a"]}),
    C("b_luck", "badge", "Luck 1M", "ultra_secret", "", {"shape": "lotus", "colors": ["#fff8a0", "#7affc0"]}),
    C("b_admin_blessing", "badge", "Architect's Mark", "admin", "", {"shape": "sigil", "colors": ["#ffe9a8", "#ff3cac"]}),
    # Profile backgrounds
    C("bg_default", "background", "Deep Space", "common", "", {"theme": "stellar_drift"}),
    C("bg_nebula", "background", "Nebula Bloom", "rare", "", {"theme": "nebula_bloom"}),
    C("bg_solar", "background", "Solar Flare", "rare", "", {"theme": "solar_flare"}),
    C("bg_aurora", "background", "Aurora Veil", "epic", "", {"theme": "aurora_veil"}),
    C("bg_frost", "background", "Frozen Comet", "epic", "", {"theme": "frozen_comet"}),
    C("bg_eclipse", "background", "Crimson Eclipse", "epic", "", {"theme": "crimson_eclipse"}),
    C("bg_starfall", "background", "Starfall", "legendary", "", {"theme": "starfall"}),
    C("bg_void", "background", "Void Rift", "legendary", "", {"theme": "void_rift"}),
    C("bg_singularity", "background", "Singularity", "secret", "", {"theme": "singularity"}),
    C("bg_genesis", "background", "Genesis", "ultra_secret", "", {"theme": "genesis"}),
    C("bg_golden", "background", "Golden Archive", "legendary", "", {"theme": "architects_domain"}),
    # Season pass track
    C("t_season_runner", "title", "Season Runner", "rare", "シーズンパスの歩みを進めた者。"),
    C("t_season_sovereign", "title", "Season Sovereign", "secret", "シーズンを走り切った者。"),
    C("b_season_ace", "badge", "Season Ace", "epic", "シーズンパス達成の証", {"shape": "star", "colors": ["#ffd98a", "#7a5cff"]}),
    C("bg_season_meteor", "background", "Meteor Season", "legendary", "", {"theme": "starfall"}),
    # Prestige (転生)
    C("t_reborn_1", "title", "Reborn", "epic", "一度、宇宙をやり直した者。"),
    C("t_reborn_3", "title", "Thrice Reborn", "legendary", "三度、宇宙をやり直した者。"),
    C("t_reborn_5", "title", "Eternal Return", "secret", "五度、宇宙をやり直した者。"),
    C("t_reborn_10", "title", "Cycle Breaker", "ultra_secret", "十度目の輪をみずから断った者。"),
    # Collection sets
    C("t_set_master", "title", "Set Master", "secret", "図鑑のセットを編み切った者。"),
    # Cosmic Atelier (stardust cosmetics)
    C("t_atelier_patron", "title", "Atelier Patron", "rare", "工房の常連。"),
    C("t_starlit", "title", "Starlit", "epic"),
    C("t_quiet_orbit", "title", "Quiet Orbit", "rare"),
    C("t_lantern_bearer", "title", "Lantern Bearer", "epic"),
    C("b_atelier_seal", "badge", "Atelier Seal", "rare", "", {"shape": "sigil", "colors": ["#ffd9a8", "#b8744a"]}),
    C("b_comet_tail", "badge", "Comet Tail", "epic", "", {"shape": "diamond", "colors": ["#8ad8ff", "#ff8ad8"]}),
    C("b_paper_moon", "badge", "Paper Moon", "rare", "", {"shape": "moon", "colors": ["#fff0c0", "#b8a070"]}),
    C("b_orbit_ring", "badge", "Orbit Ring", "epic", "", {"shape": "ring", "colors": ["#a8ffd8", "#5a8aff"]}),
    C("bg_quiet_dust", "background", "Quiet Dust", "rare", "", {"theme": "stellar_drift"}),
    C("bg_amber_drift", "background", "Amber Drift", "epic", "", {"theme": "solar_flare"}),
    C("bg_glass_sea", "background", "Glass Sea", "epic", "", {"theme": "frozen_comet"}),
]
