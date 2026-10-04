"""入室時の認証（管理者用のコマンド）"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

import emoji as E
from core import settings
from db.models import Verification
from db.session import session_scope
from services.server import verify
from cogs._checks import admin_only, handle_check_failure
from ui import embeds

log = logging.getLogger("bot.cogs.verify")


class VerifyCog(commands.Cog):
    """入室時の認証"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="verify", description="入室時の認証（管理者用）",
    )

    # -- 設定 ---------------------------------------------------

    @group.command(name="setup", description="認証をまとめて設定します")
    @app_commands.describe(
        enabled="認証を受け付けるかどうか",
        role="認証した方に付けるロール",
        how="認証のやり方",
        pending_role="未認証の方に付いているロール（認証時に外します・任意）",
        log_channel="認証の記録を送る先（任意）",
    )
    @app_commands.choices(how=[
        app_commands.Choice(name="ボタンを押すだけ", value="button"),
        app_commands.Choice(name="画像の文字を入力（強め）", value="captcha"),
    ])
    @admin_only()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        role: discord.Role | None = None,
        how: app_commands.Choice[str] | None = None,
        pending_role: discord.Role | None = None,
        log_channel: discord.TextChannel | None = None,
    ) -> None:
        by = interaction.user.id
        await settings.set_value("verify_enabled", enabled, updated_by=by)
        if role is not None:
            await settings.set_value("verify_role", role.id, updated_by=by)
        if how is not None:
            await settings.set_value("verify_mode", how.value, updated_by=by)
        if pending_role is not None:
            await settings.set_value(
                "verify_pending_role", pending_role.id, updated_by=by,
            )
        if log_channel is not None:
            await settings.set_value(
                "verify_log_channel", log_channel.id, updated_by=by,
            )
        await interaction.response.send_message(
            embed=embeds.ok(self._status_text(interaction.guild)), ephemeral=True,
        )

    def _status_text(self, guild: discord.Guild | None) -> str:
        rid = verify.role_id()
        pend = verify.pending_role_id()
        lines = [
            f"**受け付け**　{'ON' if verify.enabled() else 'OFF'}",
            f"**やり方**　　{'画像の文字を入力' if verify.mode() == 'captcha' else 'ボタンを押すだけ'}",
            f"**付けるロール**　{f'<@&{rid}>' if rid else '**未設定**'}",
        ]
        if pend:
            lines.append(f"**外すロール**　<@&{pend}>")
        days = verify.min_account_days()
        if days:
            lines.append(f"**アカウント年齢**　{days}日以上")
        hours = int(settings.get("verify_kick_hours", 0) or 0)
        if hours:
            lines.append(f"**未認証の自動退出**　{hours}時間")
        body = "\n".join(lines)

        todo: list[str] = []
        if rid is None:
            todo.append("付けるロールが未設定です（`/verify setup role:` で指定）")
        if guild is not None:
            me = guild.me
            if me is not None:
                if not me.guild_permissions.manage_roles:
                    todo.append("BOTに「ロールの管理」権限が足りません")
                role = guild.get_role(rid) if rid else None
                if role is not None and role >= me.top_role:
                    todo.append(
                        f"付けるロール（{role.name}）がBOTより上にあるため付けられません。"
                        "サーバー設定でBOTのロールを上に動かしてください"
                    )
        if todo:
            body += f"\n\n{E.WARN} **ご確認ください**\n" + "\n".join(f"・{t}" for t in todo)
        else:
            body += f"\n\n{E.OK} `/panel verify <チャンネル>` でパネルを設置できます。"
        return body

    @group.command(name="rules", description="認証の条件を決めます")
    @app_commands.describe(
        min_account_days="Discordアカウント作成からの最低日数（0で条件なし）",
        kick_hours="未認証のまま何時間で退出させるか（0で退出させない）",
        max_attempts="画像認証を何回まちがえられるか（0で無制限）",
    )
    @admin_only()
    async def rules(
        self,
        interaction: discord.Interaction,
        min_account_days: app_commands.Range[int, 0, 365] | None = None,
        kick_hours: app_commands.Range[int, 0, 720] | None = None,
        max_attempts: app_commands.Range[int, 0, 20] | None = None,
    ) -> None:
        by = interaction.user.id
        if min_account_days is not None:
            await settings.set_value(
                "verify_min_account_days", min_account_days, updated_by=by,
            )
        if kick_hours is not None:
            await settings.set_value("verify_kick_hours", kick_hours, updated_by=by)
        if max_attempts is not None:
            await settings.set_value(
                "verify_max_attempts", max_attempts, updated_by=by,
            )
        note = ""
        if int(settings.get("verify_kick_hours", 0) or 0) > 0:
            note = (
                f"\n\n{E.WARN} 自動退出をONにしました。"
                "**認証パネルを設置してから**お使いください。"
                "パネルが無いと、認証する手段が無いまま退出させてしまいます。"
            )
        await interaction.response.send_message(
            embed=embeds.ok(self._status_text(interaction.guild) + note),
            ephemeral=True,
        )

    @group.command(name="status", description="認証の状況を表示します")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        st = await verify.stats(interaction.guild_id)
        body = self._status_text(interaction.guild)
        body += (
            f"\n\n**認証済み**　{st['verified']} 人\n"
            f"**失敗が残っている方**　{st['failing']} 人"
        )
        await interaction.followup.send(
            embed=embeds.info(body, title=f"{E.KEY} 認証"), ephemeral=True,
        )

    # -- 手当て -------------------------------------------------

    @group.command(name="user", description="指定した方を手動で認証します")
    @app_commands.describe(member="対象の方")
    @admin_only()
    async def user_cmd(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await verify.grant(member, method="manual")
        except verify.VerifyError as e:
            await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=embeds.ok(f"{member.mention} さんを認証しました。"), ephemeral=True,
        )

    @group.command(name="reset", description="失敗の回数を取り消します")
    @app_commands.describe(member="対象の方（省略すると全員）")
    @admin_only()
    async def reset(
        self, interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            q = delete(Verification).where(
                Verification.guild_id == interaction.guild_id,
                Verification.method == "pending",
            )
            if member is not None:
                q = q.where(Verification.discord_id == member.id)
            r = await s.execute(q)
            n = r.rowcount or 0
        who = member.mention if member else "全員"
        await interaction.followup.send(
            embed=embeds.ok(f"{who}の失敗回数を取り消しました（{n} 件）。"),
            ephemeral=True,
        )

    @group.command(
        name="bulk",
        description="いまロールを持っている方を、認証済みとして記録します",
    )
    @admin_only()
    async def bulk(self, interaction: discord.Interaction) -> None:
        """
        ⚠️ 途中から認証を入れたときの引っ越し用。
           すでにロールを持っている人を認証済みにして、
           自動退出の対象から外す。
        """
        rid = verify.role_id()
        if rid is None:
            await interaction.response.send_message(
                embed=embeds.warn("先に `/verify setup role:` でロールをご指定ください。"),
                ephemeral=True,
            )
            return
        role = interaction.guild.get_role(rid)
        if role is None:
            await interaction.response.send_message(
                embed=embeds.warn("ロールが見つかりません。"), ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        added = 0
        async with session_scope() as s:
            have = set((await s.execute(
                select(Verification.discord_id).where(
                    Verification.guild_id == interaction.guild_id,
                    Verification.method != "pending",
                )
            )).scalars().all())
            for m in role.members:
                if m.bot or m.id in have:
                    continue
                row = await s.get(Verification, (interaction.guild_id, m.id))
                if row is None:
                    s.add(Verification(
                        guild_id=interaction.guild_id, discord_id=m.id,
                        method="bulk",
                    ))
                else:
                    row.method = "bulk"
                added += 1
        await interaction.followup.send(
            embed=embeds.ok(
                f"{role.mention} をお持ちの **{added} 人**を認証済みにしました。\n"
                f"{E.INFO} この方々は自動退出の対象から外れます。"
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("verify コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerifyCog(bot))
