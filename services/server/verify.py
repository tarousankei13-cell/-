"""
入室時の認証

ボタンを押す（または画像の文字を入力する）とロールが付く。

⚠️ **認証は「人かどうか」を見るものではない。**
   ボタンを押すだけの認証は、自動化すれば突破できる。
   それでも意味があるのは、
     ・荒らし用の使い捨てアカウントには手間がかかる
     ・入室しただけの人と、意思を持って入った人を分けられる
   ため。強くしたいときは、アカウント作成からの日数条件を併せて使う。

⚠️ 画像認証の文字は**見間違える字を外してある**（config.VERIFY_CAPTCHA_CHARS）。
   0とO、1とI を混ぜると、正しく読めた人を弾いてしまう。
"""

from __future__ import annotations

import io
import logging
import random
import secrets
import time
from datetime import datetime, timezone

import discord
from sqlalchemy import func, select

import config
from core import settings
from db.models import Verification
from db.session import session_scope

log = logging.getLogger("bot.server.verify")


class VerifyError(Exception):
    """利用者にそのまま見せてよい、断る理由。"""


# ------------------------------------------------------------
#  設定
# ------------------------------------------------------------

def enabled() -> bool:
    return bool(settings.get("verify_enabled", False))


def mode() -> str:
    m = str(settings.get("verify_mode", config.VERIFY_MODE) or "button")
    return m if m in ("button", "captcha") else "button"


def role_id() -> int | None:
    rid = settings.get("verify_role")
    return int(rid) if rid else None


def pending_role_id() -> int | None:
    rid = settings.get("verify_pending_role")
    return int(rid) if rid else None


def min_account_days() -> int:
    return int(settings.get("verify_min_account_days", 0) or 0)


# ------------------------------------------------------------
#  画像認証
# ------------------------------------------------------------

# 出した問題。利用者ID → (答え, 出した時刻)
#   ⚠️ DBに置かない。再起動したら作り直せばよいもので、
#      残しておくと「答えの一覧」という持ちたくないものになる。
_puzzles: dict[int, tuple[str, float]] = {}

# 問題の有効時間（秒）。長すぎると使い回されるため短くする。
PUZZLE_TTL = 300


def new_code() -> str:
    """問題の文字列を作る。"""
    n = int(settings.get("verify_captcha_length", config.VERIFY_CAPTCHA_LENGTH) or 5)
    n = max(4, min(8, n))
    chars = config.VERIFY_CAPTCHA_CHARS
    # ⚠️ random ではなく secrets を使う。推測できると意味がなくなる。
    return "".join(secrets.choice(chars) for _ in range(n))


def issue(user_id: int) -> str:
    """問題を出して、答えを覚える。"""
    code = new_code()
    _puzzles[user_id] = (code, time.monotonic())
    _sweep()
    return code


def _sweep() -> None:
    """期限切れの問題を捨てる。放っておくと増え続ける。"""
    now = time.monotonic()
    for uid in [u for u, (_, t) in _puzzles.items() if now - t > PUZZLE_TTL]:
        _puzzles.pop(uid, None)


def answer_matches(user_id: int, given: str) -> bool | None:
    """
    答え合わせ。

    True  … 合っていた（問題は使い捨てにする）
    False … 間違い
    None  … 問題が無い／期限切れ（出し直してもらう）
    """
    got = _puzzles.get(user_id)
    if got is None:
        return None
    code, when = got
    if time.monotonic() - when > PUZZLE_TTL:
        _puzzles.pop(user_id, None)
        return None
    # 大文字小文字と全角半角の違いは許す。読めていれば通す。
    import unicodedata

    cleaned = unicodedata.normalize("NFKC", given or "").strip().upper()
    cleaned = cleaned.replace(" ", "")
    if cleaned == code:
        _puzzles.pop(user_id, None)
        return True
    return False


def render(code: str) -> discord.File:
    """
    問題を画像にする。

    ⚠️ 読めない画像を作ってはいけない。
       傾き・ゆらぎは控えめにして、線は文字の上に薄く引く。
       「機械に読ませない」より「人が読める」を優先する。
    """
    from PIL import Image, ImageDraw, ImageFont

    from services.receipt import FONT_PATH

    w, h = 90 + 52 * len(code), 120
    img = Image.new("RGB", (w, h), (248, 248, 250))
    d = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(str(FONT_PATH), 62)
    except OSError:               # フォントが無い環境でも止めない
        font = ImageFont.load_default()
        log.warning("フォントが読めないため、簡易な画像で認証します")

    # 背景の薄い点（下地）
    for _ in range(420):
        x, y = random.randint(0, w), random.randint(0, h)
        d.point((x, y), fill=(random.randint(200, 230),) * 3)

    # 文字
    x = 42
    for ch in code:
        y = 24 + random.randint(-7, 7)
        angle = random.randint(-14, 14)
        piece = Image.new("RGBA", (72, 86), (0, 0, 0, 0))
        ImageDraw.Draw(piece).text(
            (6, 2), ch, font=font,
            fill=(random.randint(20, 70), random.randint(20, 70),
                  random.randint(60, 120), 255),
        )
        piece = piece.rotate(angle, resample=Image.BICUBIC, expand=False)
        img.paste(piece, (x, y), piece)
        x += 52

    # 横切る線（薄く・文字を潰さない太さ）
    for _ in range(2):
        y1, y2 = random.randint(20, h - 20), random.randint(20, h - 20)
        d.line((0, y1, w, y2), fill=(150, 150, 170), width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="verify.png")


