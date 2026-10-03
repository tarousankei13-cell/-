"""設定コマンド（管理者用）"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

import config
import emoji as E
from core import settings
from db.models import SubsidyRule
from db.session import session_scope
from services.mcd import store_index
from cogs._checks import admin_only, handle_check_failure, owner_only
from ui import embeds

log = logging.getLogger("bot.cogs.config")


class ConfigCog(commands.Cog):
    """負担率・チャンネル・各種設定"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(name="config", description="BOTの設定（管理者用）")
    subsidy_group = app_commands.Group(name="subsidy", description="負担率の設定", parent=group)
    channel_group = app_commands.Group(name="channel", description="チャンネルの設定", parent=group)
    menu_group = app_commands.Group(name="menu", description="メニュー同期の設定", parent=group)
    store_group = app_commands.Group(name="store", description="店舗一覧の設定", parent=group)

    # -- 一覧 ---------------------------------------------------

    @group.command(name="show", description="現在の設定を一覧表示します")
    @admin_only()
    async def show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        v = settings.all_values()

        def ch(key: str) -> str:
            cid = v.get(key)
            return f"<#{cid}>" if cid else "（未設定）"

        e = discord.Embed(title=f"{E.GEAR} 現在の設定", color=embeds.BLUE)
        g = float(v.get("subsidy_rate", 40))
        e.add_field(
            name=f"{E.YEN} 負担率（全体）",
            value=f"管理者負担 **{g:g}%** → 利用者の支払い **{100 - g:g}%**",
            inline=False,
        )
        e.add_field(
            name=f"{E.CHARGE} チャージ",
            value=f"{embeds.yen(int(v.get('charge_min', 100)))} 〜 {embeds.yen(int(v.get('charge_max', 50000)))}",
            inline=True,
        )
        cap = v.get("monthly_subsidy_cap")
        e.add_field(
            name=f"{E.CHART} 月間負担上限",
            value=embeds.yen(int(cap)) if cap else "無制限",
            inline=True,
        )
        omax = v.get("order_max_amount")
        e.add_field(
            name=f"{E.BURGER} 1注文の上限",
            value=embeds.yen(int(omax)) if omax else "無制限",
            inline=True,
        )
        e.add_field(
            name="チャンネル",
            value=(
                f"実績　　{ch('channel_achievement')}\n"
                f"管理通知　{ch('channel_admin')}\n"
                f"チャージ　{ch('channel_charge')}"
            ),
            inline=False,
        )
        e.add_field(
            name="その他",
            value=(
                f"注文方式　　　{embeds.ORDER_MODE_LABEL.get(v.get('order_mode', 'both'), '?')}\n"
                f"メンテナンス　{'ON' if v.get('maintenance') else 'OFF'}\n"
                f"感想ゲート　　{'ON' if v.get('feedback_gate') else 'OFF'}\n"
                f"メニュー同期　{int(v.get('menu_sync_interval_minutes', 15))} 分ごと\n"
                f"差分通知　　　{'ON' if v.get('menu_notify_diff') else 'OFF'}"
            ),
            inline=False,
        )
        fields = v.get("achievement_fields", [])
        e.add_field(name="実績パネルの表示項目", value="`" + "`, `".join(fields) + "`" if fields else "（なし）", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    # -- 負担率 -------------------------------------------------

    @subsidy_group.command(name="global", description="全体の管理者負担率(%)を設定します")
    @app_commands.describe(rate="管理者が負担する割合(%)。40 なら利用者は60%支払う")
    @admin_only()
    async def subsidy_global(
        self, interaction: discord.Interaction,
        rate: app_commands.Range[float, 0.0, 100.0],
    ) -> None:
        await settings.set_value("subsidy_rate", float(rate), updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"全体の負担率を **{rate:g}%** に設定しました。\n"
                f"利用者の支払いは **{100 - rate:g}%** になります。"
            ),
            ephemeral=True,
        )

    @subsidy_group.command(name="role", description="ロール別の負担率を設定します")
    @app_commands.describe(
        role="対象のロール", rate="管理者が負担する割合(%)",
        priority="小さいほど優先（既定 50）", monthly_cap="月間の負担上限(円)。0で無制限",
    )
    @admin_only()
    async def subsidy_role(
        self, interaction: discord.Interaction, role: discord.Role,
        rate: app_commands.Range[float, 0.0, 100.0],
        priority: int = 50, monthly_cap: int = 0,
    ) -> None:
        async with session_scope() as s:
            existing = (
                await s.execute(
                    select(SubsidyRule).where(
                        SubsidyRule.scope == "role", SubsidyRule.target_id == role.id
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.subsidy_rate = rate
                existing.priority = priority
                existing.monthly_cap = monthly_cap or None
                existing.enabled = True
                rule_id = existing.id
            else:
                rule = SubsidyRule(
                    scope="role", target_id=role.id, subsidy_rate=rate,
                    priority=priority, monthly_cap=monthly_cap or None,
                )
                s.add(rule)
                await s.flush()
                rule_id = rule.id
        cap = f"／月上限 {embeds.yen(monthly_cap)}" if monthly_cap else ""
        await interaction.response.send_message(
            embed=embeds.ok(
                f"`#{rule_id}` {role.mention} の負担率を **{rate:g}%** に設定しました"
                f"（利用者の支払い {100 - rate:g}%{cap}）。"
            ),
            ephemeral=True,
        )

    @subsidy_group.command(name="user", description="利用者別の負担率を設定します（最優先）")
    @app_commands.describe(user="対象の利用者", rate="管理者が負担する割合(%)", monthly_cap="月間の負担上限(円)。0で無制限")
    @admin_only()
    async def subsidy_user(
        self, interaction: discord.Interaction, user: discord.User,
        rate: app_commands.Range[float, 0.0, 100.0], monthly_cap: int = 0,
    ) -> None:
        async with session_scope() as s:
            existing = (
                await s.execute(
                    select(SubsidyRule).where(
                        SubsidyRule.scope == "user", SubsidyRule.target_id == user.id
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.subsidy_rate = rate
                existing.monthly_cap = monthly_cap or None
                existing.enabled = True
                rule_id = existing.id
            else:
                rule = SubsidyRule(
                    scope="user", target_id=user.id, subsidy_rate=rate,
                    priority=1, monthly_cap=monthly_cap or None,
                )
                s.add(rule)
                await s.flush()
                rule_id = rule.id
        await interaction.response.send_message(
            embed=embeds.ok(
                f"`#{rule_id}` {user.mention} の負担率を **{rate:g}%** に設定しました。"
            ),
            ephemeral=True,
        )

    @subsidy_group.command(name="remove", description="負担率ルールを削除します")
    @app_commands.describe(rule_id="ルールID（/config subsidy list で確認）")
    @admin_only()
    async def subsidy_remove(self, interaction: discord.Interaction, rule_id: int) -> None:
        async with session_scope() as s:
            rule = await s.get(SubsidyRule, rule_id)
            if rule is None:
                await interaction.response.send_message(
                    embed=embeds.error(f"ルール `#{rule_id}` が見つかりません。"), ephemeral=True
                )
                return
            await s.delete(rule)
        await interaction.response.send_message(
            embed=embeds.ok(f"ルール `#{rule_id}` を削除しました。"), ephemeral=True
        )

    @subsidy_group.command(name="list", description="負担率ルールの一覧")
    @admin_only()
    async def subsidy_list(self, interaction: discord.Interaction) -> None:
        from ui import admin_flows

        await admin_flows.show_subsidy(interaction)

    # -- チャンネル ---------------------------------------------

    @channel_group.command(name="achievement", description="実績を送るチャンネルを設定します")
    @admin_only()
    async def ch_achievement(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await settings.set_value("channel_achievement", channel.id, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(f"実績チャンネルを {channel.mention} に設定しました。"), ephemeral=True
        )

    @channel_group.command(name="admin", description="管理者通知を送るチャンネルを設定します")
    @admin_only()
    async def ch_admin(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await settings.set_value("channel_admin", channel.id, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(f"管理者通知チャンネルを {channel.mention} に設定しました。"), ephemeral=True
        )

    # -- その他 -------------------------------------------------

    @group.command(name="charge_limit", description="チャージ額の下限・上限を設定します")
    @app_commands.describe(minimum="1回の下限(円)", maximum="1回の上限(円)")
    @admin_only()
    async def charge_limit(self, interaction: discord.Interaction, minimum: int, maximum: int) -> None:
        if minimum < 1 or maximum < minimum:
            await interaction.response.send_message(
                embed=embeds.error("下限は1円以上、上限は下限以上で指定してください。"), ephemeral=True
            )
            return
        await settings.set_value("charge_min", minimum, updated_by=interaction.user.id)
        await settings.set_value("charge_max", maximum, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"チャージ額を {embeds.yen(minimum)} 〜 {embeds.yen(maximum)} に設定しました。"
            ),
            ephemeral=True,
        )

    @group.command(name="order_limit", description="1注文あたりの上限金額を設定します")
    @app_commands.describe(maximum="上限(円)。0で無制限")
    @admin_only()
    async def order_limit(self, interaction: discord.Interaction, maximum: int) -> None:
        await settings.set_value("order_max_amount", maximum or None, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"1注文の上限を {embeds.yen(maximum)} に設定しました。" if maximum
                else "1注文の上限を無制限にしました。"
            ),
            ephemeral=True,
        )

    @group.command(name="order_mode", description="注文の方式を切り替えます")
    @app_commands.describe(mode="利用者が使える注文方法")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="両方（注文コード・メニュー）", value="both"),
            app_commands.Choice(name="注文コード(HEX)のみ", value="hex"),
            app_commands.Choice(name="メニューから選ぶ方式のみ", value="menu"),
        ]
    )
    @admin_only()
    async def order_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        await settings.set_value("order_mode", mode.value, updated_by=interaction.user.id)
        detail = {
            "both": "利用者は「注文コードを貼る」と「メニューから選ぶ」を選べます。",
            "hex": "利用者は注文コード(HEX)を貼る方法だけで注文します。",
            "menu": (
                "利用者はメニューから選ぶ方法だけで注文します。\n"
                "「注文コードを作る」ボタンはパネルから消えます。"
            ),
        }[mode.value]
        await interaction.response.send_message(
            embed=embeds.ok(
                f"注文方式を **{mode.name}** にしました。\n{detail}\n\n"
                f"{E.WARN} 設置済みのパネルに反映するには "
                "`/panel refresh` を実行してください。"
            ),
            ephemeral=True,
        )

    @group.command(name="maintenance", description="メンテナンスモードを切り替えます")
    @app_commands.describe(enabled="ONにすると注文を一時停止します")
    @admin_only()
    async def maintenance(self, interaction: discord.Interaction, enabled: bool) -> None:
        await settings.set_value("maintenance", enabled, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"{E.MAINTENANCE} メンテナンスモードを **ON** にしました。注文を停止します。"
                if enabled else f"{E.OK} メンテナンスモードを **OFF** にしました。"
            ),
            ephemeral=True,
        )

    @group.command(name="feedback", description="感想ゲート機能を切り替えます")
    @app_commands.describe(enabled="ONにすると、感想を送るまで次回の注文ができなくなります")
    @admin_only()
    async def feedback(self, interaction: discord.Interaction, enabled: bool) -> None:
        await settings.set_value("feedback_gate", enabled, updated_by=interaction.user.id)
        note = (
            "\n\n" + E.WARN + " ONにする場合は、Discord Developer Portal で "
            "**Message Content Intent** を有効にし、main.py の Intents も変更してください。"
            if enabled else ""
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"感想ゲートを **{'ON' if enabled else 'OFF'}** にしました。{note}"),
            ephemeral=True,
        )

    @menu_group.command(name="interval", description="商品・時間帯・店舗の同期間隔を設定します")
    @app_commands.describe(minutes="何分ごとに同期するか（5〜1440）")
    @admin_only()
    async def menu_interval(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 5, 1440]
    ) -> None:
        await settings.set_value(
            "menu_sync_interval_minutes", int(minutes), updated_by=interaction.user.id
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                f"商品・提供時間帯・店舗情報の同期を **{minutes}分ごと** に設定しました。\n"
                f"{E.INFO} 変更が無ければ通信は発生しないため、短くしても負荷はほとんど増えません。"
            ),
            ephemeral=True,
        )

    @menu_group.command(name="store_refresh", description="店舗情報を取り直す間隔を設定します")
    @app_commands.describe(minutes="何分ごとに取り直すか（5〜1440）")
    @admin_only()
    async def store_refresh(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 5, 1440]
    ) -> None:
        await settings.set_value(
            "store_refresh_minutes", int(minutes), updated_by=interaction.user.id
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"店舗情報の再取得を **{minutes}分ごと** に設定しました。"),
            ephemeral=True,
        )

    @menu_group.command(name="notify", description="メニュー差分の通知を切り替えます")
    @app_commands.describe(enabled="新商品・値上げを管理者チャンネルへ通知するか")
    @admin_only()
    async def menu_notify(self, interaction: discord.Interaction, enabled: bool) -> None:
        await settings.set_value("menu_notify_diff", enabled, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(f"メニュー差分の通知を **{'ON' if enabled else 'OFF'}** にしました。"),
            ephemeral=True,
        )

    # -- 店舗一覧の同期 -----------------------------------------

    @store_group.command(name="interval", description="店舗一覧を巡回更新する間隔を設定します")
    @app_commands.describe(minutes="何分ごとに巡回するか（5〜1440）")
    @admin_only()
    async def store_interval(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 5, 1440]
    ) -> None:
        await settings.set_value(
            "store_index_sync_minutes", int(minutes), updated_by=interaction.user.id
        )
        batch = config.STORE_INDEX_REFRESH_BATCH
        total = store_index.count() or 3036
        cycle = (total / max(batch, 1)) * int(minutes) / 60
        await interaction.response.send_message(
            embed=embeds.ok(
                f"店舗一覧の巡回更新を **{minutes}分ごと** に設定しました。\n"
                f"1回に {batch:,} 店舗を取り直すので、全 {total:,} 店舗を"
                f"**約{cycle:.1f}時間**で一周します。\n"
                f"{E.INFO} 変化が無ければ通信量は0バイトです（ETag）。"
            ),
            ephemeral=True,
        )

    @store_group.command(name="sitemap", description="店舗IDを照合する間隔を設定します")
    @app_commands.describe(minutes="何分ごとに照合するか（30〜1440）")
    @admin_only()
    async def store_sitemap(
        self, interaction: discord.Interaction, minutes: app_commands.Range[int, 30, 1440]
    ) -> None:
        """
        新しい店舗の開店・閉店を調べる間隔。

        この照合だけは相手が ETag を返さないため、毎回0.5MBほど
        受け取ることになる。開店・閉店はそう頻繁では無いので、
        通信量が気になる場合は長めにしてよい。
        """
        await settings.set_value(
            "store_sitemap_check_minutes", int(minutes),
            updated_by=interaction.user.id, actor_name=str(interaction.user),
        )
        per_day = 24 * 60 / int(minutes) * 0.5
        await interaction.response.send_message(
            embed=embeds.ok(
                f"店舗IDの照合を **{minutes}分ごと** に設定しました。\n"
                f"{E.INFO} 1日あたりおよそ **{per_day:.0f}MB** を受け取ります。\n"
                "（商品や営業時間の同期はETagが効くので、これとは別です）"
            ),
            ephemeral=True,
        )

    @store_group.command(name="notify", description="店舗一覧の変化の通知を切り替えます")
    @app_commands.describe(enabled="開店・閉店・店名変更を管理者チャンネルへ通知するか")
    @admin_only()
    async def store_notify(self, interaction: discord.Interaction, enabled: bool) -> None:
        await settings.set_value("store_notify_diff", enabled, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"店舗一覧の変化の通知を **{'ON' if enabled else 'OFF'}** にしました。"
            ),
            ephemeral=True,
        )

    @group.command(name="receipt_url", description="完了DMの「受け取り画面」リンクを設定します")
    @app_commands.describe(
        url="リンク先のURL。off と入れるとボタンを出しません（既定に戻すなら default）"
    )
    @admin_only()
    async def receipt_url(self, interaction: discord.Interaction, url: str) -> None:
        """
        完了DMに出る「受け取り画面を開く」ボタンのリンク先。

        これは外部サイトへの飾りのリンクで、BOTの動作には関わらない。
        注文・メニュー同期・店舗同期はマクドナルド公式のAPIだけで完結する。
        店頭で必要な注文番号はBOTが作るレシート画像に入っている。
        """
        from services import receipt as receipt_svc

        value = url.strip()
        if value.lower() in ("off", "なし", "無効", "disable"):
            await settings.set_value("receipt_view_url", "", updated_by=interaction.user.id)
            await interaction.response.send_message(
                embed=embeds.ok(
                    "「受け取り画面を開く」ボタンを**表示しない**ようにしました。\n"
                    f"{E.INFO} 注文番号はレシート画像に入っているので、受け取りには影響しません。"
                ),
                ephemeral=True,
            )
            return

        if value.lower() in ("default", "既定", "デフォルト"):
            value = config.RECEIPT_VIEW_URL

        if not value.startswith("https://"):
            await interaction.response.send_message(
                embed=embeds.error("URLは https:// で始めてください。"), ephemeral=True
            )
            return
        try:
            value.format(store_id="13934", receipt_number="0000")
        except (KeyError, IndexError, ValueError):
            await interaction.response.send_message(
                embed=embeds.error(
                    "URLの書式が正しくありません。\n"
                    "`{store_id}` と `{receipt_number}` を差し込み位置に入れてください。"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await settings.set_value("receipt_view_url", value, updated_by=interaction.user.id)
        alive = await receipt_svc.check_link_alive(force=True)
        await interaction.followup.send(
            embed=embeds.ok(
                f"受け取り画面のリンクを設定しました。\n```{value}```\n"
                + (
                    f"{E.OK} リンク先に接続できました。"
                    if alive
                    else f"{E.WARN} リンク先に接続できなかったため、"
                         "ボタンは自動的に非表示になります。"
                )
            ),
            ephemeral=True,
        )

    @group.command(name="order_concurrency", description="同時に処理する注文の数を設定します")
    @app_commands.describe(count="同時に処理する数（1〜20）")
    @admin_only()
    async def order_concurrency(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 20]
    ) -> None:
        """
        大人数が一斉に注文すると、マクドナルド側から見て不自然な量の
        要求が短時間に集中する。同時に処理する数を絞ると、それを避けられる。
        """
        from core import queue as order_gate

        await settings.set_value(
            "order_concurrency", int(count),
            updated_by=interaction.user.id, actor_name=str(interaction.user),
        )
        order_gate.gate.set_limit(int(count))
        await interaction.response.send_message(
            embed=embeds.ok(
                f"同時に処理する注文を **{count}件** までにしました。\n"
                f"{E.INFO} 超えた分は順番にお待ちいただきます。\n"
                f"現在: {order_gate.gate.describe()}"
            ),
            ephemeral=True,
        )

    @group.command(name="fraud", description="気になる動きの検知を設定します")
    @app_commands.describe(
        enabled="検知を有効にするか",
        burst_count="この回数を超えて短時間に注文したら知らせる（既定5）",
        big_order="この金額以上の注文を知らせる（既定10000円）",
    )
    @admin_only()
    async def fraud_config(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        burst_count: app_commands.Range[int, 2, 100] | None = None,
        big_order: app_commands.Range[int, 1000, 1000000] | None = None,
    ) -> None:
        """
        気になる動きの検知。

        ⚠️ これは**自動で止める仕組みではありません**。
           ふつうに使っている人を誤って止めるほうが痛いので、
           管理者に知らせるところまでにしてあります。
        """
        changed = []
        if enabled is not None:
            for key in ("fraud_burst", "fraud_big_order", "fraud_quick_spend",
                        "fraud_heavy", "fraud_reused_link", "fraud_scan"):
                await settings.set_value(
                    key, enabled, updated_by=interaction.user.id, audit=False
                )
            await settings.set_value(
                "fraud_enabled", enabled,
                updated_by=interaction.user.id, actor_name=str(interaction.user),
            )
            changed.append(f"検知を **{'ON' if enabled else 'OFF'}**")
        if burst_count is not None:
            await settings.set_value(
                "fraud_burst_count", int(burst_count),
                updated_by=interaction.user.id, actor_name=str(interaction.user),
            )
            changed.append(f"短時間の注文 **{burst_count}回**")
        if big_order is not None:
            await settings.set_value(
                "fraud_big_order_amount", int(big_order),
                updated_by=interaction.user.id, actor_name=str(interaction.user),
            )
            changed.append(f"高額注文 **{big_order:,}円**")

        if not changed:
            e = discord.Embed(
                title=f"{E.WARN} 気になる動きの検知",
                description=(
                    "いまの設定です。変えるには項目を指定してください。\n\n"
                    f"短時間の注文　**{settings.get('fraud_burst_count', 5)}回** / "
                    f"{settings.get('fraud_burst_minutes', 10)}分\n"
                    f"高額注文　**{int(settings.get('fraud_big_order_amount', 10000)):,}円** 以上\n"
                    f"全体の点検　**{'ON' if settings.get('fraud_scan', True) else 'OFF'}**"
                ),
                color=embeds.BLUE,
            )
            e.set_footer(text="自動で止めることはありません。管理者へ知らせるだけです")
            await interaction.response.send_message(embed=e, ephemeral=True)
            return

        await interaction.response.send_message(
            embed=embeds.ok(
                "設定しました。\n" + "\n".join(f"・{c}" for c in changed)
                + f"\n\n{E.INFO} 自動で止めることはありません。管理者へ知らせるだけです。"
            ),
            ephemeral=True,
        )

    backup_group = app_commands.Group(
        name="backup", description="バックアップの設定", parent=group
    )

    @backup_group.command(name="where", description="バックアップの保存先を設定します")
    @app_commands.describe(place="保存先", keep="サーバー上に残す世代の数（1〜90）")
    @app_commands.choices(place=[
        app_commands.Choice(name="管理者チャンネル", value="channel"),
        app_commands.Choice(name="オーナーのDM", value="dm"),
        app_commands.Choice(name="サーバー上（data/backups）", value="local"),
        app_commands.Choice(name="すべて（おすすめ）", value="all"),
    ])
    @owner_only()
    async def backup_where(
        self,
        interaction: discord.Interaction,
        place: app_commands.Choice[str] | None = None,
        keep: app_commands.Range[int, 1, 90] | None = None,
    ) -> None:
        """
        控えの保存先。

        1か所しか無いと、そこが消えたときに復旧できなくなる。
        「すべて」にしておくのが安全。
        """
        from services import backup as backup_svc

        changed = []
        if place is not None:
            await settings.set_value(
                "backup_where", place.value,
                updated_by=interaction.user.id, actor_name=str(interaction.user),
            )
            changed.append(f"保存先 **{place.name}**")
        if keep is not None:
            await settings.set_value(
                "backup_keep", int(keep),
                updated_by=interaction.user.id, actor_name=str(interaction.user),
            )
            changed.append(f"残す世代 **{keep}個**")

        current = settings.get("backup_where", "channel")
        local = backup_svc.list_local()
        e = discord.Embed(
            title=f"{E.NOTE} バックアップの保存先",
            description=(
                ("設定しました。\n" + "\n".join(f"・{c}" for c in changed) + "\n\n")
                if changed else ""
            ) + f"いまの保存先: **{current}**",
            color=embeds.GREEN if changed else embeds.BLUE,
        )
        if local:
            newest = local[0]
            e.add_field(
                name="サーバー上の控え",
                value=(
                    f"**{len(local)}** 個\n"
                    f"最新 `{newest[0]}`（{newest[1] / 1024:.0f} KB）\n"
                    f"<t:{int(newest[2])}:R>"
                ),
                inline=False,
            )
        e.set_footer(text="1か所しか無いと、そこが消えると復旧できません")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @backup_group.command(name="now", description="いますぐバックアップを取ります")
    @owner_only()
    async def backup_now(self, interaction: discord.Interaction) -> None:
        from cogs.tasks import save_backup
        from services import tasks as jobs

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            name, data = await jobs.make_backup()
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error(f"作成できませんでした。\n```{e}```"), ephemeral=True
            )
            return
        result = await save_backup(interaction.client, name, data)
        lines = [f"`{name}`（{len(data) / 1024:.0f} KB）"]
        if result.where:
            lines.append(f"{E.OK} 保存先: " + " / ".join(result.where))
        for err in result.errors:
            lines.append(f"{E.WARN} {err}")
        await interaction.followup.send(
            embed=(embeds.ok if result.ok else embeds.error)("\n".join(lines)),
            ephemeral=True,
        )

    @group.command(name="achievement_fields", description="実績パネルの表示項目を設定します")
    @app_commands.describe(
        anon_code="匿名コード", list_price="定価", subsidy_rate="負担率",
        user_amount="お支払い額", daily_count="本日の通算",
        username="ユーザー名（プライバシーに注意）", store_name="店舗名（居場所が分かります）",
        receipt_number="注文番号", pickup_method="受取方法",
    )
    @admin_only()
    async def achievement_fields(
        self, interaction: discord.Interaction,
        anon_code: bool = True, list_price: bool = True, subsidy_rate: bool = True,
        user_amount: bool = True, daily_count: bool = True,
        username: bool = False, store_name: bool = False,
        receipt_number: bool = False, pickup_method: bool = False,
    ) -> None:
        chosen = [
            k for k, v in {
                "anon_code": anon_code, "list_price": list_price,
                "subsidy_rate": subsidy_rate, "user_amount": user_amount,
                "daily_count": daily_count, "username": username,
                "store_name": store_name, "receipt_number": receipt_number,
                "pickup_label": pickup_method,
            }.items() if v
        ]
        await settings.set_value("achievement_fields", chosen, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok("実績パネルの表示項目を更新しました。\n`" + "`, `".join(chosen) + "`"),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("config コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ConfigCog(bot))
