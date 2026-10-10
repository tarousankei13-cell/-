"""定期処理の中身"""

from __future__ import annotations

import asyncio
import logging

import config
from datetime import datetime, timezone
from pathlib import Path

from core import ledger as L
from db.session import session_scope
from services.kyash import accounts as kyash_accounts
from services.mcd import accounts as mcd_accounts
from services.mcd import stores as mcd_stores
from services.mcd.menu import MenuDiff

log = logging.getLogger("bot.tasks")


# ============================================================
#  メニューの自動同期
# ============================================================

async def sync_all_menus(force: bool = False) -> dict[str, MenuDiff]:
    """
    使われている店舗の商品・提供時間帯・店舗情報を最新にする。

    force=True のときは ETag を無視して必ず取り直す
    （提供時間帯は日付ごとの定義なので、日付が変わったら必要）。
    """
    store_ids = await mcd_stores.active_store_ids()
    if not store_ids:
        return {}

    handle = None
    out: dict[str, MenuDiff] = {}
    try:
        handle = await mcd_accounts.pick_account()

        # 店舗ごとの同期は互いに関係が無いので同時に行う。
        # 順番に待つと、店舗が20件あるだけで十数秒かかっていた。
        # ETag が効くので大半は 304（0バイト）で終わる。
        sem = asyncio.Semaphore(config.MENU_SYNC_CONCURRENCY)

        async def one(store_id: str) -> None:
            async with sem:
                try:
                    # 店舗情報（営業時間・対応する受取方法）も一緒に最新にする
                    info = await mcd_stores.resolve_store(
                        handle.client, store_id, force=force
                    )
                    out[store_id] = await mcd_stores.sync_menu(
                        handle.client, store_id, store=info, force=force
                    )
                except Exception:
                    log.exception("店舗 %s の同期に失敗しました", store_id)

        await asyncio.gather(*[one(sid) for sid in store_ids])
    except Exception:
        log.exception("メニュー同期を開始できませんでした")
    finally:
        if handle:
            await handle.aclose()

    changed = sum(1 for d in out.values() if d.has_changes)
    log.info(
        "メニューを同期しました: %d店舗（変更あり %d店舗）", len(out), changed
    )
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


def format_menu_news(diffs: dict, *, min_stores: int = 1) -> str | None:
    """
    利用者向けのお知らせ。変更がなければ None。

    ⚠️ 管理者向け（format_menu_diff）とは作りが違う。
       あちらは店舗ごとに出すが、利用者には **全店まとめて**
       「何が増えた・終わった・いくらになった」だけを伝える。
       同じ新商品が300店舗ぶん並んでも読めないため、商品名で束ねる。

    ⚠️ 1店舗だけの変更は出さないようにもできる（min_stores）。
       改装中の1店だけ品切れ、といったものを全体のお知らせにしない。
    """
    added: dict[str, set] = {}
    removed: dict[str, set] = {}
    priced: dict[tuple[str, int, int], set] = {}

    for store_id, diff in (diffs or {}).items():
        for _code, name in getattr(diff, "added", []) or []:
            added.setdefault(name, set()).add(store_id)
        for _code, name in getattr(diff, "removed", []) or []:
            removed.setdefault(name, set()).add(store_id)
        for _code, name, old_p, new_p in getattr(diff, "price_changed", []) or []:
            priced.setdefault((name, int(old_p), int(new_p)), set()).add(store_id)

    def keep(d):
        return {k: v for k, v in d.items() if len(v) >= min_stores}

    added, removed, priced = keep(added), keep(removed), keep(priced)
    if not (added or removed or priced):
        return None

    lines = []
    if added:
        names = "\n".join(f"・{n}" for n in sorted(added)[:10])
        more = f"\n　ほか {len(added) - 10} 品" if len(added) > 10 else ""
        lines.append(f"**新しく登場しました**\n{names}{more}")
    if priced:
        rows = []
        for (name, old_p, new_p) in sorted(priced)[:10]:
            arrow = "↑" if new_p > old_p else "↓"
            rows.append(f"・{name}　¥{old_p:,} → **¥{new_p:,}** {arrow}")
        more = f"\n　ほか {len(priced) - 10} 品" if len(priced) > 10 else ""
        lines.append("**お値段が変わりました**\n" + "\n".join(rows) + more)
    if removed:
        names = "\n".join(f"・{n}" for n in sorted(removed)[:10])
        more = f"\n　ほか {len(removed) - 10} 品" if len(removed) > 10 else ""
        lines.append(f"**販売を終了しました**\n{names}{more}")
    return "\n\n".join(lines)


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
        # ⚠️ PayPay も忘れずに。片方だけ戻すと上限の判定がずれる。
        from services.paypay import accounts as paypay_accounts

        await paypay_accounts.reset_monthly_counters()
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
    # ファイル名は日本時間（管理者が見て分かる時刻にする）
    stamp = config.now_jst().strftime("%Y%m%d-%H%M")
    return f"backup-{stamp}.db", data


# ============================================================
#  Kyash トークンの期限
# ============================================================

