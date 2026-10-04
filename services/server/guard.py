"""
荒らし・スパムの検知

⚠️ **自動で人を罰する機能なので、誤検知が一番の害になる。**
   疑わしいときは何もしない方を選ぶこと。
   消された人は「なぜ消されたのか」を知れないため、
   対処したときは必ず記録（GuardHit）を残す。

⚠️ 内容を読む権限（MESSAGE CONTENT INTENT）が無い環境でも、
   **数えられるものは数える**。
     ・何件/何秒で出したか   … 内容が無くても数えられる
     ・メンションの数         … Discord が内容とは別の項目で送ってくる
   読めないのは「同じ文の繰り返し」「NGワード」「招待リンク」だけ。
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass

import discord

from core import settings

log = logging.getLogger("bot.server.guard")

# 招待リンク。discord.gg / discord.com/invite / discordapp.com/invite
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg)/"
    r"(?P<code>[a-zA-Z0-9\-_]+)",
    re.I,
)

# 対処の種類
ACT_NONE = "none"
ACT_DELETE = "delete"
ACT_TIMEOUT = "timeout"

KIND_LABEL = {
    "spam": "連投",
    "repeat": "同じ文の繰り返し",
    "mention": "メンション爆撃",
    "invite": "他サーバーの招待リンク",
    "word": "NGワード",
    "raid": "短時間の大量入室",
    "newaccount": "作りたてのアカウント",
}


@dataclass
class Hit:
    """検知した1件。"""
    kind: str
    detail: str
    action: str = ACT_NONE

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)


def normalize(text: str) -> str:
    """
    比べる前に形をそろえる。

    全角・半角、大文字・小文字の違いで検知をすり抜けられないようにする。
    （「ＮＧ」と「ng」を同じものとして扱う）
    """
    return unicodedata.normalize("NFKC", text or "").lower()


def _squeeze(text: str) -> str:
    """
    すべての空白と改行を取り除く。

    「あ い う」のように字の間を空けて書く逃げ方に備える。
    ⚠️ ただし取り除いた形だけで判定すると、別の語がつながって
       たまたま一致してしまう。普通の形と**両方**で見ること。
    """
    return re.sub(r"\s+", "", text)


class Window:
    """
    「何秒以内に何回」を数える入れ物。

    ⚠️ 時刻は time.monotonic() を使う。
       壁時計だと、時刻の修正やサマータイムで数え間違える。
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._slots: dict[int, deque[float]] = defaultdict(deque)

    def add(self, key: int, *, now: float | None = None) -> int:
        """1件足して、窓の中の件数を返す。"""
        t = time.monotonic() if now is None else now
        q = self._slots[key]
        q.append(t)
        cut = t - self.seconds
        while q and q[0] < cut:
            q.popleft()
        return len(q)

    def count(self, key: int, *, now: float | None = None) -> int:
        t = time.monotonic() if now is None else now
        q = self._slots[key]
        cut = t - self.seconds
        while q and q[0] < cut:
            q.popleft()
        return len(q)

    def clear(self, key: int | None = None) -> None:
        if key is None:
            self._slots.clear()
        else:
            self._slots.pop(key, None)

    def prune(self, *, now: float | None = None) -> int:
        """
        空になった分を捨てる。消した件数を返す。

        ⚠️ これを呼ばないと、来た人の数だけ辞書が増え続ける。
           定期処理から呼ぶこと。
        """
        t = time.monotonic() if now is None else now
        cut = t - self.seconds
        gone = []
        for key, q in self._slots.items():
            while q and q[0] < cut:
                q.popleft()
            if not q:
                gone.append(key)
        for key in gone:
            del self._slots[key]
        return len(gone)


