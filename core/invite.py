"""
招待キャンペーン

招待した人に残高を渡す。**実際にお金が出ていく**ので、
配りすぎないための歯止めをいくつも重ねてある。

  1. 既定で無効（/campaign enable で管理者が明示的に開ける）
  2. 1人あたりの上限（invite_max_per_user）
  3. キャンペーン全体の上限（invite_budget）
  4. 1人は一度しか招待されない（invites の主キー）
  5. 自分で自分を招待できない
  6. 条件を満たすまで渡さない（既定は「招待された人の初回注文」）
  7. 渡すのは元帳（core/ledger）を通す。残高の出どころが必ず残る

⚠️ 「招待された」判定は、あとから取り消せない。
   先に条件を満たしてから渡すこと。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

import config
from core import ledger as L
from core import settings
from db.models import Invite, InviteLink, InvitePayout, User, utcnow
from db.session import session_scope, user_scope

log = logging.getLogger("bot.invite")

# 招待コードに使う文字。
# 読み上げたり書き写したりするので、見間違えるものは入れない。
#   0 と O、1 と I と L は、フォントによってほぼ同じに見える。
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


class InviteError(Exception):
    """利用者にそのまま見せてよい、招待の失敗理由。"""


def code_for(discord_id: int, *, salt: int = 0) -> str:
    """
    その人の招待コード。discord_id から導き、いつでも同じ値になる。

    ⚠️ discord_id を復元できてはいけないので、ハッシュから作る。

    salt は**他の人とぶつかったときだけ**使う。0 のときは今までと
    同じ値になるので、すでに配ったコードは変わらない。
    """
    seed = f"invite:{discord_id}" if not salt else f"invite:{discord_id}#{salt}"
    digest = hashlib.sha256(seed.encode()).digest()
    n = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(CODE_LENGTH):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(out)


# 書き写すときに間違えやすい文字を、正しい文字へ寄せる。
# （コード側は 0 1 I O L を使っていないので、入力されたら読み違い）
_CONFUSABLE = str.maketrans({"0": "O", "O": "Q", "1": "7", "I": "J", "L": "J"})


def normalize(code: str) -> str:
    """
    入力ゆれを吸収する。

    小文字・空白・ハイフンを直したうえで、見間違えやすい文字を寄せる。
    ⚠️ 寄せ先は ALPHABET に入っている文字でなければ意味がない。
    """
    t = (code or "").strip().upper().replace("-", "").replace(" ", "")
    t = "".join(ch for ch in t if ch.isalnum())
    return t.translate(_CONFUSABLE)


def enabled() -> bool:
    return bool(settings.get("invite_enabled", config.INVITE_ENABLED))


def reward_amount() -> int:
    return max(0, int(settings.get("invite_reward", config.INVITE_REWARD)))


def invitee_amount() -> int:
    return max(0, int(settings.get("invite_reward_invitee", config.INVITE_REWARD_INVITEE)))


def condition() -> str:
    return str(settings.get("invite_condition", config.INVITE_CONDITION))


def reward_every() -> int:
    """何名の達成ごとに特典を出すか。1 なら1人ごと。"""
    return max(1, int(settings.get("invite_reward_every", config.INVITE_REWARD_EVERY)))


def min_order() -> int:
    """達成とみなす最低の注文額（定価）。"""
    return max(0, int(settings.get("invite_min_order", config.INVITE_MIN_ORDER)))


def min_account_days() -> int:
    return max(0, int(settings.get("invite_min_account_days",
                                   config.INVITE_MIN_ACCOUNT_DAYS)))


def min_member_hours() -> int:
    return max(0, int(settings.get("invite_min_member_hours",
                                   config.INVITE_MIN_MEMBER_HOURS)))


def link_channel_id() -> int:
    """招待リンクを向ける先。0 なら未設定（発行できない）。"""
    return int(settings.get("invite_link_channel", config.INVITE_LINK_CHANNEL) or 0)


def link_days() -> int:
    return max(1, int(settings.get("invite_link_days", config.INVITE_LINK_DAYS)))


def max_per_user() -> int:
    """1人が特典の対象にできる人数。0 で無制限。"""
    return max(0, int(settings.get("invite_max_per_user", config.INVITE_MAX_PER_USER)))


# ============================================================
#  集計
# ============================================================

@dataclass
class Stats:
    total: int = 0            # 紐づいた招待の数
    rewarded: int = 0         # 特典を渡した回数（発火の回数）
    spent: int = 0            # 渡した合計額
    pending: int = 0          # 条件待ち
    claimed: int = 0          # 受取ボタンを押した数
    qualified: int = 0        # 条件を満たす注文まで終えた数

    @property
    def budget_left(self) -> int:
        budget = int(settings.get("invite_budget", config.INVITE_BUDGET))
        return max(0, budget - self.spent) if budget > 0 else -1   # -1 は無制限


async def stats() -> Stats:
    """
    ⚠️ 配った額は **invite_payouts から** 数える。
       「2名ごとに¥500」は招待1件に紐づかないので、
       招待の行を足しても合わない（予算の判定がずれる）。
    """
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(Invite))).scalar() or 0
        claimed = (
            await s.execute(
                select(func.count()).select_from(Invite).where(Invite.claimed.is_(True))
            )
        ).scalar() or 0
        qualified = (
            await s.execute(
                select(func.count()).select_from(Invite)
                .where(Invite.qualified.is_(True))
            )
        ).scalar() or 0
        fired = (
            await s.execute(select(func.count()).select_from(InvitePayout))
        ).scalar() or 0
        spent = int(
            (
                await s.execute(
                    select(func.coalesce(func.sum(InvitePayout.amount), 0))
                )
            ).scalar() or 0
        )
        # 招待された側へのお礼も、配った額に含める
        spent += int(
            (
                await s.execute(
                    select(func.coalesce(func.sum(Invite.reward_amount), 0))
                )
            ).scalar() or 0
        )
    return Stats(
        total=int(total), rewarded=int(fired), spent=int(spent),
        pending=int(total) - int(qualified),
        claimed=int(claimed), qualified=int(qualified),
    )


async def count_for(inviter_id: int, *, rewarded_only: bool = False) -> int:
    """その人が紐づけた人数。rewarded_only なら達成まで終えた人数。"""
    async with session_scope() as s:
        q = select(func.count()).select_from(Invite).where(
            Invite.inviter_id == inviter_id
        )
        if rewarded_only:
            q = q.where(Invite.claimed.is_(True), Invite.qualified.is_(True))
        return int((await s.execute(q)).scalar() or 0)


async def earned_by(inviter_id: int) -> int:
    """その人が受け取った特典の合計額。"""
    async with session_scope() as s:
        return int(
            (
                await s.execute(
                    select(func.coalesce(func.sum(InvitePayout.amount), 0))
                    .where(InvitePayout.inviter_id == inviter_id)
                )
            ).scalar() or 0
        )


async def ranking(limit: int = 10) -> list[tuple[int, int]]:
    """(招待した人のID, 成立した数) を多い順に。"""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Invite.inviter_id, func.count().label("n"))
                .where(Invite.claimed.is_(True), Invite.qualified.is_(True))
                .group_by(Invite.inviter_id)
                .order_by(func.count().desc())
                .limit(limit)
            )
        ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


async def monthly_ranking(
    limit: int = 5, *, month_start: datetime | None = None,
) -> list[tuple[int, int]]:
    """
    今月に成立した紹介の数で並べる。(招待した人のID, 件数)。

    ⚠️ 基準は `qualified_at`（条件を満たした時刻）。
       招待した時刻ではない。先月に招待して今月に成立した分は
       **今月**として数える。特典が出たのが今月だから。

    ⚠️ 月の区切りは日本時間。UTCで切ると、毎月1日の朝9時までが
       前月に入ってしまう。
    """
    start = month_start or config.jst_month_start_utc()
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Invite.inviter_id, func.count().label("n"))
                .where(
                    Invite.claimed.is_(True),
                    Invite.qualified.is_(True),
                    Invite.qualified_at.is_not(None),
                    Invite.qualified_at >= start,
                )
                .group_by(Invite.inviter_id)
                .order_by(func.count().desc(), Invite.inviter_id)
                .limit(limit)
            )
        ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


async def monthly_total() -> int:
    """今月に成立した紹介の合計。"""
    start = config.jst_month_start_utc()
    async with session_scope() as s:
        got = (await s.execute(
            select(func.count()).select_from(Invite).where(
                Invite.claimed.is_(True),
                Invite.qualified.is_(True),
                Invite.qualified_at.is_not(None),
                Invite.qualified_at >= start,
            )
        )).scalar()
    return int(got or 0)


# ============================================================
#  紐づけ
# ============================================================

async def ensure_code(discord_id: int) -> str:
    """
    その人の招待コードを用意して返す。

    code_for() は discord_id から決まるので毎回同じだが、
    **コードから人を引く**ために列にも入れておく。
    全員ぶんハッシュを計算して突き合わせるのは、人が増えると重い。

    ⚠️ **他の人と同じコードになりうる**（8.9億通りだが、
       5,000人で約1.4%、2万人で約20%）。ぶつかったまま保存しようとすると
       一意制約で失敗し、その人は招待コードを受け取れなくなる。
       ぶつかったら塩を足して別の値にする。

    ⚠️ 返すのは**実際に保存した値**。導いた値をそのまま返すと、
       画面に出すコードとDBの中身が食い違い、貼っても誰も引けなくなる。

    ⚠️ すでに自分のコードを持っている人は、そのまま使う。
       配ったあとに変えると、出回っているリンクが死ぬ。
    """
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        if row is None:
            return code_for(discord_id)
        if row.invite_code:
            return row.invite_code

        for salt in range(8):
            code = code_for(discord_id, salt=salt)
            taken = (await s.execute(
                select(User.discord_id).where(User.invite_code == code)
            )).scalar()
            if taken is None or taken == discord_id:
                row.invite_code = code
                return code

        # ここまで全部ぶつかることは現実には起こらない
        log.error("招待コードを決められませんでした（discord_id=%s）", discord_id)
        return code_for(discord_id)


async def find_inviter(code: str) -> int | None:
    """
    招待コードから、招待した人を探す。

    ⚠️ コードは2種類ある。どちらを貼られても引けるようにする。
       ・Discord の招待コード（vsqvB5wp）… BOT が発行したリンク
       ・6文字のコード（A7K2MX）        … 画面に出す短いコード
    """
    raw = (code or "").strip()
    # discord.gg/xxxx の形で貼られることがあるので、末尾だけ取る
    for prefix in ("https://discord.gg/", "http://discord.gg/", "discord.gg/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.strip().strip("/").split("?")[0]
    if not raw:
        return None

    async with session_scope() as s:
        # ① BOT が発行した招待リンク（大文字小文字をそのまま見る）
        owner = (
            await s.execute(
                select(InviteLink.discord_id).where(InviteLink.code == raw)
            )
        ).scalars().first()
        if owner is not None:
            return int(owner)

        # ② 6文字のコード
        wanted = normalize(raw)
        if len(wanted) != CODE_LENGTH:
            return None
        uid = (
            await s.execute(
                select(User.discord_id).where(User.invite_code == wanted)
            )
        ).scalars().first()
    return int(uid) if uid is not None else None


def check_eligibility(
    *, account_created: datetime | None, joined_at: datetime | None, manual: bool
) -> None:
    """
    招待された側の条件。満たさなければ InviteError。

    ⚠️ 作ったばかりの捨てアカウントを並べて人数を稼ぐのを防ぐための歯止め。
       「参加からの時間」は **手動でコードを入れたときだけ** 見る。
       招待リンクから入った人は、参加した瞬間に紐づくので待たせない。
    """
    now = datetime.now(timezone.utc)

    days = min_account_days()
    if days > 0 and account_created is not None:
        age = (now - _as_utc(account_created)).total_seconds() / 86400
        if age < days:
            raise InviteError(
                f"Discordアカウントを作成されてから **{days}日以上** が必要です。"
                f"（いまは{int(age)}日）"
            )

    hours = min_member_hours()
    if manual and hours > 0 and joined_at is not None:
        been = (now - _as_utc(joined_at)).total_seconds() / 3600
        if been < hours:
            raise InviteError(
                f"サーバーに参加されてから **{hours}時間以上** が必要です。"
                f"（あと{max(1, int(hours * 60 - been * 60))}分ほどお待ちください）"
            )


def _as_utc(dt: datetime) -> datetime:
    """素の日時を UTC とみなす（DBから出た値がこれ）。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def link(
    discord_id: int, inviter_id: int, *, source: str = "code",
    guild_id: int | None = None,
) -> Invite:
    """
    招待された人を、招待した人に紐づける。

    ⚠️ ここではまだ数に入らない。本人が受取ボタンを押し（claim）、
       条件を満たす注文を終えて（qualify）、はじめて1名と数える。
    """
    if not enabled():
        raise InviteError("いまは紹介プログラムを行っていません。")
    if discord_id == inviter_id:
        raise InviteError("ご自身の招待コードはご利用いただけません。")

    async with session_scope() as s:
        if await s.get(Invite, discord_id) is not None:
            raise InviteError("すでに招待コードを登録済みです。")

        inviter = await s.get(User, inviter_id)
        if inviter is None:
            raise InviteError("その招待コードは見つかりませんでした。")
        if inviter.is_banned:
            raise InviteError("その招待コードはご利用いただけません。")

        # ⚠️ 上限は「特典の対象になる人数」。紐づけの時点では、まだ
        #    達成していない人も含めて数える（先に押さえる形にしないと、
        #    紐づけだけ無限に積み上がって上限の意味が無くなる）。
        limit = max_per_user()
        if limit > 0:
            n = int(
                (
                    await s.execute(
                        select(func.count()).select_from(Invite)
                        .where(Invite.inviter_id == inviter_id)
                    )
                ).scalar() or 0
            )
            if n >= limit:
                raise InviteError("その方の紹介はすでに上限に達しています。")

        row = Invite(
            discord_id=discord_id, inviter_id=inviter_id, source=source,
            guild_id=guild_id, rewarded=False, reward_amount=0,
        )
        s.add(row)

        me = await s.get(User, discord_id)
        if me is not None:
            me.invited_by = inviter_id
        return row


