"""Public share pages for notable rolls.

A share URL is minted only inside a roll result the owner received, and it
carries an HMAC so IDs cannot be enumerated. The page itself is public HTML
with OGP tags; the OG image is drawn server-side (Latin text only — the
bundled font has no CJK glyphs, so the card shows the English item name).
"""
from __future__ import annotations

import html
import io
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..core.errors import NotFound
from ..db import get_db
from ..models import Roll, User
from ..services import icons as icons_svc
from ..services import inventory as inv_svc
from ..services.share import check_sig, share_sig

router = APIRouter(tags=["share"])

async def _load(db: AsyncSession, roll_id: int, s: str) -> tuple[Roll, dict[str, Any], User | None]:
    if not check_sig(roll_id, s):
        raise NotFound("ページが見つかりません")
    roll = await db.get(Roll, roll_id)
    if roll is None:
        raise NotFound("ページが見つかりません")
    info = await inv_svc.item_info(db, roll.item_id)
    user = await db.get(User, roll.user_id)
    if user is not None and user.status == "banned":
        user = None
    return roll, info, user


def _odds_text(odds: float) -> str:
    return f"1 in {int(round(odds)):,}" if odds >= 2 else "1 in 1"


@router.get("/share/r/{roll_id}")
async def share_page(roll_id: int, s: str = Query(min_length=8, max_length=64),
                     db: AsyncSession = Depends(get_db)) -> Response:
    roll, info, user = await _load(db, roll_id, s)
    base = get_settings().public_base_url.rstrip("/")
    name = str(info.get("name_ja") or info.get("name"))
    name_en = str(info.get("name") or name)
    player = user.display_name if user is not None else "???"
    odds = _odds_text(float(roll.base_odds))
    title = f"{name} ({odds}) - COSMIC RNG"
    desc = f"{player} が {name} を引き当てました！確率 {odds}。あなたも運試ししませんか？"
    og_img = f"{base}/share/r/{roll_id}/og.png?s={share_sig(roll_id)}"
    e = html.escape
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:image" content="{e(og_img)}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:image" content="{e(og_img)}">
<style>
  body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 30% 20%,#131a3a,#05060f 70%);color:#e8ecff;font-family:system-ui,sans-serif;text-align:center}}
  .card{{padding:32px 24px;max-width:520px}}
  img{{width:180px;height:180px;filter:drop-shadow(0 0 24px rgba(120,140,255,.5))}}
  h1{{font-size:1.5rem;margin:.6em 0 .2em}}
  .odds{{color:#9fb2ff;font-size:1.1rem;margin-bottom:.4em}}
  .by{{color:#8890b8;font-size:.95rem}}
  a.cta{{display:inline-block;margin-top:24px;padding:12px 32px;border-radius:999px;background:linear-gradient(90deg,#5b6cff,#a75bff);color:#fff;text-decoration:none;font-weight:700}}
</style></head><body>
<div class="card">
  <img src="{e(og_img)}" alt="" style="width:300px;height:auto">
  <h1>{e(name)}</h1>
  <div class="odds">{e(odds)} · {e(name_en)}</div>
  <div class="by">引き当てたプレイヤー: {e(player)}</div>
  <a class="cta" href="{e(base)}/">COSMIC RNG で運試しする</a>
</div>
</body></html>"""
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=600"})


@router.get("/share/r/{roll_id}/og.png")
async def share_og(roll_id: int, s: str = Query(min_length=8, max_length=64),
                   db: AsyncSession = Depends(get_db)) -> Response:
    from PIL import Image, ImageDraw, ImageFont

    roll, info, user = await _load(db, roll_id, s)
    snap_colors = ("#ffffff", "#888888")
    icon_png = icons_svc.render_icon(info.get("visual") or {}, snap_colors[0], snap_colors[1])
    icon = Image.open(io.BytesIO(icon_png)).convert("RGBA")

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (7, 9, 22))
    d = ImageDraw.Draw(img)
    # subtle vignette bands
    for i in range(24):
        a = 22 - i
        d.ellipse((W * 0.55 - i * 26, -220 - i * 12, W * 1.25 + i * 26, H * 0.85 + i * 12),
                  outline=(20 + a, 24 + a, 58 + a))
    # stars
    import random as _r
    rng = _r.Random(roll_id)
    for _ in range(140):
        x, y = rng.randrange(W), rng.randrange(H)
        b = rng.randrange(60, 200)
        d.point((x, y), fill=(b, b, min(255, b + 30)))
    icon = icon.resize((360, 360))
    img.paste(icon, (80, 135), icon)

    def font(sz: int) -> Any:
        try:
            return ImageFont.load_default(sz)
        except Exception:
            return ImageFont.load_default()

    name_en = str(info.get("name") or "Unknown")
    odds = _odds_text(float(roll.base_odds))
    d.text((500, 150), "COSMIC RNG", font=font(40), fill=(140, 156, 255))
    d.text((500, 235), name_en[:26], font=font(64), fill=(240, 244, 255))
    d.text((500, 340), odds, font=font(56), fill=(255, 214, 120))
    tier = int(info.get("tier") or 1)
    d.text((500, 430), f"Rarity Tier {tier}", font=font(36), fill=(170, 180, 220))
    d.text((500, 520), "cosmic-rng — try your luck", font=font(30), fill=(120, 128, 170))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
