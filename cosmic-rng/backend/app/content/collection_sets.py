"""Collection sets: finish a themed group of the book, claim a permanent bonus.

The luck reward is intentionally tiny per set (fractions of a percent to a few
percent) — the point is a reason to fill the book, not a power spiral.
"""
from __future__ import annotations

from typing import Any

SETS: list[dict[str, Any]] = [
    {"key": "set_starter", "name": "Starter Cache", "name_ja": "はじまりの欠片", "luck_pct": 0.5, "stardust": 2000,
     "items": ["cosmic_dust", "stardust_mote", "space_pebble", "ion_spark", "frozen_vapor"]},
    {"key": "set_drifter", "name": "Drifter's Pockets", "name_ja": "漂流者のポケット", "luck_pct": 0.8, "stardust": 5000,
     "items": ["lunar_sand", "orbit_fragment", "nebula_wisp", "photon_bead", "quartz_meteorite", "solar_ember", "comet_tail"]},
    {"key": "set_bloom", "name": "Nebula Bloom", "name_ja": "星雲の花園", "luck_pct": 1.0, "stardust": 8000,
     "items": ["bloom_petal", "nebula_rose", "pink_quasar"]},
    {"key": "set_solar", "name": "Heart of Fire", "name_ja": "太陽の心臓", "luck_pct": 1.2, "stardust": 10000,
     "items": ["flare_cinder", "corona_crown", "heart_of_the_sun"]},
    {"key": "set_frost", "name": "Absolute Zero", "name_ja": "絶対零度", "luck_pct": 1.2, "stardust": 10000,
     "items": ["comet_ice", "glacial_heart", "absolute_frost"]},
    {"key": "set_aurora", "name": "Veil of Lights", "name_ja": "光のヴェール", "luck_pct": 1.2, "stardust": 10000,
     "items": ["aurora_silk", "borealis_lantern", "veil_of_lights"]},
    {"key": "set_eclipse", "name": "Crimson Rite", "name_ja": "紅の儀式", "luck_pct": 1.5, "stardust": 15000,
     "items": ["blood_moon_shard", "crimson_halo", "red_giants_tear"]},
    {"key": "set_storm", "name": "Impact Zone", "name_ja": "衝突地帯", "luck_pct": 1.2, "stardust": 10000,
     "items": ["meteor_chunk", "impact_crystal", "shooting_star_core"]},
    {"key": "set_quantum", "name": "Superposition", "name_ja": "重ね合わせ", "luck_pct": 1.8, "stardust": 20000,
     "items": ["qubit_bubble", "entangled_pair", "schrodingers_box"]},
    {"key": "set_starfall", "name": "Wish Upon", "name_ja": "星に願いを", "luck_pct": 1.8, "stardust": 20000,
     "items": ["fallen_star", "wishing_star", "star_sovereign"]},
    {"key": "set_void", "name": "Into the Rift", "name_ja": "裂け目の奥へ", "luck_pct": 2.5, "stardust": 40000, "cosmetic": "t_void_walker",
     "items": ["void_shard", "rift_walker_mask", "null_heart", "abyssal_eye"]},
    {"key": "set_singularity", "name": "Beyond the Horizon", "name_ja": "事象の地平の先", "luck_pct": 3.0, "stardust": 80000,
     "cosmetic": "t_set_master",
     "items": ["compressed_star", "spaghettified_light", "hawking_radiance", "event_horizon_crown"]},
]

SET_MAP = {s["key"]: s for s in SETS}