async def claim(discord_id: int) -> int | None:
    """
    招待された本人が受取ボタンを押した。紹介した人のIDを返す。

    ⚠️ ここを通らないと人数に入らない。なりすましで勝手に紐づけられ、
       知らないうちに誰かの実績になることを防ぐ。
    """
    async with session_scope() as s:
        row = await s.get(Invite, discord_id)
        if row is None:
            raise InviteError("お受け取りいただく紹介が見つかりませんでした。")
        if row.claimed:
            raise InviteError("すでにお受け取りいただいています。")
        row.claimed = True
        row.claimed_at = utcnow()
        return int(row.inviter_id)


async def is_claimed(discord_id: int) -> bool:
    async with session_scope() as s:
        row = await s.get(Invite, discord_id)
        return bool(row and row.claimed)


async def qualify(discord_id: int, list_price: int) -> int | None:
    """
    注文が条件を満たした。紹介した人のIDを返す（対象外なら None）。

    ⚠️ 判定に使うのは **定価**。割引後の額で見ると、
       クーポンで下げて条件を回避されてしまう。
    """
    if not enabled():
        return None
    low = min_order()
    async with session_scope() as s:
        row = await s.get(Invite, discord_id)
        if row is None or row.qualified:
            return None
        if not row.claimed:
            return None                       # 受取ボタンがまだ
        if low > 0 and int(list_price or 0) < low:
            log.info("注文額が条件に届かないため、紹介の達成にしませんでした")
            return None
        row.qualified = True
        row.qualified_at = utcnow()
        return int(row.inviter_id)


