"""定期処理の中身"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import config
from core import ledger as L
from core import settings
from db.session import session_scope
from services.kyash import accounts as kyash_accounts
from services.mcd import accounts as mcd_accounts
from services.mcd import stores as mcd_stores
from services.mcd.menu import MenuDiff

log = logging.getLogger("bot.tasks")


# ============================================================
#  メニューの自動同期
# ============================================================

async def sync_all_menus() -> dict[str, MenuDiff]:
    """直近で使われた店舗のメニューを更新する。新商品はここで取り込まれる。"""
    store_ids = await mcd_stores.active_store_ids()
    if not store_ids:
        return {}

    handle = None
    out: dict[str, MenuDiff] = {}
    try:
        handle = await mcd_accounts.pick_account()
        for store_id in store_ids:
            try:
                out[store_id] = await mcd_stores.sync_menu(handle.client, store_id)
            except Exception:
                log.exception("店舗 %s のメニュー同期に失敗しました", store_id)
    except Exception:
        log.exception("メニュー同期を開始できませんでした")
    finally:
        if handle:
            await handle.aclose()
    return out


def format_menu_diff(store_id: str, store_name: str, diff: MenuDiff) -> str | None:
    """差分を人が読める形にする。変更がなければ None。"""
    if not diff.has_changes:
        return None
    lines = [f"**{store_name or store_id}**（`{store_id}`）"]
    if diff.added:
        names = "　".join(n for _, n in diff.added[:5])
        more = f" ほか{len(diff.added) - 5}件" if len(diff.added) > 5 else ""
        lines.append(f"➕ 新商品 {len(diff.added)}件　{names}{more}")
    if diff.removed:
        names = "　".join(n for _, n in diff.removed[:5])
        more = f" ほか{len(diff.removed) - 5}件" if len(diff.removed) > 5 else ""
        lines.append(f"➖ 販売終了 {len(diff.removed)}件　{names}{more}")
    if diff.price_changed:
        changes = "\n".join(
            f"　{n}　¥{o:,} → ¥{p:,}" for _, n, o, p in diff.price_changed[:5]
        )
        more = f"\n　ほか{len(diff.price_changed) - 5}件" if len(diff.price_changed) > 5 else ""
        lines.append(f"💴 価格変更 {len(diff.price_changed)}件\n{changes}{more}")
    return "\n".join(lines)


# ============================================================
#  トークンの事前更新
# ============================================================

async def warm_tokens() -> int:
    """
    使えるアカウントのトークンを先に更新しておく。

    注文のときに取り直さずに済むので、待ち時間が短くなる。
    """
    from sqlalchemy import select

    from db.models import McdAccount

    async with session_scope() as s:
        ids = [
            a.id for a in (
                await s.execute(
                    select(McdAccount).where(McdAccount.status.in_(mcd_accounts.USABLE))
                )
            ).scalars().all()
        ]

    warmed = 0
    for account_id in ids:
        handle = None
        try:
            handle = await mcd_accounts.open_account(account_id)
            await handle.client.ensure_auth()
            warmed += 1
        except Exception as e:
            log.info("アカウント %s のトークン更新に失敗しました: %s", account_id, e)
        finally:
            if handle:
                await handle.aclose()
    return warmed


# ============================================================
#  元帳の点検
# ============================================================

async def check_ledger() -> tuple[bool, str]:
    async with session_scope() as s:
        report = await L.verify_integrity(s)
        outstanding = await L.outstanding_user_balance(s)

    if report.ok:
        return True, ""

    lines = ["**元帳の整合性が破れています。至急確認してください。**"]
    if report.broken:
        lines.append(f"貸借が一致しない取引: {len(report.broken)} 件")
        lines += [f"　`{t}` 差額 {d:+,}" for t, d in report.broken[:5]]
    if report.negative:
        lines.append(f"マイナス残高: {len(report.negative)} 件")
        lines += [f"　`{a}` {b:,}" for a, b in report.negative[:5]]
    lines.append(f"利用者の未使用残高合計: ¥{outstanding:,}")
    return False, "\n".join(lines)


# ============================================================
#  日次・月次のリセット
# ============================================================

async def daily_reset() -> None:
    await mcd_accounts.reset_daily_counters()
    log.info("アカウントの当日カウンタをリセットしました")


async def monthly_reset_if_needed(last_month: int | None) -> int:
    now = datetime.now(timezone.utc)
    if last_month != now.month:
        await kyash_accounts.reset_monthly_counters()
        log.info("Kyash口座の月間受取額をリセットしました")
    return now.month


# ============================================================
#  バックアップ
# ============================================================

async def make_backup() -> tuple[str, bytes]:
    """
    データベースのバックアップを作る。

    SQLite はファイルをそのまま返す。無料ホスティングでデータが失われても
    元帳を復元できるよう、管理者チャンネルへ定期的に送る。
    """
    import asyncio

    from db.session import _engine

    url = str(_engine.url) if _engine else ""
    if "sqlite" not in url:
        raise RuntimeError(
            "SQLite以外のデータベースは、この機能ではバックアップできません。"
            "PostgreSQL の場合は pg_dump をご利用ください。"
        )

    path = Path(url.split("///", 1)[-1])
    if not path.exists():
        raise RuntimeError(f"データベースファイルが見つかりません: {path}")

    def _read() -> bytes:
        # WALモードのため、-wal も含めて整合性を取る
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "backup.db"
            src = sqlite3.connect(str(path))
            dst = sqlite3.connect(str(dest))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            return dest.read_bytes()

    data = await asyncio.to_thread(_read)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"backup-{stamp}.db", data


# ============================================================
#  Kyash トークンの期限
# ============================================================

async def kyash_token_warnings() -> list[str]:
    out = []
    for account_id, label, days in await kyash_accounts.expiring_accounts():
        if days <= 0:
            out.append(f"🔴 `#{account_id}` **{label}** のトークンは**失効しています**")
        else:
            out.append(f"🟡 `#{account_id}` **{label}** のトークンは残り **{int(days)}日** です")
    return out
