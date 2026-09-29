"""定期実行まわり。

* 呼び出し（バズー）番号の追跡
* 決済成否不明の注文を後から照合する
* 月次レポート・日次サマリーの送信
* トークンのヘルスチェックと期限警告
* パネルの混雑状況の更新
* 期限切れの承認待ちと未完了のリンク予約の掃除
* SQLite の自動バックアップ
* 長期未使用の残高の通知
* 自動メンテナンスからの復帰
* 起動時の自己診断
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from mcd.store import JST, now_jst

from ._shared import BAD, INFO, MONEY, OK, WARN, dm, embed, post, yen

log = logging.getLogger("bot.tasks")

PANEL_KEY = "panel_message"
LAST_MONTHLY_KEY = "last_monthly_report"
LAST_DAILY_KEY = "last_daily_summary"
LAST_BACKUP_KEY = "last_backup"
LAST_DORMANT_KEY = "last_dormant_check"
MAINTENANCE_SINCE_KEY = "maintenance_since"


class Tasks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._buzzer_tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        # 間隔は設定値に合わせる（デコレータの値は既定値でしかない）
        try:
            self.health_loop.change_interval(hours=int(self.bot.cfg.HEALTH_CHECK_HOURS))
        except Exception:
            log.warning("ヘルスチェックの間隔を設定できませんでした。既定値で動かします。")
        self.health_loop.start()
        self.panel_loop.start()
        self.daily_loop.start()
        self.unknown_loop.start()

    async def cog_unload(self) -> None:
        self.health_loop.cancel()
        self.panel_loop.cancel()
        self.daily_loop.cancel()
        self.unknown_loop.cancel()
        for task in list(self._buzzer_tasks):
            task.cancel()

    # -------------------------------------------------------- 起動時の自己診断

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # 再接続のたびに送らないよう1度だけ
        if getattr(self, "_diagnosed", False):
            return
        self._diagnosed = True
        await asyncio.sleep(5)
        await self.self_diagnose()

    async def self_diagnose(self) -> None:
        """設定・権限・トークン・フォントをまとめて点検し、オーナーへ DM する。"""
        cfg = self.bot.cfg
        problems: list[str] = []
        notes: list[str] = []

        missing = cfg.missing_required()
        if missing:
            problems.append(
                f"{cfg.E_NG} 未設定: " + "、".join(s.label for s in missing)
            )

        # 投稿できないチャンネルを事前に洗い出す
        checks = [
            ("パネル", cfg.PANEL_CHANNEL_ID),
            ("実績", cfg.ACHIEVEMENT_CHANNEL_ID),
            ("承認待ち", cfg.APPROVAL_CHANNEL_ID),
            ("注文ログ", cfg.LOG_ORDERS_CHANNEL_ID),
            ("入出金ログ", cfg.LOG_MONEY_CHANNEL_ID),
            ("エラーログ", cfg.LOG_ERRORS_CHANNEL_ID),
            ("管理ログ", cfg.LOG_ADMIN_CHANNEL_ID),
        ]
        for label, channel_id in checks:
            if not channel_id:
                continue
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except Exception:
                    problems.append(f"{cfg.E_NG} {label}チャンネルが見つかりません")
                    continue
            guild = getattr(channel, "guild", None)
            me = guild.me if guild else None
            if me is not None:
                perms = channel.permissions_for(me)
                if not (perms.view_channel and perms.send_messages):
                    problems.append(f"{cfg.E_NG} {label}チャンネルに投稿できません")
                elif label in ("パネル", "実績", "承認待ち") and not perms.embed_links:
                    problems.append(f"{cfg.E_WARN} {label}チャンネルで埋め込みが使えません")

        mcd_rows = await asyncio.to_thread(self.bot.store.list_mcd_accounts, True)
        kyash_status = await asyncio.to_thread(self.bot.kyash.token_status)
        if not mcd_rows:
            problems.append(f"{cfg.E_NG} マクドナルドアカウントが未登録です（/mcd login）")
        if not any(i["enabled"] for i in kyash_status):
            problems.append(f"{cfg.E_NG} Kyash アカウントが未登録です（/kyash login）")
        for item in kyash_status:
            days = item["days_left"]
            if item["enabled"] and days is not None and days <= cfg.KYASH_TOKEN_WARN_DAYS:
                problems.append(
                    f"{cfg.E_WARN} Kyash「{item['label']}」のトークンが残り {days} 日"
                )

        from mcd import cards

        if cards._font_path is None:
            problems.append(
                f"{cfg.E_WARN} 日本語フォントが見つかりません"
                "（番号カードの日本語が表示されません）"
            )
        else:
            notes.append(f"フォント: {cards._font_path}")

        if int(cfg.MAINTENANCE):
            problems.append(
                f"{cfg.E_WARN} メンテナンスモード中です（注文を受け付けません）"
            )

        mismatches = await asyncio.to_thread(self.bot.store.audit_balances)
        if mismatches:
            problems.append(f"{cfg.E_NG} 台帳と残高に不整合が {len(mismatches)} 件あります")

        pending = await asyncio.to_thread(self.bot.store.pending_reports)
        unknown = await asyncio.to_thread(self.bot.store.unknown_orders)
        if pending:
            notes.append(f"承認待ちの実績 {len(pending)} 件")
        if unknown:
            problems.append(f"{cfg.E_WARN} 決済成否が不明な注文が {len(unknown)} 件あります")

        ok = not problems
        e = embed(
            f"{cfg.E_OK} 起動しました（問題なし）" if ok else f"{cfg.E_WARN} 起動時の点検で問題があります",
            None,
            OK if ok else WARN,
            footer=cfg.BRAND_NAME,
        )
        if problems:
            e.add_field(name="要対応", value="\n".join(problems)[:1020], inline=False)
        e.add_field(
            name="状態",
            value=(
                f"マクドナルド {len(mcd_rows)} 件 / "
                f"Kyash {sum(1 for i in kyash_status if i['enabled'])} 件\n"
                + ("\n".join(notes) if notes else "")
            )[:1020],
            inline=False,
        )
        if problems:
            e.add_field(
                name="確認のしかた",
                value="`/config setup` で未設定の項目と手順を確認できます。",
                inline=False,
            )

        for owner_id in self.bot.owner_ids or []:
            await dm(self.bot, owner_id, e=e)
        log.info("起動時の点検: 問題 %s 件", len(problems))

    # ------------------------------------------------ 呼び出し番号の追跡

    def watch_buzzer(self, user_id: int, order_id: int, paid) -> None:
        task = asyncio.create_task(self._buzzer_worker(user_id, order_id, paid))
        self._buzzer_tasks.add(task)
        task.add_done_callback(self._buzzer_tasks.discard)

    async def _buzzer_worker(self, user_id: int, order_id: int, paid) -> None:
        cfg = self.bot.cfg
        for _ in range(cfg.BUZZER_POLL_MAX):
            await asyncio.sleep(cfg.BUZZER_POLL_SECONDS)
            try:
                number = await self.bot.mcd.buzzer(
                    paid.order_token, paid.group, paid.account_id
                )
            except Exception:
                continue
            if number:
                await dm(
                    self.bot, user_id,
                    e=embed(
                        f"{cfg.E_BELL} 呼び出し番号が出ました",
                        f"**{number}**\nカウンターでお受け取りください。",
                        OK,
                        footer=f"注文 #{order_id} ・ 注文番号 {paid.receipt_number}",
                    ),
                )
                await self.bot.send_log(
                    cfg.LOG_ORDERS_CHANNEL_ID,
                    embed(
                        f"{cfg.E_BELL} 呼び出し番号",
                        f"<@{user_id}> ・ 注文 #{order_id} ・ 番号 **{number}**",
                        INFO,
                    ),
                )
                return

        log.info("呼び出し番号を取得できませんでした (order=%s)", order_id)

    # ------------------------------------------------------ ヘルスチェック

    @tasks.loop(hours=6)
    async def health_loop(self) -> None:
        cfg = self.bot.cfg
        problems: list[str] = []

        for row in await asyncio.to_thread(self.bot.store.list_mcd_accounts, True):
            ok, detail = await self.bot.mcd.health_check(int(row["id"]))
            if not ok:
                problems.append(f"{cfg.E_NG} マック `#{row['id']}` {row['label']}: {detail[:120]}")

        for item in await asyncio.to_thread(self.bot.kyash.token_status):
            if not item["enabled"]:
                continue
            ok, detail = await self.bot.kyash.health_check(item["id"])
            if not ok:
                problems.append(f"{cfg.E_NG} Kyash `#{item['id']}` {item['label']}: {detail[:120]}")
            elif item["days_left"] is not None and item["days_left"] <= cfg.KYASH_TOKEN_WARN_DAYS:
                problems.append(
                    f"{cfg.E_WARN} Kyash `#{item['id']}` {item['label']}: "
                    f"トークンの期限まであと {item['days_left']} 日。"
                    f"`/kyash login` で再ログインしてください。"
                )

        if not problems:
            return

        owners = " ".join(f"<@{oid}>" for oid in (self.bot.owner_ids or []))
        await post(
            self.bot,
            cfg.LOG_ERRORS_CHANNEL_ID,
            content=owners or None,
            embed=embed(
                f"{cfg.E_WARN} ヘルスチェックで問題を検出しました",
                "\n".join(problems)[:3800],
                WARN,
            ),
        )

    @health_loop.before_loop
    async def _before_health(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)

    # ------------------------------------------------ パネルの混雑状況更新

    @tasks.loop(minutes=2)
    async def panel_loop(self) -> None:
        cfg = self.bot.cfg
        stored = await asyncio.to_thread(self.bot.store.get_kv, PANEL_KEY, None)
        if not stored:
            return
        channel_id, message_id = stored
        panel = self.bot.get_cog("Panel")
        if panel is None:
            return
        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(
                int(channel_id)
            )
            message = await channel.fetch_message(int(message_id))
            await message.edit(embed=panel.panel_embed())
        except discord.NotFound:
            await asyncio.to_thread(self.bot.store.set_kv, PANEL_KEY, None)
        except Exception:
            pass

    @panel_loop.before_loop
    async def _before_panel(self) -> None:
        await self.bot.wait_until_ready()

    # -------------------------------------- 日次: 月次レポートと掃除

    # ------------------------------------------ 決済成否不明の注文を照合する

    @tasks.loop(minutes=10)
    async def unknown_loop(self) -> None:
        """応答が切れて課金の有無が分からない注文を、後から照会して片付ける。

        手作業での突き合わせをなくすのが目的。判断できないものは触らない。
        """
        cfg = self.bot.cfg
        rows = await asyncio.to_thread(self.bot.store.unknown_orders)
        for order in rows:
            order_id = int(order["id"])
            uid = int(order["user_id"])
            paid, note = await self.bot.mcd.check_paid(
                order["order_token"] or "",
                order["group_name"] or "group-f",
                int(order["mcd_account_id"] or 0),
            )
            if paid is None:
                continue  # まだ分からない。次回に回す。

            await asyncio.to_thread(
                self.bot.store.resolve_unknown, order_id, paid, f"自動照合: {note}"
            )
            await asyncio.to_thread(
                self.bot.store.audit, "order.reconciled", 0,
                {"order": order_id, "paid": paid, "note": note},
            )

            if paid:
                # 課金されていた。残高は引いたままで正しい。
                await dm(
                    self.bot, uid,
                    e=embed(
                        f"{cfg.E_OK} 注文は成立していました",
                        f"確認中だった注文 #{order_id} は決済が完了していました。\n"
                        f"{note}\n\n"
                        "感想の投稿をお願いします（次回注文の条件です）。",
                        OK,
                    ),
                )
                await self.bot.send_log(
                    cfg.LOG_ORDERS_CHANNEL_ID,
                    embed(
                        f"{cfg.E_OK} 成否不明を解消（課金あり）",
                        f"<@{uid}> ・ 注文 #{order_id}\n{note}",
                        OK,
                    ),
                )
            else:
                # 課金されていなかった。引いた残高を戻す。
                from mcd.store import K_REFUND

                balance = await asyncio.to_thread(
                    self.bot.store.credit, uid, K_REFUND, int(order["paid_amount"]),
                    f"order:{order_id}", "自動照合で未課金と判明",
                )
                await asyncio.to_thread(self.bot.store.release_hex, order_id)
                await dm(
                    self.bot, uid,
                    e=embed(
                        f"{cfg.E_MONEY} 注文は成立していませんでした",
                        f"確認中だった注文 #{order_id} は決済されていませんでした。\n"
                        f"**{yen(int(order['paid_amount']))}** を返金しました"
                        f"（残高 {yen(balance)}）。\n"
                        "同じコードでもう一度お試しいただけます。",
                        MONEY,
                    ),
                )
                await self.bot.send_log(
                    cfg.LOG_MONEY_CHANNEL_ID,
                    embed(
                        f"{cfg.E_MONEY} 成否不明を解消（未課金・返金済み）",
                        f"<@{uid}> ・ 注文 #{order_id} ・ +{yen(int(order['paid_amount']))}",
                        MONEY,
                    ),
                )
            await asyncio.sleep(1)

    @unknown_loop.before_loop
    async def _before_unknown(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(90)

    @tasks.loop(minutes=30)
    async def daily_loop(self) -> None:
        await self._cleanup_stale_approvals()
        await self._cleanup_stale_reservations()
        await self._maybe_recover_maintenance()
        await self._maybe_backup()
        await self._maybe_daily_summary()
        await self._maybe_dormant_notice()
        await self._maybe_monthly_report()

    @daily_loop.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    async def _cleanup_stale_approvals(self) -> None:
        """期限を過ぎた承認待ちの注文を解放する。"""
        cfg = self.bot.cfg
        cutoff = now_jst() - timedelta(minutes=cfg.APPROVAL_TIMEOUT_MINUTES)
        stale = await asyncio.to_thread(self.bot.store.stale_pending_orders, cutoff)
        for order in stale:
            await asyncio.to_thread(self.bot.store.release_hex, int(order["id"]))
            await dm(
                self.bot, int(order["user_id"]),
                e=embed(
                    f"{cfg.E_CLOCK} 承認の期限が切れました",
                    f"注文 #{order['id']} は承認されなかったため取り消しました。\n"
                    "残高は引き落とされていません。",
                    WARN,
                ),
            )
            log.info("期限切れの承認待ちを解放しました (order=%s)", order["id"])

    async def _cleanup_stale_reservations(self) -> None:
        """受け取りに失敗したまま残った Kyash リンクの予約を掃除する。

        予約直後にプロセスが落ちると user_id=0 の行が残り、そのリンクが
        二度と使えなくなるため、1時間たったものは消す。
        """
        cutoff = now_jst() - timedelta(hours=1)
        removed = await asyncio.to_thread(self.bot.store.drop_stale_reservations, cutoff)
        if removed:
            log.info("未完了のリンク予約を %s 件掃除しました", removed)

    async def _maybe_recover_maintenance(self) -> None:
        """自動で入ったメンテナンスから、時間を置いて復帰を試みる。"""
        cfg = self.bot.cfg
        if not int(cfg.MAINTENANCE):
            return
        since = await asyncio.to_thread(self.bot.store.get_kv, MAINTENANCE_SINCE_KEY, None)
        if not since:
            return  # 手動で入れたものは自動で戻さない
        try:
            started = datetime.fromisoformat(str(since))
            if started.tzinfo is None:
                started = started.replace(tzinfo=JST)
        except ValueError:
            return
        if now_jst() - started < timedelta(minutes=int(cfg.CIRCUIT_COOLDOWN_MINUTES)):
            return

        # アカウントが1つでも生きていれば再開する
        ok_any = False
        for row in await asyncio.to_thread(self.bot.store.list_mcd_accounts, True):
            ok, _detail = await self.bot.mcd.health_check(int(row["id"]))
            if ok:
                ok_any = True
                break

        if not ok_any:
            await asyncio.to_thread(
                self.bot.store.set_kv, MAINTENANCE_SINCE_KEY,
                now_jst().isoformat(timespec="seconds"),
            )
            log.info("メンテナンスからの復帰を見送りました（まだ復旧していません）")
            return

        cfg.set("MAINTENANCE", 0)
        cfg.set("MAINTENANCE_NOTE", "")
        self.bot.mcd.consecutive_failures = 0
        await asyncio.to_thread(self.bot.store.set_kv, MAINTENANCE_SINCE_KEY, None)
        await asyncio.to_thread(self.bot.store.audit, "maintenance.auto_off", 0, {})

        panel = self.bot.get_cog("Panel")
        if panel is not None:
            await panel.refresh_panel()
        await self.bot.send_log(
            cfg.LOG_ERRORS_CHANNEL_ID,
            embed(
                f"{cfg.E_OK} メンテナンスから復帰しました",
                "接続が回復したため注文の受付を再開しました。",
                OK,
            ),
        )

    async def _maybe_backup(self) -> None:
        """1日1回、決まった時刻に SQLite のバックアップを取る。"""
        cfg = self.bot.cfg
        now = now_jst()
        if now.hour != int(cfg.BACKUP_HOUR):
            return
        tag = now.strftime("%Y-%m-%d")
        if await asyncio.to_thread(self.bot.store.get_kv, LAST_BACKUP_KEY, "") == tag:
            return
        await asyncio.to_thread(self.bot.store.set_kv, LAST_BACKUP_KEY, tag)
        try:
            path = await asyncio.to_thread(self.bot.store.backup, int(cfg.BACKUP_KEEP))
        except Exception:
            log.exception("バックアップに失敗しました")
            await self.bot.send_log(
                cfg.LOG_ERRORS_CHANNEL_ID,
                embed(f"{cfg.E_NG} バックアップに失敗しました", "ログを確認してください。", BAD),
            )
            return
        log.info("バックアップを作成しました: %s", path)
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_OK} バックアップを作成しました",
                f"`{path.name}`（保持 {cfg.BACKUP_KEEP} 世代）",
                INFO,
            ),
        )

    async def _maybe_daily_summary(self) -> None:
        """前日の実績をオーナーに DM する。持ち出し額の推移を把握するため。"""
        cfg = self.bot.cfg
        now = now_jst()
        if now.hour != int(cfg.DAILY_SUMMARY_HOUR):
            return
        tag = now.strftime("%Y-%m-%d")
        if await asyncio.to_thread(self.bot.store.get_kv, LAST_DAILY_KEY, "") == tag:
            return
        await asyncio.to_thread(self.bot.store.set_kv, LAST_DAILY_KEY, tag)

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        before = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        today_stat = await asyncio.to_thread(self.bot.store.daily_summary, yesterday)
        prev_stat = await asyncio.to_thread(self.bot.store.daily_summary, before)
        totals = await asyncio.to_thread(self.bot.store.dashboard_totals)

        diff = today_stat["burden"] - prev_stat["burden"]
        sign = "+" if diff > 0 else ""
        elapsed = max(1, now.day - 1)
        pace = int(totals["month"]["burden"] / elapsed * 30)

        e = embed(
            f"{cfg.E_CHART} {yesterday} の実績",
            None,
            MONEY if today_stat["burden"] else INFO,
            footer=cfg.BRAND_NAME,
        )
        e.add_field(name="注文", value=f"{today_stat['orders']} 件", inline=True)
        e.add_field(name="利用者負担", value=yen(today_stat["paid"]), inline=True)
        e.add_field(
            name="持ち出し",
            value=f"**{yen(today_stat['burden'])}**\n前日比 {sign}{diff:,} 円",
            inline=True,
        )
        e.add_field(name="定価合計", value=yen(today_stat["face"]), inline=True)
        e.add_field(
            name="チャージ",
            value=f"{today_stat['charges']} 件 / {yen(today_stat['charged_total'])}",
            inline=True,
        )
        e.add_field(name="失敗した注文", value=f"{today_stat['failed']} 件", inline=True)
        e.add_field(
            name="今月の累計",
            value=(
                f"{totals['month']['count']} 件 ・ "
                f"持ち出し **{yen(totals['month']['burden'])}**\n"
                f"このペースだと月末で約 {yen(pace)}"
            ),
            inline=False,
        )
        e.add_field(
            name="預かり残高", value=yen(totals["held_balance"]), inline=True
        )
        if totals["pending_reports"] or totals["open_flags"]:
            e.add_field(
                name="対応待ち",
                value=(
                    f"実績の承認 {totals['pending_reports']} 件 / "
                    f"不正フラグ {totals['open_flags']} 件"
                ),
                inline=True,
            )

        for owner_id in self.bot.owner_ids or []:
            await dm(self.bot, owner_id, e=e)
        await self.bot.send_log(cfg.LOG_ADMIN_CHANNEL_ID, e)

    async def _maybe_dormant_notice(self) -> None:
        """長く動きのない残高を本人に知らせる。"""
        cfg = self.bot.cfg
        days = int(cfg.DORMANT_DAYS)
        if days <= 0:
            return
        now = now_jst()
        if now.hour != int(cfg.DAILY_SUMMARY_HOUR):
            return
        tag = now.strftime("%Y-%m-%d")
        if await asyncio.to_thread(self.bot.store.get_kv, LAST_DORMANT_KEY, "") == tag:
            return
        await asyncio.to_thread(self.bot.store.set_kv, LAST_DORMANT_KEY, tag)

        cutoff = now - timedelta(days=days)
        rows = await asyncio.to_thread(
            self.bot.store.dormant_users, cutoff, int(cfg.DORMANT_MIN_BALANCE)
        )
        if not rows:
            return

        notified = 0
        for item in rows:
            sent = await dm(
                self.bot, item["user_id"],
                e=embed(
                    f"{cfg.E_MONEY} 残高が残っています",
                    f"**{yen(item['balance'])}** をお預かりしたままです。\n"
                    f"最後のご利用から {days} 日以上経っています。\n\n"
                    "チャージした残高は返金できませんので、ご利用ください。",
                    WARN,
                ),
            )
            if sent:
                notified += 1
            await asyncio.sleep(0.5)

        total = sum(i["balance"] for i in rows)
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_MONEY} 長期未使用の残高",
                f"{len(rows)} 人 / 合計 {yen(total)}（{notified} 人に通知）",
                WARN,
            ),
        )

    async def _maybe_monthly_report(self) -> None:
        """機能13: 月次レポートを DM で送る。"""
        cfg = self.bot.cfg
        now = now_jst()
        if now.day != cfg.MONTHLY_REPORT_DAY or now.hour != cfg.MONTHLY_REPORT_HOUR:
            return

        tag = now.strftime("%Y-%m")
        last = await asyncio.to_thread(self.bot.store.get_kv, LAST_MONTHLY_KEY, "")
        if last == tag:
            return
        await asyncio.to_thread(self.bot.store.set_kv, LAST_MONTHLY_KEY, tag)

        target = now - timedelta(days=1)  # 前月
        year, month = target.year, target.month
        await self.send_monthly_reports(year, month)

    async def send_monthly_reports(self, year: int, month: int) -> None:
        cfg = self.bot.cfg
        user_ids = await asyncio.to_thread(self.bot.store.active_user_ids)
        sent = 0
        totals = {"count": 0, "face": 0, "paid": 0}

        for uid in user_ids:
            orders = await asyncio.to_thread(self.bot.store.monthly_orders, uid, year, month)
            if not orders:
                continue

            count = len(orders)
            face = sum(int(o["face_amount"]) for o in orders)
            paid = sum(int(o["paid_amount"]) for o in orders)
            totals["count"] += count
            totals["face"] += face
            totals["paid"] += paid

            row = await asyncio.to_thread(self.bot.store.get_user, uid)
            balance = int(row["balance"]) if row else 0
            reports = await asyncio.to_thread(
                self.bot.store.count_reports_since, uid, datetime(year, month, 1, tzinfo=JST)
            )

            e = embed(
                f"{cfg.E_CHART} {year}年{month}月 のご利用レポート",
                None,
                MONEY,
                footer=cfg.BRAND_NAME,
            )
            e.add_field(name="注文回数", value=f"{count} 回", inline=True)
            e.add_field(name="お支払い合計", value=yen(paid), inline=True)
            e.add_field(name="現在の残高", value=yen(balance), inline=True)
            e.add_field(name="定価の合計", value=yen(face), inline=True)
            e.add_field(name="割引された額", value=f"**{yen(face - paid)}**", inline=True)
            e.add_field(name="投稿した感想", value=f"{reports} 件", inline=True)

            top = {}
            for order in orders:
                key = order["store_name"] or order["store_id"] or "-"
                top[key] = top.get(key, 0) + 1
            if top:
                best = max(top.items(), key=lambda kv: kv[1])
                e.add_field(name="よく使った店舗", value=f"{best[0]}（{best[1]} 回）", inline=False)

            if await dm(self.bot, uid, e=e):
                sent += 1
            await asyncio.sleep(0.5)  # レート制限を踏まないよう間隔を空ける

        log.info("月次レポートを %s 人に送信しました (%s-%s)", sent, year, month)
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_CHART} 月次レポートを送信しました",
                f"{year}年{month}月 ・ 送信 {sent} 人\n"
                f"注文 {totals['count']} 件 ・ 定価 {yen(totals['face'])} ・ "
                f"利用者負担 {yen(totals['paid'])} ・ "
                f"**持ち出し {yen(totals['face'] - totals['paid'])}**",
                INFO,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tasks(bot))