# ------------------------------------------------------------
#  判定
# ------------------------------------------------------------

def account_too_new(member: discord.Member) -> float | None:
    """
    条件に足りない場合、あと何日待てばよいかを返す。足りていれば None。
    """
    need = min_account_days()
    if need <= 0:
        return None
    created = getattr(member, "created_at", None)
    if created is None:
        return None
    age = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    if age >= need:
        return None
    return need - age


async def already(guild_id: int, user_id: int) -> bool:
    async with session_scope() as s:
        return (await s.get(Verification, (guild_id, user_id))) is not None


async def note_attempt(guild_id: int, user_id: int) -> int:
    """
    失敗を1回数えて、合計を返す。

    ⚠️ まだ認証していない人の行も作る。
       作らないと失敗回数を覚えられない（verified_at は後で入れ直す）。
    """
    async with session_scope() as s:
        row = await s.get(Verification, (guild_id, user_id))
        if row is None:
            row = Verification(
                guild_id=guild_id, discord_id=user_id,
                method="pending", attempts=0,
            )
            s.add(row)
        row.attempts = int(row.attempts or 0) + 1
        return row.attempts


async def attempts_left(guild_id: int, user_id: int) -> int:
    """あと何回間違えられるか。"""
    cap = int(settings.get("verify_max_attempts", 5) or 5)
    if cap <= 0:
        return 999
    async with session_scope() as s:
        row = await s.get(Verification, (guild_id, user_id))
    used = int(row.attempts or 0) if row else 0
    return max(0, cap - used)


async def grant(
    member: discord.Member, *, method: str = "button",
) -> None:
    """
    ロールを付けて、認証済みとして記録する。

    ⚠️ ロールを付けてから記録する。
       逆にすると、ロールを付けられなかったのに
       「認証済み」になって二度と押せなくなる。
    """
    rid = role_id()
    if rid is None:
        raise VerifyError(
            "認証で付けるロールが設定されていません。"
            "管理者に `/verify setup` をお願いしてください。"
        )
    role = member.guild.get_role(rid)
    if role is None:
        raise VerifyError(
            "認証で付けるロールが見つかりません（消された可能性があります）。"
            "管理者にお知らせください。"
        )

    me = member.guild.me
    if me is not None and role >= me.top_role:
        raise VerifyError(
            "BOTより上の位置にあるロールは付けられません。"
            "サーバー設定でBOTのロールを、付けたいロールより**上**に"
            "移動してください。"
        )

    try:
        await member.add_roles(role, reason="認証")
    except discord.Forbidden as e:
        raise VerifyError(
            "ロールを付けられませんでした。"
            "BOTに「ロールの管理」権限が必要です。"
        ) from e

    pend = pending_role_id()
    if pend:
        prole = member.guild.get_role(pend)
        if prole is not None and prole in member.roles:
            try:
                await member.remove_roles(prole, reason="認証")
            except discord.HTTPException:
                # 付ける方は済んでいるので、外せなくても失敗にしない
                log.warning("未認証ロールを外せませんでした: %s", member)

    async with session_scope() as s:
        row = await s.get(Verification, (member.guild.id, member.id))
        if row is None:
            s.add(Verification(
                guild_id=member.guild.id, discord_id=member.id,
                method=method,
            ))
        else:
            row.method = method
            row.verified_at = datetime.now(timezone.utc)

    log.info("認証しました: %s（%s）", member, method)


async def stats(guild_id: int) -> dict:
    """認証の状況。"""
    async with session_scope() as s:
        done = (await s.execute(
            select(func.count()).select_from(Verification).where(
                Verification.guild_id == guild_id,
                Verification.method != "pending",
            )
        )).scalar() or 0
        failing = (await s.execute(
            select(func.count()).select_from(Verification).where(
                Verification.guild_id == guild_id,
                Verification.method == "pending",
                Verification.attempts > 0,
            )
        )).scalar() or 0
    return {"verified": int(done), "failing": int(failing)}
