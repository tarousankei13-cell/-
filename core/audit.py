"""
管理操作の記録

誰がいつ何を変えたかを残す。お金を扱うので、あとから
「この設定は誰が変えたのか」に答えられる必要がある。

記録する側は `await audit.record(...)` を1行足すだけでよい。
**記録に失敗しても、元の操作は止めない**（記録のために操作が
できなくなるほうが困るため）。ただしログには必ず残す。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from db.models import AuditLog, as_utc
from db.session import session_scope

log = logging.getLogger("bot.audit")

# 操作の種類と、一覧に出すときの見出し
ACTIONS: dict[str, str] = {
    "subsidy.global": "全体の負担率を変更",
    "subsidy.role": "ロール別の負担率を変更",
    "subsidy.user": "利用者別の負担率を変更",
    "subsidy.remove": "負担率ルールを削除",
    "balance.grant": "残高を付与",
    "balance.adjust": "残高を調整",
    "user.ban": "利用を停止",
    "user.unban": "利用停止を解除",
    "account.add": "アカウントを追加",
    "account.remove": "アカウントを削除",
    "account.card": "カードを設定",
    "account.relogin": "再ログイン",
    "account.quarantine": "アカウントを隔離",
    "config.set": "設定を変更",
    "panel.post": "パネルを設置",
    "order.review": "注文を手動で処理",
    "achievement.proxy": "実績を代理送信",
    "broadcast": "一斉通知を送信",
    "backup.restore": "バックアップから復元",
    "bot.restart": "BOTを再起動",
}


def label(action: str) -> str:
    return ACTIONS.get(action, action)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:2000]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:2000]
    except (TypeError, ValueError):
        return str(value)[:2000]


async def record(
    *,
    actor_id: int,
    action: str,
    actor_name: str | None = None,
    target: str | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    detail: Any = None,
) -> None:
    """
    操作を1件記録する。

    ⚠️ ここで例外を外に出さない。記録できなかったせいで
       管理操作そのものが失敗すると、かえって危ない。
    """
    try:
        async with session_scope() as s:
            s.add(
                AuditLog(
                    actor_id=int(actor_id),
                    actor_name=(actor_name or "")[:64] or None,
                    action=action[:48],
                    target=(str(target)[:64] if target is not None else None),
                    before=_dump(before),
                    after=_dump(after),
                    reason=(reason or "")[:255] or None,
                    detail=_dump(detail),
                )
            )
    except Exception:
        log.exception("監査ログを書けませんでした: %s / %s", action, target)
        return
    log.info(
        "監査: %s が「%s」を実行しました（対象 %s）",
        actor_name or actor_id, label(action), target or "—",
    )


@dataclass
class Entry:
    """一覧表示用に整えたもの。"""
    id: int
    when: Any
    actor_id: int
    actor_name: str
    action: str
    target: str
    before: str
    after: str
    reason: str

    def line(self) -> str:
        who = self.actor_name or f"`{self.actor_id}`"
        text = f"<t:{int(self.when.timestamp())}:R>　**{label(self.action)}**　{who}"
        if self.target:
            text += f"\n　対象 `{self.target}`"
        if self.before or self.after:
            text += f"\n　{self.before or '—'} → **{self.after or '—'}**"
        if self.reason:
            text += f"\n　理由: {self.reason}"
        return text


async def search(
    *,
    actor_id: int | None = None,
    action: str | None = None,
    target: str | None = None,
    limit: int = 20,
) -> list[Entry]:
    """新しい順に取り出す。"""
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 100))
    if actor_id is not None:
        q = q.where(AuditLog.actor_id == int(actor_id))
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if target is not None:
        q = q.where(AuditLog.target == str(target))

    async with session_scope() as s:
        rows = (await s.execute(q)).scalars().all()

    return [
        Entry(
            id=r.id,
            when=as_utc(r.created_at),
            actor_id=r.actor_id,
            actor_name=r.actor_name or "",
            action=r.action,
            target=r.target or "",
            before=r.before or "",
            after=r.after or "",
            reason=r.reason or "",
        )
        for r in rows
    ]


async def count() -> int:
    from sqlalchemy import func

    async with session_scope() as s:
        return int(await s.scalar(select(func.count()).select_from(AuditLog)) or 0)