async def reached_count(inviter_id: int) -> int:
    """その人が達成した人数（受取済み かつ 条件を満たす注文あり）。"""
    async with session_scope() as s:
        n = int(
            (
                await s.execute(
                    select(func.count()).select_from(Invite).where(
                        Invite.inviter_id == inviter_id,
                        Invite.claimed.is_(True),
                        Invite.qualified.is_(True),
                    )
                )
            ).scalar() or 0
        )
    limit = max_per_user()
    return min(n, limit) if limit > 0 else n


async def settle(inviter_id: int) -> list[tuple[int, int]]:
    """
    溜まった達成人数を特典に換えて渡す。渡した (相手のID, 金額) を返す。

    「2名ごとに¥500」は**何度も発火する**ので、
    すでに渡した回数との差ぶんだけを渡す。

    ⚠️ 何度呼んでも二重には渡さない。渡した回数は invite_payouts に
       主キーで記録してあり、同じ区切りは2行入らない。
    """
    if not enabled():
        return []

    every = reward_every()
    pay = reward_amount()
    if pay <= 0:
        return []

    reached = await reached_count(inviter_id)
    due = reached // every
    if due <= 0:
        return []

    async with session_scope() as s:
        done = int(
            (
                await s.execute(
                    select(func.count()).select_from(InvitePayout)
                    .where(InvitePayout.inviter_id == inviter_id)
                )
            ).scalar() or 0
        )
    if due <= done:
        return []

    paid: list[tuple[int, int]] = []
    for milestone in range(done + 1, due + 1):
        # ---- 全体の予算 ----
        st = await stats()
        budget = int(settings.get("invite_budget", config.INVITE_BUDGET))
        if budget > 0 and st.spent + pay > budget:
            log.info("紹介プログラムの予算に達したため、特典を渡しませんでした")
            break

        # ⚠️ 先に「渡した」と記録してから渡す。
        #    主キーの衝突で、同時に2回走っても1回しか通らない。
        #    渡すのに失敗したら、この記録を消して元に戻す。
        try:
            async with session_scope() as s:
                s.add(InvitePayout(
                    inviter_id=inviter_id, milestone=milestone,
                    amount=pay, reached=reached,
                ))
        except Exception:
            log.info("この区切りはすでに渡し済みでした（%d回目）", milestone)
            continue

        try:
            async with user_scope(inviter_id) as s:
                await L.adjust(s, inviter_id, pay, memo="紹介プログラムの特典")
        except L.LedgerError:
            log.exception("紹介の特典を渡せませんでした")
            async with session_scope() as s:
                row = await s.get(InvitePayout, (inviter_id, milestone))
                if row is not None:
                    await s.delete(row)
            break
        paid.append((inviter_id, pay))

    return paid


