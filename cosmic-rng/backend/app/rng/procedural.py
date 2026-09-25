"""Procedural (auto-generated) items: Material + Shape + Effect (+ Modifier).

A procedural slot item (e.g. "Forged Relic", 1/300) is rolled like any other
item; when it hits, a concrete composite item is generated. Each chosen part
multiplies the rarity by ``max_weight_in_category / chosen_weight`` so
common-part combinations keep the slot's odds while rare parts produce
genuinely rarer items. The combination is persisted as an ``items`` row
(kind='generated') on first discovery, so collections, first-discovery and
statistics work exactly as for fixed items.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..content.registry import ItemDef, Snapshot
from .engine import RandomSource


@dataclass(slots=True)
class GeneratedSpec:
    key: str
    name: str
    name_ja: str
    odds: float
    sell_value: int
    visual: dict[str, Any]
    parts: dict[str, str]
    description: str
    combo_prob: float = 1.0  # true probability of this exact combination given the slot hit


def _weighted(options: list[dict[str, Any]], rng: RandomSource, invert: bool = False) -> dict[str, Any]:
    weights = [(1.0 / max(o["weight"], 1e-9)) if invert else max(o["weight"], 0.0) for o in options]
    total = sum(weights)
    u = rng.random() * total
    acc = 0.0
    for o, w in zip(options, weights):
        acc += w
        if u < acc:
            return o
    return options[-1]


def compose_name_ja(chosen: dict[str, dict[str, Any]]) -> str:
    """Japanese name for a part combination, or "" if any part lacks one.

    Japanese reads modifier → effect → material → shape ("暁の輝く黄金の宝珠"),
    the reverse of the English "Radiant Gold Orb of Dawn". A half-translated
    name is worse than none, so one missing part means no Japanese name at all.
    """
    order = ("modifier", "effect", "material", "shape")
    words = []
    for ptype in order:
        part = chosen.get(ptype)
        if part is None:
            continue
        ja = (part.get("name_ja") or "").strip()
        if not ja:
            return ""
        words.append(ja)
    return "".join(words)


def build_spec(slot: ItemDef, chosen: dict[str, dict[str, Any]], snap: Snapshot) -> GeneratedSpec:
    proc = slot.procedural or {}
    factor = 1.0
    value_mult = 1.0
    combo_prob = 1.0
    for ptype, part in chosen.items():
        options = snap.parts.get(ptype, [])
        max_w = max((o["weight"] for o in options), default=1.0)
        total_w = sum(o["weight"] for o in options) or 1.0
        factor *= max_w / max(part["weight"], 1e-9)
        combo_prob *= max(part["weight"], 0.0) / total_w
        value_mult *= float(part.get("value_mult", 1.0))
    odds = float(slot.odds or 1) * factor
    words = [chosen[p]["name"] for p in ("effect", "material", "shape") if p in chosen]
    name = " ".join(words)
    if "modifier" in chosen:
        name = f"{name} {chosen['modifier']['name']}"
    prefix = proc.get("prefix")
    mat = chosen.get("material", {}).get("visual", {})
    shp = chosen.get("shape", {}).get("visual", {})
    eff = chosen.get("effect", {}).get("visual", {})
    colors = list(mat.get("colors") or slot.visual.get("colors") or ["#c0c8e0", "#7080a8"])
    while len(colors) < 3:
        colors.append("#ffffff")
    visual = {"shape": shp.get("shape", slot.visual.get("shape", "rune")), "colors": colors[:3], "glow": colors[0],
              "fx": eff.get("fx", "none"), "procedural": True}
    key = "gen:" + slot.key + ":" + "-".join(chosen[p]["key"] for p in sorted(chosen))
    desc = f"{prefix + ' ' if prefix else ''}{slot.name}から生まれた自動生成アイテム。"
    return GeneratedSpec(
        key=key[:128], name=name[:128], name_ja=compose_name_ja(chosen)[:128], odds=odds,
        sell_value=max(1, round(float(proc.get("base_value", 10)) * value_mult * factor ** 0.35)),
        visual=visual, parts={p: chosen[p]["key"] for p in chosen}, description=desc, combo_prob=combo_prob,
    )


def generate(slot: ItemDef, snap: Snapshot, rng: RandomSource, *, min_odds: float | None = None) -> GeneratedSpec:
    proc = slot.procedural or {}
    ptypes = [p for p in proc.get("parts", ["effect", "material", "shape"]) if snap.parts.get(p)]
    if not ptypes:
        raise RuntimeError(f"no parts configured for procedural slot {slot.key}")
    if min_odds is None:
        chosen = {p: _weighted(snap.parts[p], rng) for p in ptypes}
        return build_spec(slot, chosen, snap)
    best: GeneratedSpec | None = None
    for _ in range(200):
        chosen = {p: _weighted(snap.parts[p], rng, invert=True) for p in ptypes}
        spec = build_spec(slot, chosen, snap)
        if spec.odds >= min_odds:
            return spec
        if best is None or spec.odds > best.odds:
            best = spec
    rarest = {p: min(snap.parts[p], key=lambda o: o["weight"]) for p in ptypes}
    spec = build_spec(slot, rarest, snap)
    return spec if best is None or spec.odds >= best.odds else best
