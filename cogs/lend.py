"""BOTの貸し出し（オーナー専用）

このBOTを他のサーバーへ貸すための窓口。

⚠️ **オーナーだけ**が使える。管理者（admin_only）では開けない。
   貸し出しは売り物そのものなので、サーバーの管理者に触らせない。

⚠️ このコマンド群は**関所を素通りする**（ui/gate.py の ALWAYS）。
   素通りしないと、ホーム未設定のときに `/lend home` が打てず詰む。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import emoji as E
from core import audit
from core import license as lic
from cogs._checks import handle_check_failure, owner_only, self_checked
from ui import embeds

log = logging.getLogger("bot.cogs.lend")


def _when(dt) -> str:
    return dt.astimezone(config.JST).strftime("%Y/%m/%d %H:%M") if dt else "—"


def _line(row) -> str:
    """一覧の1行。"""
    st = lic._status_of(row)
    name = row.guild_name or "（名前不明）"
    if row.is_home:
        return f"{E.KEY} **{name}**　`{row.guild_id}`　ホーム（期限なし）"
    if row.suspended:
        why = f"：{row.suspend_reason}" if row.suspend_reason else ""
        return f"{E.NG} **{name}**　`{row.guild_id}`　停止中{why}"
    left = st.days_left
    if left is not None and left <= 0:
        return f"{E.NG} **{name}**　`{row.guild_id}`　期限切れ（{_when(st.expires_at)}）"
    mark = E.WARN if (left is not None and left <= 3) else E.OK
    return (f"{mark} **{name}**　`{row.guild_id}`　"
            f"残り **{left}日**（{_when(st.expires_at)}）")


class LendCog(commands.Cog):
    """BOTの貸し出し"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    lend = app_commands.Group(
        name="lend", description="BOTの貸し出し（オーナー専用）",
        guild_only=False,
    )

    # -- 貸す ---------------------------------------------------

    @lend.command(name="grant", description="サーバーへ貸し出します（期限を入れ替えます）")
    @app_commands.describe(
        guild_id="貸すサーバーのID（省略するといまのサーバー）",
        days="何日間貸すか",
        contact="連絡先の人（期限の予告をDMします）",
        note="覚え書き（受け取った金額など）",
    )
    @owner_only()
    async def lend_grant(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650],
        guild_id: app_commands.Range[str, 1, 24] | None = None,
        contact: discord.User | None = None,
        note: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, err = self._resolve(interaction, guild_id)
        if err:
            await interaction.followup.send(embed=embeds.error(err), ephemeral=True)
            return

        g = self.bot.get_guild(gid)
        try:
            st = await lic.grant(
                gid, days, guild_name=(g.name if g else ""),
                actor_id=interaction.user.id,
                contact_id=contact.id if contact else None,
                note=note or "",
            )
        except lic.LicenseError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return

        await audit.record(
            action="lend.grant", actor_id=interaction.user.id,
            actor_name=str(interaction.user), target=str(gid),
            detail={"days": days, "expires": _when(st.expires_at)},
        )
        await interaction.followup.send(
            embed=embeds.ok(
                f"**{g.name if g else gid}** に **{days}日間** 貸し出しました。\n"
                f"期限: **{_when(st.expires_at)}**\n\n"
                f"{E.INFO} 期限の **7日前・3日前・前日** に予告が出ます。"
                + (f"\n{E.INFO} 連絡先: {contact.mention}" if contact else "")
            ),
            ephemeral=True,
        )
        await self._tell_guild(gid, st, f"{days}日間のご利用を開始しました")

    @lend.command(name="extend", description="期限を延ばします")
    @app_commands.describe(
        days="何日ぶん延ばすか",
        guild_id="サーバーのID（省略するといまのサーバー）",
        note="覚え書き",
    )
    @owner_only()
    async def lend_extend(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650],
        guild_id: app_commands.Range[str, 1, 24] | None = None,
        note: app_commands.Range[str, 1, 500] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, err = self._resolve(interaction, guild_id)
        if err:
            await interaction.followup.send(embed=embeds.error(err), ephemeral=True)
            return
        try:
            st = await lic.extend(gid, days, actor_id=interaction.user.id,
                                  note=note or "")
        except lic.LicenseError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return

        await audit.record(
            action="lend.extend", actor_id=interaction.user.id,
            actor_name=str(interaction.user), target=str(gid),
            detail={"days": days, "expires": _when(st.expires_at)},
        )
        g = self.bot.get_guild(gid)
        await interaction.followup.send(
            embed=embeds.ok(
                f"**{g.name if g else gid}** の期限を **{days}日** 延ばしました。\n"
                f"新しい期限: **{_when(st.expires_at)}**（残り {st.days_left}日）"
            ),
            ephemeral=True,
        )
        await self._tell_guild(gid, st, f"ご利用期限を{days}日延長しました")

    # -- 止める・再開する ---------------------------------------

    @lend.command(name="revoke", description="貸し出しを止めます（期限は残ります）")
    @app_commands.describe(
        guild_id="サーバーのID（省略するといまのサーバー）", reason="理由",
    )
    @owner_only()
    async def lend_revoke(
        self, interaction: discord.Interaction,
        guild_id: app_commands.Range[str, 1, 24] | None = None,
        reason: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, err = self._resolve(interaction, guild_id)
        if err:
            await interaction.followup.send(embed=embeds.error(err), ephemeral=True)
            return
        try:
            done = await lic.revoke(gid, actor_id=interaction.user.id,
                                    reason=reason or "")
        except lic.LicenseError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        if not done:
            await interaction.followup.send(
                embed=embeds.info("そのサーバーには貸し出していません。"), ephemeral=True)
            return
        await audit.record(
            action="lend.revoke", actor_id=interaction.user.id,
            actor_name=str(interaction.user), target=str(gid),
            reason=reason or "",
        )
        await interaction.followup.send(
            embed=embeds.ok(
                f"`{gid}` の利用を止めました。\n"
                f"{E.INFO} 期限そのものは残っています。`/lend resume` で戻せます。"
            ),
            ephemeral=True,
        )

    @lend.command(name="resume", description="止めていた貸し出しを再開します")
    @app_commands.describe(guild_id="サーバーのID（省略するといまのサーバー）")
    @owner_only()
    async def lend_resume(
        self, interaction: discord.Interaction,
        guild_id: app_commands.Range[str, 1, 24] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, err = self._resolve(interaction, guild_id)
        if err:
            await interaction.followup.send(embed=embeds.error(err), ephemeral=True)
            return
        done = await lic.resume(gid, actor_id=interaction.user.id)
        st = await lic.status(gid)
        await audit.record(
            action="lend.resume", actor_id=interaction.user.id,
            actor_name=str(interaction.user), target=str(gid),
        )
        await interaction.followup.send(
            embed=(embeds.ok(
                f"`{gid}` の利用を再開しました。\n"
                f"期限: **{_when(st.expires_at)}**"
            ) if done else embeds.info("止まっていませんでした。")),
            ephemeral=True,
        )

    # -- 見る ---------------------------------------------------

    @lend.command(name="list", description="貸し出しの一覧を表示します")
    @owner_only()
    async def lend_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await lic.all_licenses()
        if not rows:
            await interaction.followup.send(
                embed=embeds.info(
                    "まだどこにも貸し出していません。\n"
                    f"{E.INFO} `/lend home` で、ご自身のサーバーを登録してください。"
                ),
                ephemeral=True,
            )
            return

        lines = [_line(r) for r in rows]
        e = discord.Embed(
            title=f"{E.RECEIPT} BOTの貸し出し",
            description="\n".join(lines)[:4000],
            color=embeds.BLUE,
        )
        live = sum(1 for r in rows if lic._status_of(r).allowed and not r.is_home)
        e.set_footer(text=f"貸し出し中 {live} 件 / 登録 {len(rows)} 件")

        # ⚠️ 貸していないのに入っているサーバーを必ず出す。
        #    気づかないと、無断で使われ続ける。
        known = {r.guild_id for r in rows}
        stray = [g for g in self.bot.guilds if g.id not in known]
        if stray:
            e.add_field(
                name=f"{E.WARN} 貸していないのに入っているサーバー",
                value="\n".join(f"・{g.name}　`{g.id}`" for g in stray[:10])[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    @lend.command(name="status", description="このサーバーの利用状況を表示します")
    @self_checked()
    async def lend_status(self, interaction: discord.Interaction) -> None:
        """⚠️ ここだけは誰でも見られる。貸し先の管理者が、自分の
           サーバーの期限を確認できないと不便なため。
           見えるのは期限だけで、他のサーバーのことは出さない。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        st = await lic.status(interaction.guild_id)
        if st.is_home:
            body = f"{E.KEY} このサーバーはホームです。期限はありません。"
        elif not st.allowed:
            body = st.message()
        else:
            body = (
                f"{E.OK} ご利用いただけます。\n\n"
                f"期限　**{_when(st.expires_at)}**\n"
                f"残り　**{st.days_left}日**"
            )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.RECEIPT} このサーバーの利用状況",
                description=body,
                color=embeds.GREEN if st.allowed else embeds.RED,
            ),
            ephemeral=True,
        )

    @lend.command(name="home", description="このサーバーをホーム（持ち主のサーバー）にします")
    @owner_only()
    async def lend_home(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild_id:
            await interaction.followup.send(
                embed=embeds.error("サーバーの中で実行してください。"), ephemeral=True)
            return
        before = await lic.home_guild_id()
        await lic.set_home(interaction.guild_id, actor_id=interaction.user.id)
        await audit.record(
            action="lend.home", actor_id=interaction.user.id,
            actor_name=str(interaction.user), target=str(interaction.guild_id),
        )
        extra = ""
        if before and before != interaction.guild_id:
            extra = (
                f"\n\n{E.WARN} 前のホーム `{before}` は貸し先に戻り、"
                "**30日の期限**が入りました。必要なら調整してください。"
            )
        await interaction.followup.send(
            embed=embeds.ok(
                "このサーバーをホームにしました。期限なしで使えます。" + extra
            ),
            ephemeral=True,
        )

    # -- 内部 ---------------------------------------------------

    def _resolve(self, interaction: discord.Interaction,
                 guild_id: str | None) -> tuple[int, str]:
        """サーバーIDを決める。(id, エラー文) を返す。"""
        if guild_id:
            t = guild_id.strip()
            if not t.isdigit():
                return 0, "サーバーIDは数字で指定してください。"
            return int(t), ""
        if not interaction.guild_id:
            return 0, "サーバーの外で実行する場合は、サーバーIDを指定してください。"
        return interaction.guild_id, ""

    async def _tell_guild(self, guild_id: int, st: lic.Status, headline: str) -> None:
        """貸し先へ知らせる。届かなくても処理は止めない。"""
        g = self.bot.get_guild(guild_id)
        if g is None:
            return
        body = (
            f"{headline}。\n\n"
            f"期限　**{_when(st.expires_at)}**\n"
            f"残り　**{st.days_left}日**"
        )
        e = discord.Embed(title=f"{E.OK} ご利用について", description=body,
                          color=embeds.GREEN)
        ch = g.system_channel or next(
            (c for c in g.text_channels if c.permissions_for(g.me).send_messages),
            None,
        )
        if ch is not None:
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                log.debug("貸し先への連絡を送れませんでした", exc_info=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("lend コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LendCog(bot))
