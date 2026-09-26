"""Initial quests (daily templates, chains, hidden) and achievements."""
from __future__ import annotations

from typing import Any


def Q(key: str, name: str, kind: str, objective: dict[str, Any], rewards: dict[str, Any], desc: str = "", *,
      chain: str | None = None, step: int | None = None, hint: str | None = None, weight: float = 1.0, min_level: int = 1) -> dict[str, Any]:
    return {"key": key, "name": name, "kind": kind, "objective": objective, "rewards": rewards, "description": desc,
            "chain_key": chain, "chain_step": step, "hint": hint, "weight": weight, "min_level": min_level}


def O(type_: str, target: int, per_level: float = 0.0, **params: Any) -> dict[str, Any]:
    return {"type": type_, "target": target, "per_level": per_level, "params": params}


def RW(stardust: int = 0, xp: int = 0, boosts: list[tuple[str, int]] | None = None, cosmetics: list[str] | None = None,
       items: list[tuple[str, int]] | None = None, per_level: float = 0.0) -> dict[str, Any]:
    return {"stardust": stardust, "xp": xp, "per_level": per_level,
            "boosts": [{"key": k, "qty": q} for k, q in (boosts or [])],
            "cosmetics": cosmetics or [], "items": [{"key": k, "qty": q} for k, q in (items or [])]}


