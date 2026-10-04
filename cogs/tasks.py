"""定期タスク"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import emoji as E
from core import saga, settings
from core.telemetry import traced
from db.session import session_scope
from services import tasks as jobs
from services import monitor
from services.mcd import store_sync
from ui import embeds

log = logging.getLogger("bot.cogs.tasks")


async def save_backup(bot, name: str, data: bytes):
    """
    控えを設定された保存先へ置く。

    どれか1つでも成功すれば ok。全部失敗したときだけ騒ぐ。
    """
    import io

    from services import backup as backup_svc

    where = str(settings.get("backup_where", "channel")).lower()
    targets = (
        ["channel", "dm", "local"] if where == "all" else [where]
    )
    saved: list[str] = []
    errors: list[str] = []

    note = (
        f"{E.OK} 定期バックアップ（{len(data) / 1024:.0f} KB）\n"
        f"{E.INFO} 残高・注文履歴はこのファイルだけで復元できます。\n"
        f"　　登録済みアカウントも戻すには、サーバーの "
        f"`data/encryption_key.txt` も必要です"
    )

    for target in targets:
        try:
            if target == "channel":
                channel_id = settings.get("channel_admin")
                channel = bot.get_channel(int(channel_id)) if channel_id else None
                if channel is None:
                    errors.append("管理者チャンネルが設定されていません")
                    continue
                await channel.send(
                    content=note, file=discord.File(io.BytesIO(data), filename=name)
                )
                saved.append("管理者チャンネル")

            elif target == "dm":
                owners = list(getattr(bot, "owner_ids", None) or [])
                if not owners:
                    errors.append("オーナーが設定されていません")
                    continue
                delivered = 0
                for uid in owners:
                    user = bot.get_user(int(uid))
                    if user is None:
                        try:
                            user = await bot.fetch_user(int(uid))
                        except Exception:
                            continue
                    try:
                        await user.send(
                            content=note,
                            file=discord.File(io.BytesIO(data), filename=name),
                        )
                        delivered += 1
                    except discord.HTTPException:
                        continue
                if delivered:
                    saved.append(f"オーナーのDM（{delivered}人）")
                else:
                    errors.append("オーナーのDMへ送れませんでした")

            elif target == "local":
                path = await asyncio.to_thread(backup_svc.save_local, name, data)
                saved.append(f"サーバー上（{path.parent.name}/）")

            else:
                errors.append(f"知らない保存先です: {target}")
        except Exception as e:
            errors.append(f"{target}: {e}")

    return backup_svc.Saved(
        name=name, size=len(data), where=saved, errors=errors
    )


class TasksCog(commands.Cog):
    """メニュー同期・健全性チェック・バックアップなど"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_month: int | None = None
        self._last_daily_reset_day: int | None = None
        self._last_menu_force_date = None
        self._last_backup_day: int | None = None
        self._started = False

    def _loops(self) -> list[tasks.Loop]:
        """
        このコグが持っている定期ループを全部拾う。

        ⚠️ 手で並べてはいけない。足したループを起動し忘れると、
           その機能だけ**黙って定期更新されなくなる**。
           気付くのは利用者が古い情報で注文に失敗したときになる。
        """
        return [
            getattr(self, name)
            for name, value in vars(type(self)).items()
            if isinstance(value, tasks.Loop)
        ]

    def _start_loops(self) -> None:
        """
        ループの開始は on_ready 以降に行う。

        cog_load の時点ではまだログインしていないため、ループ内の
        wait_until_ready() が例外を投げてループが静かに死ぬことがある。
        """
        for loop in self._loops():
            if not loop.is_running():
                loop.start()

    async def cog_unload(self) -> None:
        for loop in self._loops():
            loop.cancel()

    async def _wait_ready(self) -> None:
        """準備完了を待つ。まだ接続していない場合でもループを殺さない。"""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            pass

    # -- 通知 ---------------------------------------------------

    async def notify_admin(
        self, embed: discord.Embed, *, kind: str = "admin"
    ) -> None:
        """
        お知らせを送る。

        kind で宛先を分けられる。店舗やメニューの更新は件数が多いので、
        管理者チャンネルに混ぜると本当に対応が要るものが埋もれる。
        専用のチャンネルを決めていなければ管理者チャンネルへ送る。
        """
        channel_id = settings.get(f"channel_{kind}") or settings.get("channel_admin")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("お知らせの送信に失敗しました（%s）", kind)

    # -- メニュー同期 -------------------------------------------

    @tasks.loop(minutes=config.MENU_SYNC_INTERVAL_MINUTES)
    @traced("メニュー同期")
    async def menu_sync(self) -> None:
        """
        商品・価格・提供時間帯を取り込む。

        ETag を使うので、変更が無ければ 304 が返り通信量はゼロ。
        そのため短い間隔で回しても負荷が小さい。
        """
        if not self._started:
            return
        interval = int(
            settings.get("menu_sync_interval_minutes", config.MENU_SYNC_INTERVAL_MINUTES)
        )
        if self.menu_sync.minutes != interval:
            self.menu_sync.change_interval(minutes=interval)

        # 提供時間帯は日付ごとに定義されているため、日付が変わったら
        # ETag を無視して必ず取り直す。
        # 提供時間帯は日付ごとに定義されているので、**日本の**日付が
        # 変わったら取り直す（UTCの日付だと切り替わりが朝9時になる）
        today = config.now_jst().date()
        force = self._last_menu_force_date != today
        if force:
            self._last_menu_force_date = today
            log.info("日付が変わったため、メニューを強制的に取り直します")

        diffs = await jobs.sync_all_menus(force=force)
        if not settings.get("menu_notify_diff", True):
            return

        from db.models import StoreCache

        blocks = []
        async with session_scope() as s:
            for store_id, diff in diffs.items():
                row = await s.get(StoreCache, store_id)
                text = jobs.format_menu_diff(store_id, row.store_name if row else "", diff)
                if text:
                    blocks.append(text)
        if blocks:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.CHART} メニューが更新されました",
                    description="\n\n".join(blocks)[:4000],
                    color=embeds.BLUE,
                ),
                kind="menu_updates",
            )

        # ⚠️ 利用者向けは別物。店舗ごとではなく全店まとめて、
        #    「何が増えた・値段が変わった・終わった」だけを伝える。
        await self._tell_menu_news(diffs)

    async def _tell_menu_news(self, diffs: dict) -> None:
        """価格改定や新商品を、利用者チャンネルへお知らせする。"""
        channel_id = settings.get("channel_menu_news")
        if not channel_id:
            return
        text = jobs.format_menu_news(diffs)
        if not text:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(
                embed=discord.Embed(
                    title=f"{E.BURGER} メニューが変わりました",
                    description=text[:4000],
                    color=embeds.GREEN,
                    timestamp=config.now_jst(),
                )
            )
        except discord.HTTPException:
            log.exception("メニューのお知らせを投稿できませんでした")

    @menu_sync.before_loop
    async def before_menu_sync(self) -> None:
        await self._wait_ready()

    # -- 店舗一覧の同期 -----------------------------------------

    @tasks.loop(minutes=config.STORE_INDEX_SYNC_MINUTES)
    @traced("店舗同期")
    async def store_index_sync(self) -> None:
        """
        店舗一覧を最新に保つ。

        新店舗の開店・閉店・店名変更・モバイルオーダー対応の切り替えを
        自動で反映する。各店舗は ETag を使って取り直すので、
        変わっていなければ 304（0バイト）で済む。
        """
        if not self._started:
            return
        interval = int(
            settings.get("store_index_sync_minutes", config.STORE_INDEX_SYNC_MINUTES)
        )
        if self.store_index_sync.minutes != interval:
            self.store_index_sync.change_interval(minutes=interval)

        try:
            report = await store_sync.sync()
        except Exception:
            log.exception("店舗一覧の同期に失敗しました")
            return

        if report.error:
            log.warning("店舗一覧の同期: %s", report.error)
        if not settings.get("store_notify_diff", True):
            return
        text = store_sync.format_report(report)
        if text:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.STORE} 店舗一覧が更新されました",
                    description=text[:4000],
                    color=embeds.BLUE,
                ),
                kind="store_updates",
            )

    @store_index_sync.before_loop
    async def before_store_index_sync(self) -> None:
        await self._wait_ready()

    # -- 外形監視 -----------------------------------------------

    @tasks.loop(minutes=config.MONITOR_INTERVAL_MINUTES)
    @traced("外形監視")
    async def outage_watch(self) -> None:
        """
        マクドナルド側が落ちていないか、軽い読み取りで確かめる。

        利用者からの報告で気付くのでは遅い。
        状態が変わったときだけ通知する（毎回出すと本当の異常が埋もれる）。
        """
        if not self._started:
            return
        try:
            report = await monitor.check()
        except Exception:
            log.exception("外形監視に失敗しました")
            return

        text = monitor.format_report(report)
        if not text:
            return
        await self.notify_admin(
            discord.Embed(
                title=(
                    f"{E.NG} マクドナルドへ接続できません"
                    if report.became_down else f"{E.OK} 接続が復帰しました"
                ),
                description=text[:4000],
                color=embeds.RED if report.became_down else embeds.GREEN,
            )
        )

    @outage_watch.before_loop
    async def before_outage_watch(self) -> None:
        await self._wait_ready()

    # -- アカウントの健全性 -------------------------------------

    @tasks.loop(hours=config.MCD_HEALTHCHECK_INTERVAL_HOURS)
    async def health_check(self) -> None:
        if not self._started:
            return
        # ⚠️ マクドナルドだけでなく **Kyash も** 確かめる。
        #    Kyash のトークンは1ヶ月で切れ、凍結もありうる。
        #    チャージしようとした利用者が最初に気付く形になっていた。
        results = await jobs.mcd_accounts.healthcheck_all()
        dead = [(f"#{i}", l, "マクドナルド") for i, l, alive in results if not alive]
        for kind, mod in (("Kyash", "kyash"), ("PayPay", "paypay")):
            try:
                if kind == "Kyash":
                    got = await jobs.kyash_accounts.healthcheck_all()
                else:
                    from services.paypay import accounts as pp_accounts

                    got = await pp_accounts.healthcheck_all()
            except Exception as e:      # 片方で落ちても点検は続ける
                log.warning("%s口座の生存確認に失敗しました: %s", kind, e)
            else:
                dead += [(f"#{i}", l, kind) for i, l, alive in got if not alive]
        if dead:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.RED} 応答しないアカウントがあります",
                    description="\n".join(
                        f"`{i}` **{l}**（{kind}）" for i, l, kind in dead
                    ),
                    color=embeds.RED,
                )
            )

    @health_check.before_loop
    async def before_health_check(self) -> None:
        await self._wait_ready()

    # -- トークンの事前更新 -------------------------------------

    @tasks.loop(minutes=config.TOKEN_WARM_INTERVAL_MINUTES)
    async def token_warm(self) -> None:
        if not self._started:
            return
        warmed = await jobs.warm_tokens()
        log.debug("トークンを事前更新しました: %d件", warmed)

    @token_warm.before_loop
    async def before_token_warm(self) -> None:
        await self._wait_ready()

    # -- できあがり通知 -----------------------------------------

    @tasks.loop(seconds=config.READY_POLL_SECONDS)
    async def ready_watch(self) -> None:
        """
        注文のできあがりを見張る。

        ⚠️ 既定では無効。有効にすると、見張っている注文の数だけ
           マクドナルドへの問い合わせが増える。
        """
        if not self._started:
            return
        from services import order_watch

        if not order_watch.enabled():
            return
        interval = order_watch.poll_seconds()
        if self.ready_watch.seconds != interval:
            self.ready_watch.change_interval(seconds=interval)
        try:
            done = await order_watch.sweep()
        except Exception:
            log.exception("できあがりの確認に失敗しました")
            return
        for ready in done:
            await self._tell_ready(ready)

    async def _tell_ready(self, ready) -> None:
        """できあがりを本人へDMで知らせる。届かなくても構わない。"""
        try:
            user = self.bot.get_user(ready.discord_id) or await self.bot.fetch_user(
                ready.discord_id
            )
            e = discord.Embed(
                title=f"{E.BELL} ご注文の品ができあがりました",
                color=embeds.GREEN,
            )
            if ready.receipt_number:
                e.add_field(
                    name=f"{E.RECEIPT} 注文番号",
                    value=f"**{ready.receipt_number}**", inline=True,
                )
            if ready.buzzer_number:
                e.add_field(
                    name=f"{E.BELL} 呼び出し番号",
                    value=f"**{ready.buzzer_number}**", inline=True,
                )
            if ready.store_name:
                e.add_field(name=f"{E.STORE} 店舗", value=ready.store_name, inline=True)
            e.set_footer(text="カウンターでお受け取りください")
            await user.send(embed=e)
        except (discord.HTTPException, AttributeError):
            log.info("できあがりをDMできませんでした（%s）", ready.discord_id)

    @ready_watch.before_loop
    async def before_ready_watch(self) -> None:
        await self._wait_ready()

    # -- 毎時の点検 ---------------------------------------------

    @tasks.loop(hours=1)
    async def hourly_checks(self) -> None:
        if not self._started:
            return
        # 日次リセット・バックアップ・通知の時刻はすべて日本時間で判断する
        # （UTCだと「0時にリセット」が日本の朝9時になってしまう）
        now = config.now_jst()

        # 元帳の整合性
        ok, message = await jobs.check_ledger()
        if not ok:
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.NG} 元帳の不整合を検出しました",
                    description=message[:4000],
                    color=embeds.RED,
                )
            )

        # 入金の照合（Kyash側の履歴と、こちらの記録を突き合わせる）
        # ⚠️ 読むだけ。見つけても自動では直さない。
        #    お金の帳尻を機械が勝手に合わせるのが一番危ない。
        if now.hour == 9:
            try:
                from services.kyash import reconcile

                rep = await reconcile.check(days=1)
            except Exception:
                log.exception("入金の照合に失敗しました")
            else:
                if not rep.clean or rep.errors:
                    lines = [rep.summary()]
                    for label, rows in (
                        ("記帳もれ（受け取ったのに残高に入っていない）", rep.missing_here),
                        ("入金が見つからない（記帳したのに履歴に無い）", rep.missing_there),
                        ("途中で止まっている", rep.stuck),
                    ):
                        if not rows:
                            continue
                        detail = "\n".join(
                            f"　口座#{r.get('account_id')} "
                            f"{embeds.yen(int(r.get('amount') or 0))}"
                            + (f"（{r.get('status')}）" if r.get("status") else "")
                            for r in rows[:5]
                        )
                        more = f"\n　ほか {len(rows) - 5} 件" if len(rows) > 5 else ""
                        lines.append(f"**{label}**\n{detail}{more}")
                    if rep.errors:
                        lines.append("**照合できなかった口座**\n　" + "\n　".join(rep.errors[:3]))
                    await self.notify_admin(
                        discord.Embed(
                            title=f"{E.WARN} 入金の照合で食い違いがありました",
                            description="\n\n".join(lines)[:4000],
                            color=embeds.ORANGE,
                        ),
                        kind="reconcile",
                    )

        # Kyash トークンの期限
        warnings = await jobs.kyash_token_warnings()
        if warnings and now.hour == 9:   # 日本時間の朝9時
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.KEY} Kyashトークンの期限が近づいています",
                    description="\n".join(warnings) + "\n\n`/kyash relogin <ID>` で更新できます。",
                    color=embeds.YELLOW,
                )
            )

        # 日次リセット（バックアップとは別のフラグで管理する）
        if now.hour == 0 and self._last_daily_reset_day != now.day:
            self._last_daily_reset_day = now.day
            await jobs.daily_reset()

        # 気になる動きがないか、全体を見渡す
        if settings.get("fraud_scan", True):
            try:
                from core import fraud

                report = await fraud.scan_all()
                text = fraud.format_report(report)
                if text:
                    await self.notify_admin(
                        discord.Embed(
                            title=f"{E.WARN} 気になる動きがあります",
                            description=text[:4000],
                            color=embeds.RED if report.worst == fraud.HIGH
                            else embeds.ORANGE,
                        )
                    )
            except Exception:
                log.exception("不正検知の点検に失敗しました")

        # 月次リセット
        self._last_month = await jobs.monthly_reset_if_needed(self._last_month)

        # バックアップ
        backup_hour = int(settings.get("backup_hour", 4))
        if (
            settings.get("backup_enabled", True)
            and now.hour == backup_hour
            and self._last_backup_day != now.day
        ):
            self._last_backup_day = now.day
            await self._send_backup()

    @hourly_checks.before_loop
    async def before_hourly(self) -> None:
        await self._wait_ready()

    # ------------------------------------------------------------
    #  サーバー管理の定期処理
    # ------------------------------------------------------------

    @tasks.loop(minutes=config.GUARD_COUNTER_MINUTES)
    async def server_upkeep(self) -> None:
        """
        チケット・認証・監視の、時間でやることをまとめて行う。

        ⚠️ 1つが失敗しても残りを続けること。
           まとめてあるぶん、1つの例外で全部止まると影響が大きい。
        """
        if not self._started:
            return

        # ① 放置されたチケットを閉じる
        try:
            from services.server import tickets

            await tickets.close_stale(self.bot)
        except Exception:
            log.warning("チケットの自動終了に失敗しました", exc_info=True)

        # ② 未認証のまま時間がたった方を退出させる
        try:
            await self._kick_unverified()
        except Exception:
            log.warning("未認証の方の整理に失敗しました", exc_info=True)

        # ③ メンバー数の表示を更新する
        #    ⚠️ チャンネル名は10分に2回までしか変えられない。
        #       このループの間隔（既定10分）がその制限に合わせてある。
        try:
            cog = self.bot.get_cog("GuardCog")
            if cog is not None and settings.get("guard_counter_channel"):
                await cog.update_counter()
        except Exception:
            log.warning("メンバー数の表示を更新できませんでした", exc_info=True)

        # ④ 検知のために覚えている分を捨てる（放っておくと増え続ける）
        try:
            from services.server import guard

            guard.detector.prune()
        except Exception:
            log.debug("検知の後片づけに失敗", exc_info=True)

    async def _kick_unverified(self) -> int:
        """
        認証しないまま時間がたった方を退出させる。

        ⚠️ **認証パネルが無いまま動かすと、入った人を順に追い出す。**
           そのため、ロールが設定されていないときは何もしない。
        """
        from services.server import verify

        hours = int(settings.get("verify_kick_hours", 0) or 0)
        if hours <= 0 or not verify.enabled() or verify.role_id() is None:
            return 0

        from datetime import datetime, timedelta, timezone

        cut = datetime.now(timezone.utc) - timedelta(hours=hours)
        done = 0
        for guild in self.bot.guilds:
            role = guild.get_role(verify.role_id())
            if role is None:
                continue
            for member in list(guild.members):
                if member.bot or role in member.roles:
                    continue
                joined = member.joined_at
                if joined is None or joined > cut:
                    continue
                if await verify.already(guild.id, member.id):
                    continue
                try:
                    from services.server import mod as modsvc

                    await modsvc.notify(
                        member, guild_name=guild.name,
                        action="自動退出",
                        reason=f"{hours}時間以内に認証が行われなかったため",
                        extra="もう一度ご参加のうえ、認証をお願いいたします。",
                    )
                    await member.kick(reason=f"未認証（{hours}時間経過）")
                    done += 1
                except discord.Forbidden:
                    log.warning(
                        "未認証の方を退出させられません（権限不足）: %s", guild.name,
                    )
                    break        # 権限が無いなら、このサーバーでは続けても同じ
                except discord.HTTPException:
                    continue
        if done:
            log.info("未認証のまま時間がたった %d 人を退出させました", done)
        return done

    @server_upkeep.before_loop
    async def before_server_upkeep(self) -> None:
        await self._wait_ready()

    async def _send_backup(self) -> None:
        """
        控えを保存する。

        保存先は設定で選べる（/config backup where）。
        1か所しか無いと、そこが消えたときに復旧できなくなる。
        """
        try:
            name, data = await jobs.make_backup()
        except Exception as e:
            log.warning("バックアップを作成できませんでした: %s", e)
            return

        result = await save_backup(self.bot, name, data)
        if result.ok:
            log.info(
                "バックアップを保存しました: %s（%s）", name, " / ".join(result.where)
            )
        if result.errors:
            log.warning("保存できなかった先があります: %s", " / ".join(result.errors))
            # 1か所も保存できなかったときは必ず知らせる
            if not result.where:
                await self.notify_admin(
                    discord.Embed(
                        title=f"{E.NG} バックアップを保存できませんでした",
                        description=(
                            "\n".join(f"・{e}" for e in result.errors)
                            + "\n\n`/config backup where` で保存先を確認してください。"
                        ),
                        color=embeds.RED,
                    )
                )

    # -- 起動時 -------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._started:
            return
        self._started = True

        await settings.load_all()
        # 設定してある上限を反映する
        from core import queue as order_gate

        order_gate.gate.set_limit(
            int(settings.get("order_concurrency", config.ORDER_CONCURRENCY))
        )
        self._start_loops()

        # 未完了の注文とチャージを復旧する
        try:
            results = await saga.recover_pending()
            if results:
                await self.notify_admin(
                    discord.Embed(
                        title=f"{E.SYNC} 未完了の注文を復旧しました",
                        description="\n".join(
                            f"`{r.order_id[:8]}` → {r.state}" for r in results
                        )[:4000],
                        color=embeds.ORANGE,
                    )
                )
        except Exception:
            log.exception("注文の復旧に失敗しました")

        try:
            from services.kyash.charge import recover_pending as recover_charges

            count = await recover_charges()
            if count:
                await self.notify_admin(
                    discord.Embed(
                        title=f"{E.WARN} 未完了のチャージがあります",
                        description=(
                            f"{count} 件を「要確認」にしました。\n"
                            "Kyashの受取履歴と照合してください。"
                        ),
                        color=embeds.ORANGE,
                    )
                )
        except Exception:
            log.exception("チャージの復旧に失敗しました")

        # レシート画像が作れるか確認
        from services.receipt import self_check

        err = self_check()
        if err:
            log.error("レシート画像の準備に問題があります: %s", err)
            await self.notify_admin(
                discord.Embed(
                    title=f"{E.WARN} レシート画像を生成できません",
                    description=err,
                    color=embeds.RED,
                )
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TasksCog(bot))