async def on_order_completed(discord_id: int, list_price: int) -> list[tuple[int, int]]:
    """
    注文が成立したときに呼ぶ。達成を数え、溜まっていれば特典を渡す。

    返すのは渡した (相手のID, 金額) の一覧。何も起きなければ空。
    """
    inviter_id = await qualify(discord_id, list_price)
    if inviter_id is None:
        return []
    paid = await settle(inviter_id)

    # 招待された側へのお礼（設定されていれば、1回だけ）
    invitee_pay = invitee_amount()
    if invitee_pay > 0:
        async with session_scope() as s:
            row = await s.get(Invite, discord_id)
            already = bool(row and row.rewarded)
        if not already:
            try:
                async with user_scope(discord_id) as s:
                    await L.adjust(s, discord_id, invitee_pay,
                                   memo="紹介プログラムの特典")
            except L.LedgerError:
                log.exception("招待された方への特典を渡せませんでした")
            else:
                async with session_scope() as s:
                    row = await s.get(Invite, discord_id)
                    if row is not None:
                        row.rewarded = True
                        row.reward_amount = invitee_pay
                        row.rewarded_at = utcnow()
                paid.append((discord_id, invitee_pay))
    return paid


async def grant_if_ready(discord_id: int, list_price: int = 0) -> list[tuple[int, int]]:
    """
    以前からの入口。条件が「コードを入力した時点」なら受取だけで達成とみなす。

    ⚠️ 注文から呼ぶときは list_price を必ず渡すこと。
       渡さないと最低注文額の条件を判定できない。
    """
    if not enabled():
        return []
    if condition() == "join":
        # 参加（受取）だけで達成とする設定。最低注文額は見ない。
        async with session_scope() as s:
            row = await s.get(Invite, discord_id)
            if row is None or row.qualified or not row.claimed:
                return []
            row.qualified = True
            row.qualified_at = utcnow()
            inviter_id = int(row.inviter_id)
        return await settle(inviter_id)
    return await on_order_completed(discord_id, list_price)


