"""
利用を広げるための機能（管理者用）

  ・声かけ … ようこそ案内、カートの残り、残高のお知らせ
  ・紹介ランキング

⚠️ 声かけは **送りすぎが一番の失敗**。既定はOFFにしてあり、
   有効にしても「同じことは二度言わない」「本人が止められる」の
   2つが常に効いている。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import emoji as E
from core import settings
from services import outreach
from cogs._checks import admin_only, handle_check_failure
from ui import embeds

log = logging.getLogger("bot.cogs.growth")


class GrowthCog(commands.Cog):
    """声かけと紹介ランキング"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="growth", description="声かけ・紹介ランキング（管理者用）",
    )

    # -- 声かけ -------------------------------------------------

    @group.command(name="nudge", description="声かけの設定をします")
    @app_commands.describe(
        enabled="声かけを行うかどうか",
        welcome="認証した方へ『ようこそ案内』を送るか",
        cart_minutes="カートを残して何分で声をかけるか（0で送らない）",
        charged_hours="チャージ後、何時間注文が無ければ声をかけるか（0で送らない）",
        idle_days="残高がある方へ、何日注文が無ければ声をかけるか（0で送らない）",
        idle_min_balance="残高がこの額未満なら声をかけない（円）",
    )
    @admin_only()
    async def nudge(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        welcome: bool | None = None,
        cart_minutes: app_commands.Range[int, 0, 14] | None = None,
        charged_hours: app_commands.Range[int, 0, 168] | None = None,
        idle_days: app_commands.Range[int, 0, 365] | None = None,
        idle_min_balance: app_commands.Range[int, 0, 100000] | None = None,
    ) -> None:
        by = interaction.user.id
        for key, value in (
            ("nudge_enabled", enabled),
            ("nudge_welcome", welcome),
            ("nudge_cart_minutes", cart_minutes),
            ("nudge_charged_hours", charged_hours),
            ("nudge_idle_days", idle_days),
            ("nudge_idle_min_balance", idle_min_balance),
        ):
            if value is not None:
                await settings.set_value(key, value, updated_by=by)
        await interaction.response.send_message(
            embed=embeds.ok(self._nudge_text()), ephemeral=True,
        )

    def _nudge_text(self) -> str:
        on = settings.get("nudge_enabled", False)
        cart = int(settings.get("nudge_cart_minutes", 0) or 0)
        charged = int(settings.get("nudge_charged_hours", 0) or 0)
        idle = int(settings.get("nudge_idle_days", 0) or 0)
        low = int(settings.get("nudge_idle_min_balance", 0) or 0)

        def sw(v: bool) -> str:
            return "ON" if v else "OFF"

        body = (
            f"**声かけ全体**　{sw(on)}\n"
            f"**ようこそ案内**　{sw(settings.get('nudge_welcome', True))}"
            "（認証が済んだときに1回）\n"
            f"**カートの残り**　"
            + (f"{cart}分で声をかける" if cart else "送らない") + "\n"
            f"**チャージ後の未注文**　"
            + (f"{charged}時間で声をかける" if charged else "送らない") + "\n"
            f"**残高のお知らせ**　"
            + (f"{idle}日で声をかける（{embeds.yen(low)}以上）" if idle else "送らない")
        )
        notes = [
            "同じ相手に同じことは二度言いません",
            "受け取る側がボタン1つで止められます",
            f"一度に送るのは {config.NUDGE_BATCH} 件までです",
        ]
        body += f"\n\n{E.INFO} " + "\n".join(f"・{n}" for n in notes)

        if cart and cart >= config.CART_RESUME_MINUTES:
            body += (
                f"\n\n{E.WARN} カートの有効時間は "
                f"{config.CART_RESUME_MINUTES}分です。"
                f"{cart}分では**消えてから**声をかけることになり、"
                "「続きから」が使えません。短くしてください。"
            )
        if on and not settings.get("nudge_welcome", True) and not (
            cart or charged or idle
        ):
            body += f"\n\n{E.WARN} すべて送らない設定になっています。"
        return body

    @group.command(name="status", description="声かけの実績を表示します")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        st = await outreach.stats()
        body = self._nudge_text()
        if st:
            rows = []
            for kind, label in outreach.LABELS.items():
                got = st.get(kind)
                if not got:
                    continue
                miss = got["sent"] - got["delivered"]
                rows.append(
                    f"**{label}**　{got['delivered']} 件届いた"
                    + (f"（{miss} 件はDMを閉じていて届かず）" if miss else "")
                )
            if rows:
                body += "\n\n**これまでの実績**\n" + "\n".join(rows)
        else:
            body += f"\n\n{E.INFO} まだ送っていません。"
        await interaction.followup.send(
            embed=embeds.info(body, title=f"{E.MAIL} 声かけ"), ephemeral=True,
        )

    @group.command(name="preview", description="声かけのDMを自分に送って確認します")
    @app_commands.describe(kind="確認したい声かけ")
    @app_commands.choices(kind=[
        app_commands.Choice(name="ようこそ案内", value="welcome"),
        app_commands.Choice(name="カートの残り", value="cart_left"),
        app_commands.Choice(name="チャージ後の未注文", value="charged_unused"),
        app_commands.Choice(name="残高のお知らせ", value="idle_balance"),
    ])
    @admin_only()
    async def preview(
        self, interaction: discord.Interaction, kind: app_commands.Choice[str],
    ) -> None:
        """
        ⚠️ 本物の送信経路は通さない（記録を汚さないため）。
           見た目だけを本人に送る。
        """
        from ui import nudge_views

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild.name if interaction.guild else "サーバー"
        embed = {
            "welcome": lambda: nudge_views.welcome_embed(guild),
            "cart_left": lambda: nudge_views.cart_left_embed(""),
            "charged_unused": lambda: nudge_views.charged_unused_embed(1500),
            "idle_balance": lambda: nudge_views.idle_balance_embed(1500, 21),
        }[kind.value]()
        try:
            await interaction.user.send(embed=embed, view=nudge_views.NudgeView())
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.warn(
                    "DMをお送りできませんでした。"
                    "Discordの設定で、このサーバーからのDMを許可してください。"
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"「{kind.name}」のDMをお送りしました。\n"
                f"{E.INFO} これは確認用です。記録には残りません。"
            ),
            ephemeral=True,
        )

    @group.command(name="run", description="声かけを今すぐ1回まわします")
    @admin_only()
    async def run_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not outreach.enabled():
            await interaction.followup.send(
                embed=embeds.warn(
                    "声かけがOFFです。`/growth nudge enabled:True` で有効にしてください。"
                ),
                ephemeral=True,
            )
            return
        done = await outreach.run(self.bot)
        total = sum(done.values())
        body = (
            "\n".join(f"**{outreach.LABELS[k]}**　{n} 件"
                      for k, n in done.items() if n)
            if total else "送る相手はいませんでした。"
        )
        await interaction.followup.send(
            embed=embeds.ok(f"声かけを {total} 件送りました。\n\n{body}"
                            if total else body),
            ephemeral=True,
        )

    # -- 紹介ランキング -----------------------------------------

    @group.command(name="ranking", description="紹介ランキングの設定をします")
    @app_commands.describe(
        top="何位まで出すか",
        show_names="名前を出すか（OFFなら実績パネルと同じ匿名コード）",
    )
    @admin_only()
    async def ranking(
        self,
        interaction: discord.Interaction,
        top: app_commands.Range[int, 1, 20] | None = None,
        show_names: bool | None = None,
    ) -> None:
        by = interaction.user.id
        if top is not None:
            await settings.set_value("ranking_top", top, updated_by=by)
        if show_names is not None:
            await settings.set_value("ranking_show_names", show_names, updated_by=by)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"**出す人数**　{settings.get('ranking_top')} 位まで\n"
                f"**名前の表示**　"
                + ("名前を出す" if settings.get("ranking_show_names", True)
                   else "匿名コードで出す") + "\n\n"
                f"{E.INFO} `/panel ranking <チャンネル>` で設置できます。\n"
                f"{E.INFO} 設置後は {config.RANKING_REFRESH_MINUTES} 分ごとに"
                "自動で更新されます。"
            ),
            ephemeral=True,
        )

    # -- 入室時のようこそ ---------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """
        ようこそ案内。

        ⚠️ 認証機能を使っている場合は、**ここでは送らない**。
           認証が済んだ時点で送る（そのほうが読まれる）。
           二重に送らないよう、どちらか一方にする。
        """
        if member.bot:
            return
        try:
            from services.server import verify

            if verify.enabled():
                return
            await outreach.welcome(self.bot, member)
        except Exception:
            log.warning("ようこそ案内を送れませんでした", exc_info=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("growth コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GrowthCog(bot))