QUESTS: list[dict[str, Any]] = [
    # --- daily templates (target scaled by level) ---------------------------
    Q("d_roll_s", "星を数える", "daily", O("roll", 150, 8), RW(1500, 40, per_level=0.05), "Rollを{target}回行う", weight=3),
    Q("d_roll_l", "終わらない夜", "daily", O("roll", 600, 25), RW(5000, 120, [("stellar_tonic", 1)], per_level=0.05), "Rollを{target}回行う", weight=1.5),
    Q("d_rare", "蒼い輝き", "daily", O("obtain_tier", 6, 0.1, tier="rare"), RW(2500, 60, per_level=0.05), "Rare以上のアイテムを{target}個獲得", weight=2),
    Q("d_epic", "紫電一閃", "daily", O("obtain_tier", 1, 0, tier="epic"), RW(6000, 150, [("triple_charm", 1)], per_level=0.05), "Epic以上のアイテムを{target}個獲得", weight=1.2, min_level=3),
    Q("d_biome", "異界の風", "daily", O("roll", 60, 2, biome="any_special"), RW(3000, 80, per_level=0.05), "特殊Biome中にRollを{target}回行う", weight=1.5),
    Q("d_sell", "星の商人", "daily", O("sell", 40, 1), RW(2000, 40, per_level=0.05), "アイテムを{target}個売却", weight=1.5),
    Q("d_earn", "星屑の蓄え", "daily", O("earn", 5000, 400), RW(2500, 60, per_level=0.05), "Stardustを{target}獲得", weight=1.5),
    Q("d_special", "共鳴", "daily", O("special_roll", 8, 0.2), RW(2000, 50, [("special_amplifier", 1)], per_level=0.05), "Special Rollを{target}回発生させる", weight=1.5),
    Q("d_boost", "加速する運命", "daily", O("boost_use", 2), RW(2500, 50, per_level=0.05), "Boostを{target}回使用", weight=1),
    Q("d_discover", "未知との遭遇", "daily", O("discover", 1), RW(4000, 100, per_level=0.05), "新しいアイテムを{target}種類発見", weight=1),
    Q("d_craft", "星の鍛冶師", "daily", O("craft", 1), RW(3000, 80, per_level=0.05), "Craftを{target}回行う", weight=1, min_level=4),
    Q("d_market", "取引の星", "daily", O("market_sell", 1), RW(4000, 80, per_level=0.05), "Marketでアイテムを{target}個販売", weight=0.7, min_level=8),
    # --- chain: The Stargazer's Path ----------------------------------------
    Q("c_star_1", "はじまりの光", "chain", O("roll", 25), RW(300, 20), "Rollを25回行う", chain="stargazer", step=1),
    Q("c_star_2", "蒼い星", "chain", O("obtain_tier", 1, tier="rare"), RW(800, 40, [("next_roll_boost", 1)]), "Rare以上のアイテムを獲得する", chain="stargazer", step=2),
    Q("c_star_3", "異界の扉", "chain", O("biome_enter", 1, biome="any_special"), RW(1500, 60), "特殊Biomeに遭遇する", chain="stargazer", step=3),
    Q("c_star_4", "装備の心得", "chain", O("equip", 1), RW(2000, 80, [("stellar_tonic", 2)]), "Equipmentを装備する", chain="stargazer", step=4),
    Q("c_star_5", "紫の予兆", "chain", O("obtain_tier", 1, tier="epic"), RW(5000, 150, [("fortune_elixir", 1)]), "Epic以上のアイテムを獲得する", chain="stargazer", step=5),
    Q("c_star_6", "鍛冶の道", "chain", O("craft", 3), RW(8000, 200), "Craftを3回行う", chain="stargazer", step=6),
    Q("c_star_7", "星の階梯", "chain", O("level", 10), RW(15000, 300, [("epic_guarantee", 1)]), "レベル10に到達する", chain="stargazer", step=7),
    Q("c_star_8", "伝説への道", "chain", O("obtain_tier", 1, tier="legendary"), RW(50000, 800, [("celestial_surge", 1)], ["t_pathfinder"]),
      "Legendary以上のアイテムを獲得する", chain="stargazer", step=8),
    # --- chain: Void Pilgrimage ------------------------------------------------
    Q("c_void_1", "裂け目の噂", "chain", O("biome_enter", 1, biome="void_rift"), RW(20000, 300), "Void Riftに遭遇する", chain="void", step=1, min_level=15),
    Q("c_void_2", "深淵の祈り", "chain", O("roll", 200, biome="void_rift"), RW(40000, 500, [("void_resonator", 1)]), "Void Rift中にRollを200回行う", chain="void", step=2, min_level=15),
    Q("c_void_3", "虚無の欠片", "chain", O("obtain_item", 1, item="rift_walker_mask"), RW(100000, 1000), "Rift Walker's Maskを獲得する", chain="void", step=3, min_level=15),
    Q("c_void_4", "百の幸運", "chain", O("luck", 100), RW(150000, 1500), "最終Luck 100以上でRollする", chain="void", step=4, min_level=15),
    Q("c_void_5", "秘密の守り人", "chain", O("obtain_tier", 1, tier="secret"), RW(500000, 5000, [("celestial_surge", 2)], ["t_void_walker", "b_void"]),
      "Secret以上のアイテムを獲得する", chain="void", step=5, min_level=15),
    # --- hidden quests (objective shown only after completion; hint visible) ---
    Q("h_eleven", "11:11", "hidden", O("roll_at_time", 1, tz="Asia/Tokyo", hh=11, mm=11), RW(11111, 111, [("triple_charm", 1)]),
      "11時11分にRollする", hint="時計の数字が揃う瞬間に、祈りを。"),
    Q("h_triple_echo", "三重の残響", "hidden", O("streak_same", 3), RW(7777, 77), "同じアイテムを3回連続で獲得する", hint="同じ星が三度輝く時。"),
    Q("h_deep_diver", "深淵の潜行者", "hidden", O("roll", 100, biome="void_rift"), RW(30000, 300), "Void Rift中にRollを100回行う", hint="虚無の深淵で祈り続けよ。", min_level=15),
    Q("h_generous", "手放す勇気", "hidden", O("sell_value", 1, value=100000), RW(20000, 200), "価値100,000以上のアイテムを売却する", hint="価値あるものを手放す勇気。"),
    Q("h_luck_1000", "千の運命", "hidden", O("luck", 1000), RW(100000, 1000, [("celestial_surge", 1)]), "最終Luck 1000以上でRollする", hint="運命の数値が千を超える時。"),
    Q("h_lucky_seven", "七の奥義", "hidden", O("hidden_special", 1, key="lucky_seven"), RW(77777, 777), "Lucky Seven Rollを発生させる", hint="七の倍数の、さらに奥…"),
    Q("h_night_owl", "夜更かし", "hidden", O("roll_between", 300, tz="Asia/Tokyo", start=2, end=4), RW(15000, 150, cosmetics=["t_night_owl"]),
      "深夜2時〜4時にRollを300回行う", hint="誰もが眠る時間、星は最も美しい。"),
    Q("h_dust", "原点回帰", "hidden", O("obtain_item", 50, item="cosmic_dust"), RW(5000, 50), "Cosmic Dustを50個獲得する", hint="すべての星は塵から始まった。"),
]
for i, q in enumerate(QUESTS):
    q["sort_order"] = i