# ============================================================
#  招待リンク（BOT が本人に代わって発行する）
# ============================================================

async def save_link(
    code: str, *, guild_id: int, discord_id: int,
    channel_id: int | None = None, expires_at: datetime | None = None,
) -> None:
    """発行した招待リンクを覚える。参加時に誰の招待かを引くために使う。"""
    async with session_scope() as s:
        row = await s.get(InviteLink, code)
        if row is None:
            s.add(InviteLink(
                code=code, guild_id=guild_id, discord_id=discord_id,
                channel_id=channel_id, expires_at=expires_at,
            ))
        else:
            row.guild_id = guild_id
            row.discord_id = discord_id
            row.channel_id = channel_id
            row.expires_at = expires_at


async def my_link(discord_id: int, guild_id: int) -> InviteLink | None:
    """
    その人がそのサーバーで持っている、まだ生きている招待リンク。

    ⚠️ 1人1本（サーバーごと）。期限切れは無かったことにして、
       発行し直せるようにする。
    """
    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(InviteLink).where(
                    InviteLink.discord_id == discord_id,
                    InviteLink.guild_id == guild_id,
                )
            )
        ).scalars().all()
        for row in rows:
            if row.expires_at is None or _as_utc(row.expires_at) > now:
                s.expunge(row)
                return row
    return None


async def forget_link(code: str) -> None:
    """期限切れ・削除された招待リンクを忘れる。"""
    async with session_scope() as s:
        row = await s.get(InviteLink, code)
        if row is not None:
            await s.delete(row)


async def owner_of_link(code: str) -> int | None:
    """招待コードから、発行した人を引く。"""
    async with session_scope() as s:
        return (
            await s.execute(
                select(InviteLink.discord_id).where(InviteLink.code == code)
            )
        ).scalars().first()


# ============================================================
#  通知設定
# ============================================================

async def notify_enabled(discord_id: int) -> bool:
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        return True if row is None else bool(row.invite_notify)


async def set_notify(discord_id: int, on: bool) -> bool:
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        if row is not None:
            row.invite_notify = bool(on)
    return bool(on)
