"""設定コマンド（管理者用）"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

import emoji as E
from core import settings
from db.models import SubsidyRule
from db.session import session_scope
from cogs._checks import admin_only, handle_check_failure
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
