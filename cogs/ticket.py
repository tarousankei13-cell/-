"""お問い合わせチケット（管理者・担当者用のコマンド）"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import emoji as E
from core import settings
from services.server import tickets
from cogs._checks import admin_only, handle_check_failure, self_checked
from ui import embeds

log = logging.getLogger("bot.cogs.ticket")


class TicketCog(commands.Cog):
    """問い合わせチケット"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="ticket", description="お問い合わせチケット（管理者用）",
    )

    # -- 設定 ---------------------------------------------------

    @group.command(name="setup", description="チケット機能をまとめて設定します")
    @app_commands.describe(
        enabled="受け付けるかどうか",
        place="方式。チャンネルは担当ロールに見せやすく、スレッドは場所を取りません",
        where="チャンネル方式の置き場所（カテゴリ）／スレッド方式の親チャンネル",
        staff="担当ロール（1つずつ追加してください）",
        log_channel="終了したやり取りの控えを送る先",
    )
    @app_commands.choices(place=[
        app_commands.Choice(name="専用チャンネル（おすすめ）", value="channel"),
        app_commands.Choice(name="プライベートスレッド", value="thread"),
    ])
    @admin_only()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        place: app_commands.Choice[str] | None = None,
        where: discord.TextChannel | discord.CategoryChannel | None = None,
        staff: discord.Role | None = None,
        log_channel: discord.TextChannel | None = None,
    ) -> None:
        by = interaction.user.id
        await settings.set_value("ticket_enabled", enabled, updated_by=by)
        if place:
            await settings.set_value("ticket_mode", place.value, updated_by=by)

        if where is not None:
            if isinstance(where, discord.CategoryChannel):
                await settings.set_value("ticket_category", where.id, updated_by=by)
            else:
                await settings.set_value("ticket_channel", where.id, updated_by=by)

        if staff is not None:
            have = [int(r) for r in (settings.get("ticket_staff_roles") or [])]
            if staff.id not in have:
                have.append(staff.id)
            await settings.set_value("ticket_staff_roles", have, updated_by=by)

        if log_channel is not None:
            await settings.set_value(
                "ticket_log_channel", log_channel.id, updated_by=by,
            )

        await interaction.response.send_message(
            embed=embeds.ok(self._status_text(interaction.guild)),
            ephemeral=True,
        )

    def _status_text(self, guild: discord.Guild | None) -> str:
        """いまの設定と、足りないものを並べる。"""
        mode = tickets.mode()
        roles = tickets.staff_role_ids()
        lines = [
            f"**受け付け**　{'ON' if tickets.enabled() else 'OFF'}",
            f"**方式**　　　{'専用チャンネル' if mode == 'channel' else 'プライベートスレッド'}",
        ]
        if mode == "channel":
            cid = settings.get("ticket_category")
            lines.append(f"**置き場所**　{f'<#{cid}>' if cid else '（指定なし・一番上に作ります）'}")
        else:
            cid = settings.get("ticket_channel")
            lines.append(f"**親チャンネル**　{f'<#{cid}>' if cid else '**未設定**'}")
        lines.append(
            "**担当ロール**　"
            + ("　".join(f"<@&{r}>" for r in roles) if roles else "**未設定**")
        )
        lcid = settings.get("ticket_log_channel")
        lines.append(f"**控えの送り先**　{f'<#{lcid}>' if lcid else '（監視ログと同じ）'}")

        body = "\n".join(lines)

        # 足りないもの・権限の確認
        todo: list[str] = []
        if not roles:
            todo.append("担当ロールが未設定です（`/ticket setup staff:` で追加）")
        if mode == "thread":
            if not settings.get("ticket_channel"):
                todo.append("親チャンネルが未設定です（`/panel ticket` で自動設定されます）")
            todo.append(
                "スレッド方式では、ロールを@メンションしても担当者はスレッドに"
                "**入りません**。親チャンネルで担当ロールに"
                "「スレッドの管理」権限を与えてください"
            )
        if guild is not None and guild.me is not None:
            perms = guild.me.guild_permissions
            need = []
            if mode == "channel" and not perms.manage_channels:
                need.append("チャンネルの管理")
            if mode == "thread" and not perms.create_private_threads:
                need.append("プライベートスレッドの作成")
            if not perms.manage_messages:
                need.append("メッセージの管理")
            if need:
                todo.append("BOTに次の権限が足りません：" + "、".join(need))

        if todo:
            body += f"\n\n{E.WARN} **ご確認ください**\n" + "\n".join(
                f"・{t}" for t in todo
            )
        else:
            body += f"\n\n{E.OK} `/panel ticket <チャンネル>` でパネルを設置できます。"
        return body

    @group.command(name="status", description="チケットの設定と件数を表示します")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        counts = await tickets.counts(interaction.guild_id)
        open_now = await tickets.open_tickets(interaction.guild_id)
        body = self._status_text(interaction.guild)
        body += (
            f"\n\n**いまの件数**\n"
            f"　対応中　{counts.get('OPEN', 0) + counts.get('CLAIMED', 0)} 件"
            f"（うち担当者あり {counts.get('CLAIMED', 0)} 件）\n"
            f"　終了済み　{counts.get('CLOSED', 0)} 件"
        )
        if open_now:
            listed = "\n".join(
                f"　{t.number}　<#{t.channel_id}>　<@{t.opener_id}>"
                for t in open_now[:15]
            )
            body += f"\n\n**開いているもの**\n{listed}"
            if len(open_now) > 15:
                body += f"\n　…ほか {len(open_now) - 15} 件"
        await interaction.followup.send(
            embed=embeds.info(body, title=f"{E.TICKET} お問い合わせ"),
            ephemeral=True,
        )

    @group.command(name="staff", description="担当ロールを足す・外します")
    @app_commands.describe(role="対象のロール", remove="外す場合は True")
    @admin_only()
    async def staff(
        self, interaction: discord.Interaction,
        role: discord.Role, remove: bool = False,
    ) -> None:
        have = [int(r) for r in (settings.get("ticket_staff_roles") or [])]
        if remove:
            have = [r for r in have if r != role.id]
        elif role.id not in have:
            have.append(role.id)
        await settings.set_value(
            "ticket_staff_roles", have, updated_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                f"担当ロールを{'外しました' if remove else '追加しました'}：{role.mention}\n"
                + (f"{E.INFO} いまの担当ロール　"
                   + ("　".join(f"<@&{r}>" for r in have) if have else "（なし）"))
            ),
            ephemeral=True,
        )

    @group.command(name="rules", description="同時に開ける数と、自動で閉じる時間を決めます")
    @app_commands.describe(
        max_open="1人が同時に開ける件数（0で無制限）",
        auto_close_hours="お返事が無いとき何時間で閉じるか（0で閉じない）",
        ping_staff="作られたときに担当ロールへ声をかけるか",
    )
    @admin_only()
    async def rules(
        self,
        interaction: discord.Interaction,
        max_open: app_commands.Range[int, 0, 10] | None = None,
        auto_close_hours: app_commands.Range[int, 0, 720] | None = None,
        ping_staff: bool | None = None,
    ) -> None:
        by = interaction.user.id
        if max_open is not None:
            await settings.set_value("ticket_max_open", max_open, updated_by=by)
        if auto_close_hours is not None:
            await settings.set_value(
                "ticket_auto_close_hours", auto_close_hours, updated_by=by,
            )
        if ping_staff is not None:
            await settings.set_value("ticket_ping_staff", ping_staff, updated_by=by)

        cap = int(settings.get("ticket_max_open", 0) or 0)
        hours = int(settings.get("ticket_auto_close_hours", 0) or 0)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"**同時に開ける数**　{cap if cap else '無制限'}\n"
                f"**自動で閉じる**　　{f'{hours}時間' if hours else '閉じない'}\n"
                f"**担当へ声かけ**　　{'する' if settings.get('ticket_ping_staff', True) else 'しない'}\n"
                f"\n{E.INFO} パネルの文面にも反映されます（`/panel refresh`）。"
            ),
            ephemeral=True,
        )

    @group.command(name="kinds", description="相談の種別を設定します")
    @app_commands.describe(
        labels="「表示名|説明」を改行か「,」で区切って並べます。空で既定に戻します",
    )
    @admin_only()
    async def kinds_cmd(
        self, interaction: discord.Interaction, labels: str = "",
    ) -> None:
        import config

        text = (labels or "").strip()
        if not text:
            await settings.set_value(
                "ticket_kinds", list(config.TICKET_KINDS_DEFAULT),
                updated_by=interaction.user.id,
            )
            await interaction.response.send_message(
                embed=embeds.ok("相談の種別を既定に戻しました。"), ephemeral=True,
            )
            return

        parts = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
        if len(parts) > 25:
            await interaction.response.send_message(
                embed=embeds.error("種別は25個までです（Discordの制限）。"),
                ephemeral=True,
            )
            return
        made = []
        for i, p in enumerate(parts):
            label, _, desc = p.partition("|")
            made.append({
                "key": f"k{i}", "label": label.strip()[:100],
                "emoji": "💬", "desc": desc.strip()[:100],
            })
        await settings.set_value(
            "ticket_kinds", made, updated_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                "相談の種別を変えました。\n"
                + "\n".join(f"・{m['label']}" for m in made)
                + f"\n\n{E.WARN} すでに貼ってあるパネルは `/panel refresh` で"
                "貼り直してください。"
            ),
            ephemeral=True,
        )

    # -- 操作 ---------------------------------------------------

    @group.command(name="close", description="このチケットを閉じます")
    @app_commands.describe(reason="終了の理由（記録に残ります）")
    @self_checked()
    async def close(
        self, interaction: discord.Interaction, reason: str = "",
    ) -> None:
        ticket = await tickets.by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.warn("ここはチケットではありません。"), ephemeral=True,
            )
            return
        if interaction.user.id != ticket.opener_id and not tickets.is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                embed=embeds.warn("開いたご本人と担当者だけが閉じられます。"),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await tickets.close(
                self.bot, interaction.channel,
                closed_by=interaction.user.id, reason=reason,
            )
        except tickets.TicketError as e:
            await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
            return
        try:
            await interaction.followup.send(
                embed=embeds.ok("終了しました。"), ephemeral=True,
            )
        except discord.HTTPException:
            pass          # チャンネルごと消えている場合がある

    @group.command(name="add", description="このチケットに人を呼びます")
    @app_commands.describe(user="呼ぶ相手")
    @self_checked()
    async def add(
        self, interaction: discord.Interaction, user: discord.Member,
    ) -> None:
        ticket = await tickets.by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.warn("ここはチケットではありません。"), ephemeral=True,
            )
            return
        if not tickets.is_staff(interaction.user):
            await interaction.response.send_message(
                embed=embeds.warn("担当者の方だけが呼べます。"), ephemeral=True,
            )
            return
        try:
            if ticket.is_thread:
                await interaction.channel.add_user(user)
            else:
                await interaction.channel.set_permissions(
                    user, view_channel=True, send_messages=True,
                    read_message_history=True, reason="チケットに追加",
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=embeds.error("追加できませんでした（権限不足）。"),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} さんを呼びました。")
        )

    @group.command(name="remove", description="このチケットから人を外します")
    @app_commands.describe(user="外す相手")
    @self_checked()
    async def remove(
        self, interaction: discord.Interaction, user: discord.Member,
    ) -> None:
        ticket = await tickets.by_channel(interaction.channel_id)
        if ticket is None or not tickets.is_staff(interaction.user):
            await interaction.response.send_message(
                embed=embeds.warn("担当者の方が、チケットの中でお使いください。"),
                ephemeral=True,
            )
            return
        if user.id == ticket.opener_id:
            await interaction.response.send_message(
                embed=embeds.warn(
                    "この問い合わせを開いたご本人は外せません。"
                    "終わっている場合は `/ticket close` をお使いください。"
                ),
                ephemeral=True,
            )
            return
        try:
            if ticket.is_thread:
                await interaction.channel.remove_user(user)
            else:
                await interaction.channel.set_permissions(
                    user, overwrite=None, reason="チケットから除外",
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=embeds.error("外せませんでした（権限不足）。"), ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=embeds.ok(f"{user.mention} さんを外しました。")
        )

    # -- 発言の記録（放置の判定に使う）--------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        チケットでの発言を覚えておく。

        ⚠️ 内容は読まない（MESSAGE CONTENT INTENT は要らない）。
           「いつ発言があったか」だけを記録する。
        """
        if message.author.bot or message.guild is None:
            return
        # ⚠️ ここは全メッセージを通る。先に、ほぼ必ず外れる条件で弾く。
        if not tickets.is_ticket_channel(message.channel.id):
            return
        if int(settings.get("ticket_auto_close_hours", 0) or 0) <= 0:
            return
        try:
            await tickets.touch(message.channel.id)
        except Exception:
            log.debug("チケットの発言記録に失敗", exc_info=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("ticket コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketCog(bot))
