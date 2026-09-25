"""The admin action catalogue must match what the server actually implements.

The panel builds its forms from USER_ACTIONS / BULK_OPERATIONS. If the catalogue
listed an action the handler does not implement, the panel would render a button
that fails when pressed; if the handler implemented one the catalogue omits,
there would be no way to reach it. Both are checked here rather than by reading.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import admin_ops
from tests.conftest import admin_action, login, set_user


SOURCE = Path(admin_ops.__file__).read_text(encoding="utf-8")


def _implemented(func: str) -> set[str]:
    body = SOURCE.split(f"async def {func}(", 1)[1]
    body = body.split("\nasync def ", 1)[0]
    return set(re.findall(r'(?:if|elif) (?:action|op) == "(\w+)"', body))


def test_every_catalogued_user_action_is_implemented():
    catalogued = {a["action"] for a in admin_ops.USER_ACTIONS}
    implemented = _implemented("user_action")
    assert not catalogued - implemented, f"catalogued but not implemented: {sorted(catalogued - implemented)}"
    assert not implemented - catalogued, f"implemented but unreachable from the panel: {sorted(implemented - catalogued)}"


def test_every_catalogued_bulk_operation_is_implemented():
    catalogued = {a["action"] for a in admin_ops.BULK_OPERATIONS}
    implemented = _implemented("bulk_operation")
    assert not catalogued - implemented, f"catalogued but not implemented: {sorted(catalogued - implemented)}"
    assert not implemented - catalogued, f"implemented but unreachable: {sorted(implemented - catalogued)}"


def test_catalogue_field_names_match_what_the_handler_reads():
    """A form field the handler never reads is a control that silently does nothing.

    This is how the panel drifts: someone adds an action, names the parameter one
    thing in the form and another in the handler, and the button looks like it
    worked. The names are compared directly against the source.
    """
    body = SOURCE.split("async def user_action(", 1)[1].split("\nasync def ", 1)[0]
    problems = []
    for entry in admin_ops.USER_ACTIONS:
        seg = body.split(f'action == "{entry["action"]}"', 1)
        if len(seg) < 2:
            problems.append((entry["action"], "not implemented"))
            continue
        seg = seg[1].split("\n    elif action ==")[0]
        read = set(re.findall(r'p\.get\("(\w+)"', seg)) | set(re.findall(r'p\["(\w+)"\]', seg))
        read.discard("confirm")
        sent = {f["name"] for f in entry["fields"]}
        if sent - read:
            problems.append((entry["action"], f"form sends {sorted(sent - read)}, handler never reads it"))
    assert not problems, problems


def test_catalogue_entries_are_well_formed():
    for a in admin_ops.USER_ACTIONS + admin_ops.BULK_OPERATIONS:
        assert a["label"] and a["group"], a
        for f in a["fields"]:
            assert f["name"] and f["label"] and f["type"], (a["action"], f)
            if f["type"] == "enum":
                assert f.get("choices"), (a["action"], f["name"])


@pytest.mark.asyncio
async def test_dangerous_actions_need_confirmation(admin, app):
    p = await login(app)
    for action in sorted(admin_ops.DANGEROUS):
        if action in ("set_role", "ban"):
            continue  # exercised in the security suite against protected targets
        r = await admin.post(f"/api/admin/users/{p.id}/action",
                             json={"action": action, "params": {}, "reason": "test"})
        assert r.status_code == 400, f"{action}: {r.text}"
        assert r.json()["error"]["code"] == "confirmation_required", action
    await p.c.aclose()


@pytest.mark.asyncio
async def test_new_progression_actions_take_effect(admin, app):
    p = await login(app)
    await admin_action(admin, p.id, "set_level", level=30)
    me = (await p.get("/api/me")).json()
    assert me["user"]["level"] == 30
    assert me["user"]["xp"] > 0, "XP must follow the level or the next roll undoes it"

    await admin_action(admin, p.id, "add_xp", amount=-10**9)
    me = (await p.get("/api/me")).json()
    assert me["user"]["xp"] == 0 and me["user"]["level"] == 1

    await admin_action(admin, p.id, "rename", display_name="改名テスト")
    assert (await p.get("/api/me")).json()["user"]["name"] == "改名テスト"

    await admin_action(admin, p.id, "set_roll_counter", value=777)
    assert (await p.get("/api/me")).json()["user"]["roll_counter"] == 777
    await p.c.aclose()


@pytest.mark.asyncio
async def test_reset_user_clears_progress_but_keeps_the_account(admin, app):
    p = await login(app)
    await set_user(p.id, stardust=50_000)
    await admin_action(admin, p.id, "give_item", item_key="cosmic_lotus", qty=3)
    await admin_action(admin, p.id, "set_level", level=20)
    assert (await p.get("/api/inventory")).json()["groups"]

    await admin_action(admin, p.id, "reset_user", confirm=True)
    me = (await p.get("/api/me")).json()
    assert me["user"]["level"] == 1 and me["user"]["stardust"] == 0
    assert (await p.get("/api/inventory")).json()["groups"] == []
    assert (await p.get("/api/me")).status_code == 200, "the account itself must survive"
    await p.c.aclose()


@pytest.mark.asyncio
async def test_bulk_give_reaches_every_player(admin, app):
    a = await login(app)
    b = await login(app)
    before_a = (await a.get("/api/me")).json()["user"]["stardust"]
    before_b = (await b.get("/api/me")).json()["user"]["stardust"]

    r = await admin.post("/api/admin/bulk", json={"op": "give_stardust", "params": {"amount": 1234}, "reason": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["targets"] >= 2

    assert (await a.get("/api/me")).json()["user"]["stardust"] == before_a + 1234
    assert (await b.get("/api/me")).json()["user"]["stardust"] == before_b + 1234
    for c in (a, b):
        await c.c.aclose()


@pytest.mark.asyncio
async def test_bulk_take_away_never_goes_negative(admin, app):
    p = await login(app)
    await set_user(p.id, stardust=10)
    r = await admin.post("/api/admin/bulk", json={"op": "give_stardust", "params": {"amount": -99999}, "reason": "test"})
    assert r.status_code == 200, r.text
    assert (await p.get("/api/me")).json()["user"]["stardust"] == 0
    await p.c.aclose()


@pytest.mark.asyncio
async def test_bulk_destructive_needs_confirmation(admin):
    r = await admin.post("/api/admin/bulk", json={"op": "wipe_market", "params": {}, "reason": "test"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirmation_required"


@pytest.mark.asyncio
async def test_bulk_operations_are_audited(admin):
    r = await admin.post("/api/admin/bulk",
                         json={"op": "recompute_stats", "params": {}, "reason": "監査テスト"})
    assert r.status_code == 200, r.text
    logs = (await admin.get("/api/admin/audit?action=bulk_recompute_stats")).json()["logs"]
    assert logs and logs[0]["reason"] == "監査テスト"


@pytest.mark.asyncio
async def test_bulk_is_admin_only(app):
    p = await login(app)
    r = await p.post("/api/admin/bulk", json={"op": "recompute_stats", "params": {}, "reason": "x"})
    assert r.status_code == 404, "the endpoint must not even exist for a normal player"
    await p.c.aclose()


def test_every_seeded_content_row_has_a_japanese_name_or_already_is_one():
    """A shop shelf or a season with a bare English label is the case players
    hit most often, because those pages are visited every session."""
    import re

    from app.content import seed_names_ja as ja
    from app.content import seed_items, seed_progress, seed_world

    ASCII = re.compile(r"^[\x20-\x7e]+$")
    sources = {
        "items": [*seed_items.GENERAL_ITEMS, *seed_items.BIOME_ITEMS, *seed_items.SPECIAL_ITEMS],
        "rarities": seed_items.RARITIES, "biomes": seed_world.BIOMES,
        "equipment": seed_world.EQUIPMENT, "boosts": seed_world.BOOSTS, "recipes": seed_world.RECIPES,
        "shops": seed_world.SHOPS, "shop_items": seed_world.SHOP_ITEMS, "cosmetics": seed_world.COSMETICS,
        "quests": seed_progress.QUESTS, "achievements": seed_progress.ACHIEVEMENTS,
        "item_parts": seed_items.ITEM_PARTS,
    }
    missing = []
    for table, rows in sources.items():
        names = ja.BY_TABLE.get(table, {})
        for row in rows:
            name = row.get("name") or ""
            # A name that is already Japanese needs no second one.
            if not ASCII.match(name):
                continue
            if not names.get(row["key"]):
                missing.append(f"{table}/{row['key']} ({name})")
    assert not missing, "untranslated: " + ", ".join(missing[:20])
