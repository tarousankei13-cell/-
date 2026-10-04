"""
警告と処分（管理者・モデレーター用）

⚠️ どの操作も監査ログに残る（`/admin audit` で見られる）。
⚠️ 本人への連絡は、キック・BANの**前に**送る。
   あとだと共通のサーバーが無くなり、DMが届かなくなる。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import emoji as E
from core import settings
from db.models import as_utc
from services.server import logs, mod
from cogs._checks import admin_only, handle_check_failure
from ui import embeds

log = logging.getLogger("bot.cogs.mod")


class ModCog(commands.Cog):
    """警告と処分"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="mod", description="警告と処分（管理者用）",
    )

    # -- 共通 ---------------------------------------------------

    async def _deny(
        self, interaction: discord.Interaction, why: str,
    ) -> None:
        await interaction.followup.send(embed=embeds.warn(why), ephemeral=True)

    async def _log(
        self, *, title: str, color: int, member: discord.abc.User,
        by: discord.abc.User, reason: str, extra: list[tuple[str, str]] = (),
    ) -> None:
        rows = [
            ("どなた", logs.who(member)),
            ("行った人", logs.who(by)),
            ("理由", logs.trim(reason or "（記載なし）")),
            *extra,
        ]
        await logs.send(self.bot, to_mod=True, embed=embeds.guard_log(
            title=title, color=color, lines=rows,
        ))

    # -- 警告 ---------------------------------------------------

    @group.command(name="warn", description="警告します")
    @app_commands.describe(member="対象の方", reason="理由（本人に伝わります）")
    @admin_only()
    async def warn(
        self, interaction: discord.Interaction,
        member: discord.Member, reason: app_commands.Range[str, 1, 400],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        why = mod.can_act(interaction.user, member)
        if why:
            await self._deny(interaction, why)
            return

        wid, count = await mod.add_warning(
            interaction.guild_id, member.id, interaction.user.id, reason,
        )
        got_dm = await mod.notify(
            member, guild_name=interaction.guild.name,
            action=f"警告（通算 {count} 回目）", reason=reason,
        )

        # たまったら自動で処分する
        auto = mod.escalation(count)
        done = ""
        if auto:
            kind, note = auto
            done = await self._escalate(interaction, member, kind, note)

        body = (
            f"{member.mention} さんに警告しました（通算 **{count}** 回）。\n"
            f"**理由**　{reason}\n"
            f"{E.MAIL} 本人への連絡　{'届きました' if got_dm else '**届きませんでした**（DMを閉じています）'}"
        )
        if done:
            body += f"\n{E.HAMMER} {done}"
        await interaction.followup.send(embed=embeds.ok(body), ephemeral=True)

        await self._log(
            title=f"{E.WARN} 警告", color=embeds.YELLOW,
            member=member, by=interaction.user, reason=reason,
            extra=[("通算", f"{count} 回"), ("記録ID", f"#{wid}")],
        )

    async def _escalate(
        self, interaction: discord.Interaction,
        member: discord.Member, kind: str, note: str,
    ) -> str:
        """警告がたまったときの自動処分。"""
        try:
            if kind == "timeout":
                mins = int(settings.get("mod_warn_timeout_minutes", 60) or 60)
                await mod.timeout(
                    member, mins, reason=note, by=interaction.user.id,
                )
                return f"{note} → {mins}分の発言停止にしました"
            if kind == "kick":
                await mod.notify(
                    member, guild_name=interaction.guild.name,
                    action="キック", reason=note,
                )
                await mod.kick(member, reason=note, by=interaction.user.id)
                return f"{note} → キックしました"
            if kind == "ban":
                await mod.notify(
                    member, guild_name=interaction.guild.name,
                    action="BAN", reason=note,
                )
                await mod.ban(
                    interaction.guild, member, reason=note, by=interaction.user.id,
                )
                return f"{note} → BANしました"
        except mod.ModError as e:
            return f"自動処分できませんでした（{e}）"
        except discord.HTTPException as e:
            return f"自動処分できませんでした（{e}）"
        return ""

    @group.command(name="warnings", description="警告の履歴を表示します")
    @app_commands.describe(member="対象の方")
    @admin_only()
    async def warnings(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await mod.all_warnings(interaction.guild_id, member.id)
        if not rows:
            await interaction.followup.send(
                embed=embeds.info(f"{member.mention} さんに警告はありません。"),
                ephemeral=True,
            )
            return
        live = [r for r in rows if r.cleared_at is None]
        lines = []
        for r in rows[-20:]:
            when = int(as_utc(r.created_at).timestamp())
            mark = E.WARN if r.cleared_at is None else E.WAIT
            text = f"{mark} `#{r.id}` <t:{when}:d>　{r.reason or '（理由なし）'}"
            if r.cleared_at is not None:
                text += "　**取消済**"
            lines.append(text)
        body = (
            f"**有効な警告　{len(live)} 件**（これまで {len(rows)} 件）\n\n"
            + "\n".join(lines)
        )
        auto = mod.escalation(len(live))
        if auto:
            body += f"\n\n{E.INFO} いまの件数は自動処分の条件（{auto[1]}）に達しています。"
        await interaction.followup.send(
            embed=embeds.info(body[:4000], title=f"{E.WARN} {member.display_name} さんの警告"),
            ephemeral=True,
        )

    @group.command(name="unwarn", description="警告を取り消します")
    @app_commands.describe(
        member="対象の方",
        warning_id="取り消す記録ID（`/mod warnings` の #番号）。省略すると全部",
    )
    @admin_only()
    async def unwarn(
        self, interaction: discord.Interaction,
        member: discord.Member, warning_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if warning_id is not None:
            ok = await mod.clear_warning(warning_id, interaction.user.id)
            msg = (f"警告 `#{warning_id}` を取り消しました。" if ok
                   else f"`#{warning_id}` は見つからないか、すでに取り消し済みです。")
        else:
            n = await mod.clear_all_warnings(
                interaction.guild_id, member.id, interaction.user.id,
            )
            msg = f"{member.mention} さんの警告を **{n} 件** 取り消しました。"
        await interaction.followup.send(embed=embeds.ok(msg), ephemeral=True)

    @group.command(name="warn_rules", description="警告がたまったときの自動処分を決めます")
    @app_commands.describe(
        timeout_at="何回で発言停止にするか（0でしない）",
        timeout_minutes="その発言停止の長さ（分）",
        kick_at="何回でキックするか（0でしない）",
        ban_at="何回でBANするか（0でしない）",
    )
    @admin_only()
    async def warn_rules(
        self,
        interaction: discord.Interaction,
        timeout_at: app_commands.Range[int, 0, 50] | None = None,
        timeout_minutes: app_commands.Range[int, 1, 40320] | None = None,
        kick_at: app_commands.Range[int, 0, 50] | None = None,
        ban_at: app_commands.Range[int, 0, 50] | None = None,
    ) -> None:
        by = interaction.user.id
        for key, value in (
            ("mod_warn_timeout_at", timeout_at),
            ("mod_warn_timeout_minutes", timeout_minutes),
            ("mod_warn_kick_at", kick_at),
            ("mod_warn_ban_at", ban_at),
        ):
            if value is not None:
                await settings.set_value(key, value, updated_by=by)

        def show(key: str, unit: str = "回") -> str:
            v = int(settings.get(key, 0) or 0)
            return f"{v}{unit}" if v else "しない"

        await interaction.response.send_message(
            embed=embeds.ok(
                f"**発言停止**　{show('mod_warn_timeout_at')}"
                f"（{settings.get('mod_warn_timeout_minutes')}分）\n"
                f"**キック**　　{show('mod_warn_kick_at')}\n"
                f"**BAN**　　　{show('mod_warn_ban_at')}\n\n"
                f"{E.INFO} 強い方が優先されます"
                "（5回でBAN・3回で停止なら、5回目はBANになります）。"
            ),
            ephemeral=True,
        )

    # -- 処分 ---------------------------------------------------

    @group.command(name="timeout", description="発言を止めます")
    @app_commands.describe(
        member="対象の方", minutes="止める長さ（分）", reason="理由",
    )
    @admin_only()
    async def timeout_cmd(
        self, interaction: discord.Interaction, member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: app_commands.Range[str, 0, 400] = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        why = mod.can_act(interaction.user, member)
        if why:
            await self._deny(interaction, why)
            return
        got_dm = await mod.notify(
            member, guild_name=interaction.guild.name,
            action=f"{minutes}分の発言停止", reason=reason,
        )
        try:
            await mod.timeout(
                member, minutes, reason=reason or "（記載なし）", by=interaction.user.id,
            )
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"{member.mention} さんを **{minutes}分** 発言停止にしました。\n"
                f"{E.MAIL} 本人への連絡　{'届きました' if got_dm else '届きませんでした'}"
            ),
            ephemeral=True,
        )
        await self._log(
            title=f"{E.MUTE} 発言停止", color=embeds.RED,
            member=member, by=interaction.user, reason=reason,
            extra=[("長さ", f"{minutes} 分")],
        )

    @group.command(name="untimeout", description="発言停止を解除します")
    @app_commands.describe(member="対象の方")
    @admin_only()
    async def untimeout_cmd(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await mod.untimeout(member, by=interaction.user.id)
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(f"{member.mention} さんの発言停止を解除しました。"),
            ephemeral=True,
        )

    @group.command(name="kick", description="サーバーから退出させます")
    @app_commands.describe(member="対象の方", reason="理由")
    @admin_only()
    async def kick_cmd(
        self, interaction: discord.Interaction,
        member: discord.Member,
        reason: app_commands.Range[str, 0, 400] = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        why = mod.can_act(interaction.user, member)
        if why:
            await self._deny(interaction, why)
            return
        # ⚠️ キックの前に送る。あとだとDMが届かなくなる。
        got_dm = await mod.notify(
            member, guild_name=interaction.guild.name,
            action="キック", reason=reason,
            extra="もう一度ご参加いただくことは可能です。",
        )
        try:
            await mod.kick(member, reason=reason or "（記載なし）", by=interaction.user.id)
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"{member.mention} さんを退出させました。\n"
                f"{E.MAIL} 本人への連絡　{'届きました' if got_dm else '届きませんでした'}"
            ),
            ephemeral=True,
        )
        await self._log(
            title=f"{E.HAMMER} キック", color=embeds.RED,
            member=member, by=interaction.user, reason=reason,
        )

    @group.command(name="ban", description="BANします")
    @app_commands.describe(
        member="対象の方", reason="理由",
        delete_days="直近何日分のメッセージも消すか（0〜7）",
    )
    @admin_only()
    async def ban_cmd(
        self, interaction: discord.Interaction, member: discord.Member,
        reason: app_commands.Range[str, 0, 400] = "",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        why = mod.can_act(interaction.user, member)
        if why:
            await self._deny(interaction, why)
            return
        got_dm = await mod.notify(
            member, guild_name=interaction.guild.name,
            action="BAN", reason=reason,
        )
        try:
            await mod.ban(
                interaction.guild, member, reason=reason or "（記載なし）",
                by=interaction.user.id, delete_days=delete_days,
            )
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"{member.mention} さんをBANしました。\n"
                f"{E.MAIL} 本人への連絡　{'届きました' if got_dm else '届きませんでした'}"
            ),
            ephemeral=True,
        )
        await self._log(
            title=f"{E.HAMMER} BAN", color=embeds.RED,
            member=member, by=interaction.user, reason=reason,
            extra=[("消したメッセージ", f"直近 {delete_days} 日分")] if delete_days else [],
        )

    @group.command(name="unban", description="BANを解除します")
    @app_commands.describe(user_id="解除する方のユーザーID")
    @admin_only()
    async def unban_cmd(
        self, interaction: discord.Interaction,
        user_id: app_commands.Range[str, 1, 25],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not user_id.strip().isdigit():
            await self._deny(interaction, "ユーザーIDは数字でご指定ください。")
            return
        try:
            ok = await mod.unban(
                interaction.guild, int(user_id), by=interaction.user.id,
            )
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok("BANを解除しました。") if ok
            else embeds.warn("その方はBANされていません。"),
            ephemeral=True,
        )

    # -- チャンネルの操作 ---------------------------------------

    @group.command(name="purge", description="メッセージをまとめて消します")
    @app_commands.describe(
        count="消す件数", member="この方の分だけ消す場合（任意）",
    )
    @admin_only()
    async def purge(
        self, interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 200],
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            n = await mod.purge(
                interaction.channel, count, by=interaction.user.id, user=member,
            )
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        body = f"**{n} 件** 消しました。"
        if n < count:
            body += (
                f"\n{E.INFO} {count}件のご指定でしたが {n}件でした。"
                "Discordでは**14日より古いメッセージをまとめて消せません**。"
            )
        await interaction.followup.send(embed=embeds.ok(body), ephemeral=True)
        await self._log(
            title=f"{E.BROOM} 一括削除", color=embeds.BLUE,
            member=member or interaction.user, by=interaction.user,
            reason=f"{n} 件",
            extra=[("どこ", f"<#{interaction.channel_id}>")],
        )

    @group.command(name="slowmode", description="低速モードを設定します")
    @app_commands.describe(seconds="何秒に1回にするか（0で解除）")
    @admin_only()
    async def slowmode(
        self, interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await mod.slowmode(interaction.channel, seconds, by=interaction.user.id)
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(
                f"低速モードを **{seconds}秒** にしました。" if seconds
                else "低速モードを解除しました。"
            ),
            ephemeral=True,
        )

    @group.command(name="lock", description="このチャンネルの発言を止めます")
    @app_commands.describe(unlock="解除する場合は True")
    @admin_only()
    async def lock(
        self, interaction: discord.Interaction, unlock: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await mod.lock(
                interaction.channel, locked=not unlock, by=interaction.user.id,
            )
        except mod.ModError as e:
            await self._deny(interaction, str(e))
            return
        await interaction.followup.send(
            embed=embeds.ok(
                "発言を止めました。`/mod lock unlock:True` で戻せます。"
                if not unlock else "発言できるように戻しました。"
            ),
            ephemeral=True,
        )
        await self._log(
            title=f"{E.LOCK} チャンネルを{'開けました' if unlock else '閉じました'}",
            color=embeds.BLUE,
            member=interaction.user, by=interaction.user, reason="",
            extra=[("どこ", f"<#{interaction.channel_id}>")],
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("mod コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModCog(bot))
