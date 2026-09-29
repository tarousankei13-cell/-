"""定期実行まわり。

* 呼び出し（バズー）番号の追跡
* 月次レポートの DM（機能13）
* トークンのヘルスチェックと期限警告
* パネルの混雑状況の更新（機能6）
* 期限切れの承認待ちの掃除
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

    async def cog_unload(self) -> None:
        self.health_loop.cancel()
        self.panel_loop.cancel()
        self.daily_loop.cancel()
        for task in list(self._buzzer_tasks):
            task.cancel()

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

    @tasks.loop(minutes=30)
    async def daily_loop(self) -> None:
        await self._cleanup_stale_approvals()
        await self._cleanup_stale_reservations()
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
