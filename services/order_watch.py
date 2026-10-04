"""
できあがり通知

注文が済んだあと、マクドナルド側の状態をしばらく見張り、
できあがったら利用者へDMで知らせる。

⚠️ **相手の状態値（status）の意味は分かっていない。**
   実データで確かめられたのは「注文が済んだ時点の値」だけ。
   そこで

     ・ブザー番号が出たら → それが一番確かな合図
     ・状態値が注文時から変わったら → できあがったとみなす

   の2段構えにしてある。見かけた状態値は監査ログに残すので、
   あとから本当の意味が分かれば `READY_STATUS` に固定できる。

⚠️ 問い合わせるたびに相手への通信が増える。既定では無効で、
   有効にしても「見張る分数」と「同時に見張る件数」で上限をかける。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

import config
from core import audit, settings
from db.models import Order, as_utc, utcnow
from db.session import session_scope

log = logging.getLogger("bot.order_watch")


def enabled() -> bool:
    return bool(settings.get("ready_notify", config.READY_NOTIFY))


def watch_minutes() -> int:
    return max(1, int(settings.get("ready_watch_minutes", config.READY_WATCH_MINUTES)))


def poll_seconds() -> int:
    return max(15, int(settings.get("ready_poll_seconds", config.READY_POLL_SECONDS)))


@dataclass
class Ready:
    """できあがったと判断した注文。"""
    order_id: str
    discord_id: int
    receipt_number: str
    store_name: str
    buzzer_number: int | None = None
    reason: str = ""          # buzzer / status


async def pending() -> list[Order]:
    """いま見張るべき注文。古いものと、知らせ済みのものは外す。"""
    if not enabled():
        return []
    from core import saga

    limit = utcnow() - timedelta(minutes=watch_minutes())
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Order)
                .where(
                    Order.state.in_(saga.PAID_OR_LATER),
                    Order.ready_notified.is_(False),
                    Order.order_token.isnot(None),
                    Order.mcd_account_id.isnot(None),
                )
                .order_by(Order.created_at.desc())
                .limit(int(config.READY_MAX_WATCHED))
            )
        ).scalars().all()
        out = []
        for r in rows:
            if r.created_at is not None and as_utc(r.created_at) < limit:
                continue          # 見張る時間を過ぎた
            s.expunge(r)
            out.append(r)
        return out


async def give_up_old() -> int:
    """
    見張る時間を過ぎた注文に印を付ける。

    ⚠️ 付けないと、毎回ふるい落とすために読み直すことになる。
    """
    limit = utcnow() - timedelta(minutes=watch_minutes())
    n = 0
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Order).where(
                    Order.ready_notified.is_(False),
                    Order.created_at < limit,
                    Order.order_token.isnot(None),
                )
            )
        ).scalars().all()
        for r in rows:
            r.ready_notified = True
            n += 1
    return n


async def check(order: Order) -> Ready | None:
    """
    1件を問い合わせる。できあがっていれば Ready を返す。

    ⚠️ ここで何があっても注文は成立済み。例外を外へ出さない。
    """
    from services.mcd import accounts as mcd_accounts

    handle = None
    try:
        handle = await mcd_accounts.open_account(int(order.mcd_account_id))
        group = order.group_name or ""
        token = order.order_token or ""
        if not group or not token:
            return None

        buzzer = await handle.client.get_buzzer_number(group, token)
        status = None
        try:
            res = await handle.client.get_paid_order(group, token)
            status = int(getattr(res, "status", 0) or 0)
        except Exception:
            pass                        # 状態が取れなくてもブザー番号で判断できる
    except Exception as e:
        log.info("できあがりの確認に失敗しました（%s）: %s", order.id, e)
        return None
    finally:
        if handle:
            await handle.aclose()

    before = order.last_status
    ready = None
    if buzzer:
        ready = Ready(
            order_id=order.id, discord_id=order.discord_id,
            receipt_number=order.receipt_number or "",
            store_name=order.store_name or "",
            buzzer_number=int(buzzer), reason="buzzer",
        )
    elif status is not None and before is not None and status != before:
        ready = Ready(
            order_id=order.id, discord_id=order.discord_id,
            receipt_number=order.receipt_number or "",
            store_name=order.store_name or "",
            reason="status",
        )

    async with session_scope() as s:
        row = await s.get(Order, order.id)
        if row is None:
            return None
        if status is not None:
            # ⚠️ 初回は控えるだけ。これを基準に「変わったか」を見る。
            if row.last_status is None:
                row.last_status = status
                ready = ready if ready and ready.reason == "buzzer" else None
            else:
                row.last_status = status
        if buzzer:
            row.buzzer_number = int(buzzer)
        if ready:
            if row.ready_notified:
                return None            # ほかの周回で知らせ済み
            row.ready_notified = True
            row.ready_at = utcnow()

    if ready:
        # ⚠️ 見かけた状態値を必ず残す。相手の状態値の意味はまだ
        #    分かっていないので、ここが唯一の手がかりになる。
        await audit.record(
            actor_id=0, actor_name="BOT", action="order.ready",
            target=str(order.discord_id),
            detail={
                "receipt": order.receipt_number,
                "判断": ready.reason,
                "状態値": f"{before} → {status}",
                "ブザー番号": buzzer,
            },
        )
    return ready


async def sweep() -> list[Ready]:
    """見張っている注文をひと回りする。できあがったものを返す。"""
    if not enabled():
        return []
    await give_up_old()
    out = []
    for order in await pending():
        got = await check(order)
        if got:
            out.append(got)
    return out