async def kyash_token_warnings() -> list[str]:
    """
    トークンの期限が近い口座。Kyash と PayPay の両方を見る。

    ⚠️ どちらも取り直しに本人の操作が要る（Kyashはメール/SMSのOTP、
       PayPayはSMSのURL）。切れてから気付くと、その間チャージが
       受けられない。
    """
    out = []
    from services.paypay import accounts as paypay_accounts

    for account_id, label, days in await paypay_accounts.expiring_accounts():
        if days <= 0:
            out.append(f"🔴 PayPay `#{account_id}` **{label}** のトークンは**失効しています**")
        else:
            out.append(f"🟡 PayPay `#{account_id}` **{label}** のトークンは残り **{int(days)}日** です")
    for account_id, label, days in await kyash_accounts.expiring_accounts():
        if days <= 0:
            out.append(f"🔴 Kyash `#{account_id}` **{label}** のトークンは**失効しています**")
        else:
            out.append(f"🟡 Kyash `#{account_id}` **{label}** のトークンは残り **{int(days)}日** です")
    return out


# ============================================================
#  BOTの貸し出し — 期限の予告と、切れたときの知らせ
# ============================================================

async def license_notices(bot) -> list[tuple[int, str, str]]:
    """期限が近い／切れた貸し先に知らせる。

    返すのは (guild_id, 印, 送り先) の一覧。送れた分だけ。

    ⚠️ **印を付けてから送る。** 送ってから付けると、送信の途中で
       落ちたときに同じ予告を何度も送る。送れなかった場合に
       1回ぶん取りこぼすほうが、何度も送るより害が小さい。

    ⚠️ ホームには送らない。期限が無いので送る内容が無い。
    """
    # ⚠️ ここだけ UI を使う。tasks.py の他の処理は画面に依存しない作りなので、
    #    上に import を足さず、この関数の中に閉じ込める。
    import discord

    import emoji as E
    from core import license as lic
    from ui import embeds

    out: list[tuple[int, str, str]] = []
    try:
        rows = await lic.all_licenses()
    except Exception:
        log.exception("貸し出しの一覧を読めませんでした")
        return out

    for row in rows:
        if row.is_home or row.suspended:
            continue
        st = lic._status_of(row)
        tag = lic.due_notice(st)
        if tag is None:
            continue
        if not await lic.mark_notified(row.guild_id, tag):
            continue        # すでに送った

        left = st.days_left or 0
        if tag == "0":
            title = "ご利用期限が切れました"
            body = (
                "このサーバーでのご利用期限が切れたため、"
                "BOTの機能はすべて停止しました。\n\n"
                "引き続きご利用になる場合は、期限の延長をご依頼ください。\n"
                "**これまでのデータはそのまま残っています。**"
            )
            color = embeds.RED
        else:
            title = f"ご利用期限まで、あと {left} 日です"
            body = (
                f"期限: **{st.expires_at.astimezone(config.JST):%Y/%m/%d %H:%M}**\n\n"
                "期限を過ぎると、このサーバーでのBOTの機能は停止します。\n"
                "延長をご希望の場合はご連絡ください。"
            )
            color = embeds.YELLOW if left <= 3 else embeds.BLUE

        e = discord.Embed(title=f"{E.BELL} {title}", description=body, color=color)

        sent_to = ""
        # ① 連絡先の人へDM
        if row.contact_id:
            try:
                user = bot.get_user(row.contact_id) or await bot.fetch_user(row.contact_id)
                await user.send(embed=e)
                sent_to = "DM"
            except Exception:
                log.debug("予告のDMを送れませんでした: %s", row.contact_id, exc_info=True)
        # ② サーバーへも出す（DMが届かない場合の保険）
        g = bot.get_guild(row.guild_id)
        if g is not None:
            ch = g.system_channel or next(
                (c for c in g.text_channels
                 if c.permissions_for(g.me).send_messages), None,
            )
            if ch is not None:
                try:
                    await ch.send(embed=e)
                    sent_to = (sent_to + "＋サーバー") if sent_to else "サーバー"
                except Exception:
                    log.debug("予告をサーバーへ送れませんでした", exc_info=True)

        out.append((row.guild_id, tag, sent_to or "送れず"))
        log.info("貸し出しの予告: guild=%s 印=%s 送り先=%s",
                 row.guild_id, tag, sent_to or "送れず")
    return out


def license_summary(rows) -> str:
    """オーナー向けの短い要約。定期処理の報告に添える。"""
    from core import license as lic

    live = expiring = expired = 0
    for r in rows:
        if r.is_home:
            continue
        st = lic._status_of(r)
        if not st.allowed:
            expired += 1
            continue
        live += 1
        if (st.days_left or 99) <= 7:
            expiring += 1
    if not (live or expired):
        return ""
    parts = [f"貸し出し中 **{live}**"]
    if expiring:
        parts.append(f"期限間近 **{expiring}**")
    if expired:
        parts.append(f"停止中 **{expired}**")
    return "　".join(parts)