def A(key: str, name: str, category: str, tier: str, condition: dict[str, Any], rewards: dict[str, Any] | None = None, desc: str = "",
      *, hidden: bool = False, hint: str | None = None) -> dict[str, Any]:
    return {"key": key, "name": name, "category": category, "tier": tier, "condition": condition, "rewards": rewards or {},
            "description": desc, "hidden": hidden, "hint": hint}


def AR(stardust: int = 0, title: str | None = None, badge: str | None = None, background: str | None = None,
       item: str | None = None, boosts: list[tuple[str, int]] | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {"stardust": stardust, "cosmetics": [c for c in (title, badge, background) if c]}
    if item:
        r["items"] = [{"key": item, "qty": 1}]
    if boosts:
        r["boosts"] = [{"key": k, "qty": q} for k, q in boosts]
    return r


ACHIEVEMENTS: list[dict[str, Any]] = []

# Rolls
for n, tier, rw in [
    (1, "bronze", AR(100, "t_newcomer", "b_first_roll")), (10, "bronze", AR(200)), (100, "bronze", AR(500, "t_stargazer")),
    (1000, "silver", AR(3000, "t_voyager")), (5000, "silver", AR(10000)), (10000, "gold", AR(25000, badge="b_roller_10k")),
    (50000, "gold", AR(100000, "t_wanderer")), (100000, "cosmic", AR(250000, "t_devoted")), (500000, "cosmic", AR(1000000)),
    (1000000, "cosmic", AR(5000000, "t_eternal", "b_roller_1m")),
]:
    ACHIEVEMENTS.append(A(f"rolls_{n}", f"{n:,} Rolls", "rolls", tier, {"stat": "total_rolls", "gte": n}, rw, f"Rollを{n:,}回行う"))

# Rarity
for rk, name, tier, rw in [
    ("rare", "蒼き邂逅", "bronze", AR(300, badge="b_rare")), ("epic", "紫の閃光", "silver", AR(2000, badge="b_epic")),
    ("legendary", "伝説の目撃者", "gold", AR(20000, "t_legend_seeker", "b_legendary", "bg_starfall")),
    ("secret", "秘密に触れし者", "cosmic", AR(150000, "t_secret_keeper", "b_secret")),
    ("ultra_secret", "理を超えて", "cosmic", AR(1000000, "t_beyond_reason", "b_ultra", "bg_singularity")),
    ("mythic", "???", "cosmic", AR(10000000, "t_unknown", "b_mythic", "bg_genesis")),
]:
    ACHIEVEMENTS.append(A(f"first_{rk}", name, "rarity", tier, {"rarity": rk, "gte": 1}, rw, f"{rk.replace('_', ' ').title() if rk != 'mythic' else '???'}以上のアイテムを獲得する",
                          hidden=(rk == "mythic"), hint=("存在するかどうかも分からない" if rk == "mythic" else None)))
for rk, n, tier, stardust in [("legendary", 10, "gold", 100000), ("legendary", 50, "cosmic", 500000), ("secret", 5, "cosmic", 1000000), ("epic", 100, "gold", 50000)]:
    ACHIEVEMENTS.append(A(f"{rk}_{n}", f"{rk.title()} ×{n}", "rarity", tier, {"rarity": rk, "gte": n}, AR(stardust), f"{rk.title()}以上を{n}個獲得する"))

# Collection
for n, tier, rw in [(10, "bronze", AR(500)), (25, "silver", AR(3000)), (50, "gold", AR(20000, "t_collector")),
                    (75, "gold", AR(80000, background="bg_golden")), (100, "cosmic", AR(300000, "t_curator"))]:
    ACHIEVEMENTS.append(A(f"collect_{n}", f"Collector {n}", "collection", tier, {"stat": "discovered_count", "gte": n}, rw, f"{n}種類のアイテムを発見する"))

# Biomes
for bk, bname, tier, rw in [
    ("nebula_bloom", "Nebula Bloom", "bronze", AR(500, background="bg_nebula")), ("solar_flare", "Solar Flare", "bronze", AR(500, background="bg_solar")),
    ("meteor_storm", "Meteor Storm", "bronze", AR(500)), ("aurora_veil", "Aurora Veil", "silver", AR(1000, background="bg_aurora")),
    ("frozen_comet", "Frozen Comet", "silver", AR(1000, background="bg_frost")), ("crimson_eclipse", "Crimson Eclipse", "silver", AR(3000, background="bg_eclipse")),
    ("quantum_foam", "Quantum Foam", "gold", AR(5000)), ("starfall", "Starfall", "gold", AR(10000)),
    ("void_rift", "Void Rift", "gold", AR(50000, background="bg_void")), ("singularity", "Singularity", "cosmic", AR(300000)),
    ("genesis", "Genesis", "cosmic", AR(3000000)),
]:
    ACHIEVEMENTS.append(A(f"biome_{bk}", bname, "biome", tier, {"biome": bk}, rw, f"{bname}に遭遇する",
                          hidden=(bk == "genesis"), hint=("世界がもう一度始まる瞬間がある" if bk == "genesis" else None)))
ACHIEVEMENTS.append(A("biomes_5", "Biome Explorer", "biome", "silver", {"biomes_count": 5}, AR(5000), "5種類の特殊Biomeに遭遇する"))
ACHIEVEMENTS.append(A("biomes_all", "Biome Master", "biome", "cosmic", {"biomes_count": 11}, AR(1000000, badge="b_biome_master"), "全ての自然Biomeに遭遇する"))

# Economy
for n, tier, rw in [(10000, "bronze", AR(1000)), (100000, "silver", AR(5000)), (1000000, "gold", AR(30000, "t_merchant")),
                    (10000000, "cosmic", AR(200000, "t_tycoon")), (100000000, "cosmic", AR(1000000))]:
    ACHIEVEMENTS.append(A(f"earn_{n}", f"{n:,} Stardust", "economy", tier, {"stat": "stardust_earned", "gte": n}, rw, f"累計{n:,} Stardustを獲得する"))
for n, tier in [(100, "bronze"), (1000, "silver"), (10000, "gold")]:
    ACHIEVEMENTS.append(A(f"sell_{n}", f"Seller {n:,}", "economy", tier, {"stat": "items_sold", "gte": n}, AR(n * 5), f"アイテムを{n:,}個売却する"))

# Social
for key, stat, n, name, tier, rw in [
    ("market_sell_1", "market_sales", 1, "初めての出品成立", "bronze", AR(1000)), ("market_sell_25", "market_sales", 25, "マーケットの顔", "silver", AR(20000)),
    ("market_sell_200", "market_sales", 200, "星間商会", "gold", AR(150000)), ("market_buy_1", "market_buys", 1, "初めての購入", "bronze", AR(500)),
    ("market_buy_25", "market_buys", 25, "目利き", "silver", AR(10000)), ("trade_1", "trades", 1, "初めてのTrade", "bronze", AR(1000, badge="b_trader")),
    ("trade_25", "trades", 25, "交渉人", "silver", AR(20000)), ("trade_100", "trades", 100, "交易王", "gold", AR(100000)),
    ("gift_1", "gifts_sent", 1, "贈り物", "bronze", AR(500)), ("gift_25", "gifts_sent", 25, "星の贈り主", "silver", AR(10000, "t_benefactor")),
]:
    ACHIEVEMENTS.append(A(key, name, "social", tier, {"stat": stat, "gte": n}, rw, f"{name}（{n}回）"))

# Crafting
for n, tier, rw in [(1, "bronze", AR(500)), (10, "silver", AR(5000, badge="b_artisan")), (50, "gold", AR(30000, "t_artisan")), (200, "cosmic", AR(200000))]:
    ACHIEVEMENTS.append(A(f"craft_{n}", f"Crafter {n}", "crafting", tier, {"stat": "crafts", "gte": n}, rw, f"Craftを{n}回行う"))

# Luck
for n, tier, rw in [(10, "bronze", AR(1000)), (100, "silver", AR(10000)), (1000, "gold", AR(100000)), (10000, "gold", AR(300000)),
                    (100000, "cosmic", AR(1000000, "t_luck_incarnate")), (1000000, "cosmic", AR(5000000, badge="b_luck"))]:
    ACHIEVEMENTS.append(A(f"luck_{n}", f"Luck {n:,}", "luck", tier, {"stat": "max_luck", "gte": n}, rw, f"最終Luck {n:,}以上でRollする"))

# Discovery
for n, tier, rw in [(1, "gold", AR(50000, "t_pioneer", "b_pioneer")), (5, "cosmic", AR(250000)), (25, "cosmic", AR(2000000, "t_world_first", "b_world_first"))]:
    ACHIEVEMENTS.append(A(f"firsts_{n}", f"World Discoverer {n}", "discovery", tier, {"stat": "first_discoveries", "gte": n}, rw, f"世界初発見を{n}回達成する"))

# Special rolls / offline / level / quests / boosts
for key, stat, n, name, tier, rw in [
    ("special_10", "special_rolls", 10, "共鳴の兆し", "bronze", AR(500)), ("special_100", "special_rolls", 100, "共鳴者", "silver", AR(5000)),
    ("special_1000", "special_rolls", 1000, "大いなる共鳴", "gold", AR(50000)),
    ("offline_1000", "offline_rolls", 1000, "眠れる観測者", "bronze", AR(2000)), ("offline_100000", "offline_rolls", 100000, "夢の中の宇宙", "gold", AR(100000)),
    ("quest_1", "quests_completed", 1, "はじめての依頼", "bronze", AR(300)), ("quest_25", "quests_completed", 25, "依頼人の友", "silver", AR(10000, "t_questor")),
    ("quest_200", "quests_completed", 200, "伝説の冒険者", "gold", AR(150000)),
    ("boost_1", "boosts_used", 1, "ブースト点火", "bronze", AR(200)), ("boost_50", "boosts_used", 50, "加速中毒", "silver", AR(10000)),
    ("boost_500", "boosts_used", 500, "光速の運命", "gold", AR(100000)),
]:
    ACHIEVEMENTS.append(A(key, name, "progress", tier, {"stat": stat, "gte": n}, rw, f"{name}（{n:,}）"))
for n, tier, rw in [(5, "bronze", AR(1000)), (10, "silver", AR(5000)), (25, "gold", AR(50000)), (50, "gold", AR(200000)), (100, "cosmic", AR(1000000))]:
    ACHIEVEMENTS.append(A(f"level_{n}", f"Level {n}", "progress", tier, {"level": n}, rw, f"レベル{n}に到達する"))

# Hidden / special
ACHIEVEMENTS += [
    A("lucky_seven", "Lucky Seven", "hidden", "gold", {"item": "lucky_seven_relic"}, AR(77777, "t_lucky_seven"), "Lucky Seven Relicを獲得する",
      hidden=True, hint="七が重なる瞬間に現れるもの"),
    A("omega", "The End", "hidden", "cosmic", {"item": "omega"}, AR(100000000), "OMEGAを獲得する", hidden=True, hint="終わりの先"),
    A("resonance", "Resonance", "hidden", "silver", {"item": "resonance_shard"}, AR(10000), "Resonance Shardを獲得する", hidden=True,
      hint="特別なRollの時だけ、何かが共鳴している"),
    A("midnight", "Midnight Watcher", "hidden", "silver", {"item": "midnight_comet"}, AR(10000), "Midnight Cometを獲得する", hidden=True,
      hint="日付が変わった後の空を見上げて"),
    A("architect_blessing", "Architect's Blessing", "hidden", "cosmic", {"event": "admin_grant"}, AR(0, badge="b_admin_blessing"),
      "管理者からAdmin Artifactを授かる", hidden=True, hint="世界を創る者に認められること"),
    A("everbloom", "Eternal Garden", "hidden", "gold", {"item": "everbloom"}, AR(30000), "秘密のレシピを発見する", hidden=True,
      hint="花と花を重ねると…"),
    A("dust_lover", "Dust to Dust", "hidden", "bronze", {"item_count": "cosmic_dust", "gte": 100}, AR(5000), "Cosmic Dustを100個獲得する", hidden=True,
      hint="ありふれたものを愛すること"),
]
for i, a in enumerate(ACHIEVEMENTS):
    a["sort_order"] = i