class Detector:
    """
    検知のまとめ役。BOT全体で1つだけ持つ。

    ⚠️ 設定（何秒以内に何件）は変えられるので、窓の長さも作り直す。
    """

    def __init__(self) -> None:
        self._spam: Window | None = None
        self._spam_seconds: float = -1.0
        self._joins: Window | None = None
        self._join_seconds: float = -1.0
        # 直前に出した文（同じ文の繰り返しを見るため）。利用者ごとに数件。
        self._recent: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=4))

    # -- 窓の用意 -------------------------------------------

    def spam_window(self) -> Window:
        secs = float(settings.get("guard_spam_seconds", 5) or 5)
        if self._spam is None or secs != self._spam_seconds:
            self._spam = Window(secs)
            self._spam_seconds = secs
        return self._spam

    def join_window(self) -> Window:
        secs = float(settings.get("guard_raid_seconds", 10) or 10)
        if self._joins is None or secs != self._join_seconds:
            self._joins = Window(secs)
            self._join_seconds = secs
        return self._joins

    # -- 入室（レイド検知）----------------------------------

    def note_join(self, guild_id: int, *, now: float | None = None) -> Hit | None:
        """
        入室を1件数えて、レイドなら Hit を返す。

        ⚠️ 設定が切れていても**数えるのは続ける**。
           あとから有効にしたときに、直前の入室も見られるようにしておく。
        """
        count = self.join_window().add(guild_id, now=now)
        if not settings.get("guard_raid_enabled", False):
            return None
        limit = int(settings.get("guard_raid_joins", 5) or 5)
        if limit <= 0 or count < limit:
            return None
        secs = int(settings.get("guard_raid_seconds", 10) or 10)
        return Hit(
            kind="raid",
            detail=f"{secs}秒以内に {count} 人が入室しました（しきい値 {limit} 人）",
            action=ACT_NONE,
        )

    def reset_joins(self, guild_id: int) -> None:
        self.join_window().clear(guild_id)

    # -- メッセージ -----------------------------------------

    def check_message(
        self,
        message: discord.Message,
        *,
        now: float | None = None,
        has_content: bool = True,
    ) -> list[Hit]:
        """
        1通を見て、当てはまったものを返す。

        has_content=False のときは、内容を読まずに分かるものだけ見る。
        """
        hits: list[Hit] = []
        uid = message.author.id

        # ① メンションの数（内容を読まなくても数えられる）
        limit = int(settings.get("guard_mention_limit", 0) or 0)
        if limit > 0:
            n = len(set(message.mentions)) + len(set(message.role_mentions))
            if message.mention_everyone:
                n += 1
            if n > limit:
                hits.append(Hit(
                    kind="mention",
                    detail=f"1通に {n} 件のメンション（上限 {limit}）",
                    action=ACT_DELETE,
                ))

        # ② 連投の速さ（内容を読まなくても数えられる）
        if settings.get("guard_spam_enabled", False):
            need = int(settings.get("guard_spam_messages", 6) or 6)
            count = self.spam_window().add(uid, now=now)
            if need > 0 and count >= need:
                secs = int(settings.get("guard_spam_seconds", 5) or 5)
                action = str(settings.get("guard_spam_action", "timeout"))
                hits.append(Hit(
                    kind="spam",
                    detail=f"{secs}秒以内に {count} 件（しきい値 {need} 件）",
                    action=ACT_TIMEOUT if action == "timeout" else ACT_DELETE,
                ))

        if not has_content:
            return hits

        raw = message.content or ""
        text = normalize(raw)
        squeezed = _squeeze(text)

        # ③ 同じ文の繰り返し
        if settings.get("guard_spam_enabled", False) and text.strip():
            seen = self._recent[uid]
            if len(seen) >= 2 and all(t == text for t in list(seen)[-2:]):
                action = str(settings.get("guard_spam_action", "timeout"))
                hits.append(Hit(
                    kind="repeat",
                    detail="同じ文を3回続けて投稿",
                    action=ACT_TIMEOUT if action == "timeout" else ACT_DELETE,
                ))
            seen.append(text)

        # ④ 他サーバーの招待リンク
        if settings.get("guard_invite_block", False):
            codes = [m.group("code") for m in INVITE_RE.finditer(raw)]
            if codes:
                hits.append(Hit(
                    kind="invite",
                    detail=f"招待リンク {', '.join(codes[:3])}",
                    action=ACT_DELETE,
                ))

        # ⑤ NGワード
        words = settings.get("guard_words") or []
        for w in words:
            needle = normalize(str(w))
            if not needle:
                continue
            # 普通の形と、空白を抜いた形の両方で見る
            if needle in text or _squeeze(needle) in squeezed:
                hits.append(Hit(
                    kind="word",
                    detail=f"NGワード「{w}」",
                    action=ACT_DELETE,
                ))
                break        # 1語見つかれば十分（並べても対処は同じ）

        return hits

    def forget(self, user_id: int) -> None:
        self._recent.pop(user_id, None)
        self.spam_window().clear(user_id)

    def prune(self) -> int:
        """定期処理から呼ぶ。古い記録を捨てる。"""
        gone = self.spam_window().prune() + self.join_window().prune()
        stale = [uid for uid, q in self._recent.items() if not q]
        for uid in stale:
            del self._recent[uid]
        return gone + len(stale)


# BOT全体で1つ
detector = Detector()


def exempt(member: discord.Member | discord.abc.User) -> bool:
    """
    検知の対象から外す人か。

    ⚠️ BOT自身と他のBOTは必ず外す。外さないと、BOTの連続投稿を
       スパムとみなして自分を止めてしまう。
    """
    if getattr(member, "bot", False):
        return True
    roles = getattr(member, "roles", None)
    if roles is None:
        return False          # DMなど。ロールが無いだけで免除はしない
    # サーバーを管理できる人は外す（運営の一斉投稿で止まらないように）
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    exempt_ids = {int(r) for r in (settings.get("guard_exempt_roles") or [])}
    return any(r.id in exempt_ids for r in roles)


def account_age_days(user: discord.abc.User) -> float | None:
    """アカウントが作られてから何日たったか。"""
    created = getattr(user, "created_at", None)
    if created is None:
        return None
    from datetime import datetime, timezone

    return (datetime.now(timezone.utc) - created).total_seconds() / 86400


def new_account_hit(user: discord.abc.User) -> Hit | None:
    """作りたてのアカウントなら Hit を返す（知らせるだけ）。"""
    days = int(settings.get("guard_new_account_days", 0) or 0)
    if days <= 0:
        return None
    age = account_age_days(user)
    if age is None or age >= days:
        return None
    return Hit(
        kind="newaccount",
        detail=f"アカウント作成から {age:.1f} 日（しきい値 {days} 日）",
        action=ACT_NONE,
    )
