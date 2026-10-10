"""
サーバー管理の画面（常設ボタン）

⚠️ custom_id は**永久に変えないこと**。
   変えると、すでに貼ってあるパネルのボタンが反応しなくなる。

⚠️ すべて timeout=None。時間切れにすると、再起動後に押せなくなる。
"""

from __future__ import annotations

import logging

import discord

import emoji as E
from ui import embeds
from ui.gate import GuardedView

log = logging.getLogger("bot.ui.server")


# ============================================================
#  チケット
# ============================================================

class TicketOpenModal(discord.ui.Modal):
    """
    問い合わせの最初の一言を聞く。

    ⚠️ ここで内容を聞いておくと、担当者が開いた時点で用件が分かる。
       空のチャンネルを作って「ご用件は？」と聞き直すより早く終わる。
    """

    def __init__(self, kind: str, kind_label: str) -> None:
        super().__init__(title=f"お問い合わせ：{kind_label}"[:45])
        self.kind = kind
        self.subject: discord.ui.TextInput = discord.ui.TextInput(
            label="件名（ひとことで）",
            placeholder="例）注文がエラーになる",
            max_length=100, required=True,
        )
        self.body: discord.ui.TextInput = discord.ui.TextInput(
            label="くわしい内容",
            style=discord.TextStyle.paragraph,
            placeholder="いつ・どの商品・どんな画面が出たか、分かる範囲でお書きください。",
            max_length=1500, required=False,
        )
        self.add_item(self.subject)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.server import tickets

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ticket, place, note = await tickets.create(
                interaction.guild, interaction.user,
                kind=self.kind, subject=str(self.subject.value),
            )
        except tickets.TicketError as e:
            await interaction.followup.send(
                embed=embeds.warn(str(e)), ephemeral=True,
            )
            return
        except Exception:
            log.exception("チケットを作れませんでした")
            await interaction.followup.send(
                embed=embeds.error(
                    "問い合わせの場所を作れませんでした。"
                    "管理者にお知らせください。"
                ),
                ephemeral=True,
            )
            return

        label = tickets.kind_of(self.kind)["label"]
        from core import settings

        ping = ""
        if tickets.staff_role_ids() and settings.get("ticket_ping_staff", True):
            ping = " ".join(f"<@&{r}>" for r in tickets.staff_role_ids())

        try:
            await place.send(
                content=f"<@{interaction.user.id}> {ping}".strip(),
                embed=embeds.ticket_opened(ticket, interaction.user.id, label),
                view=TicketControls(),
            )
            if str(self.body.value or "").strip():
                await place.send(
                    embed=embeds.info(
                        str(self.body.value), title="いただいた内容",
                    )
                )
        except discord.HTTPException:
            log.warning("チケット %s に案内を書けませんでした", ticket.number)

        msg = f"{E.OK} お問い合わせを受け付けました → <#{place.id}>"
        if note:
            msg += f"\n{E.WARN} 管理者の方へ：{note}"
        await interaction.followup.send(
            embed=embeds.ok(msg), ephemeral=True,
        )


class TicketKindSelect(discord.ui.Select):
    """種別を選ぶ。"""

    def __init__(self) -> None:
        from services.server import tickets

        options = [
            discord.SelectOption(
                label=k["label"][:100],
                value=k["key"][:100],
                description=(k.get("desc") or "")[:100] or None,
                emoji=k.get("emoji") or None,
            )
            for k in tickets.kinds()[:25]
        ] or [discord.SelectOption(label="お問い合わせ", value="other")]
        super().__init__(
            placeholder="ご相談の内容をお選びください",
            options=options, min_values=1, max_values=1,
            custom_id="ticket:kind",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from services.server import tickets

        if not tickets.enabled():
            await interaction.response.send_message(
                embed=embeds.warn("ただいま受け付けを停止しております。"),
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=embeds.warn("サーバー内でお使いください。"), ephemeral=True,
            )
            return
        key = self.values[0]
        await interaction.response.send_modal(
            TicketOpenModal(key, tickets.kind_of(key)["label"])
        )


class TicketPanel(GuardedView):
    """チャンネルに貼る問い合わせパネル。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketKindSelect())


class TicketCloseModal(discord.ui.Modal, title="この問い合わせを閉じます"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="終了の理由（任意・記録に残ります）",
        style=discord.TextStyle.paragraph,
        required=False, max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.server import tickets

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.channel.send(
                embed=embeds.info(
                    f"<@{interaction.user.id}> がこの問い合わせを終了しました。\n"
                    "やり取りの控えを残しています。ありがとうございました。"
                )
            )
        except discord.HTTPException:
            pass
        try:
            await tickets.close(
                interaction.client, interaction.channel,
                closed_by=interaction.user.id,
                reason=str(self.reason.value or ""),
            )
        except tickets.TicketError as e:
            await interaction.followup.send(
                embed=embeds.warn(str(e)), ephemeral=True,
            )
            return
        except Exception:
            log.exception("チケットを閉じられませんでした")
            await interaction.followup.send(
                embed=embeds.error("閉じられませんでした。管理者にお知らせください。"),
                ephemeral=True,
            )
            return
        # チャンネルは消えているので、ここで送れなくても問題ない
        try:
            await interaction.followup.send(
                embed=embeds.ok("終了しました。"), ephemeral=True,
            )
        except discord.HTTPException:
            pass


class TicketControls(GuardedView):
    """チケットの中に出すボタン。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="この問い合わせを閉じる", emoji="🔒",
        style=discord.ButtonStyle.danger, custom_id="ticket:close",
    )
    async def close(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        from services.server import tickets

        ticket = await tickets.by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.warn("ここはチケットではありません。"), ephemeral=True,
            )
            return
        # 開いた本人か、担当者なら閉じられる
        if interaction.user.id != ticket.opener_id and not tickets.is_staff(
            interaction.user
        ):
            await interaction.response.send_message(
                embed=embeds.warn("この問い合わせを閉じられるのは、"
                                  "開いたご本人と担当者だけです。"),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TicketCloseModal())

    @discord.ui.button(
        label="担当する", emoji="🙋",
        style=discord.ButtonStyle.primary, custom_id="ticket:claim",
    )
    async def claim(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        from services.server import tickets

        if not tickets.is_staff(interaction.user):
            await interaction.response.send_message(
                embed=embeds.warn("担当者の方だけが押せます。"), ephemeral=True,
            )
            return
        try:
            await tickets.claim(interaction.channel_id, interaction.user.id)
        except tickets.TicketError as e:
            await interaction.response.send_message(
                embed=embeds.warn(str(e)), ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=embeds.ok(f"<@{interaction.user.id}> が担当いたします。")
        )


# ============================================================
#  認証
# ============================================================

class CaptchaModal(discord.ui.Modal, title="画像の文字を入力してください"):
    answer: discord.ui.TextInput = discord.ui.TextInput(
        label="画像に書かれていた文字",
        placeholder="大文字・小文字はどちらでも構いません",
        max_length=16, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.server import verify

        await interaction.response.defer(ephemeral=True, thinking=True)
        got = verify.answer_matches(interaction.user.id, str(self.answer.value))

        if got is None:
            await interaction.followup.send(
                embed=embeds.warn(
                    "時間が経ちすぎたため、やり直しをお願いいたします。\n"
                    "もう一度ボタンを押してください。"
                ),
                ephemeral=True,
            )
            return

        if got is False:
            n = await verify.note_attempt(interaction.guild_id, interaction.user.id)
            left = await verify.attempts_left(interaction.guild_id, interaction.user.id)
            body = "文字が違うようです。"
            if left > 0:
                body += f"\nもう一度ボタンを押してお試しください（あと {left} 回）。"
            else:
                body += (
                    "\n失敗の回数が上限に達しました。"
                    "恐れ入りますが、管理者にお知らせください。"
                )
            await interaction.followup.send(
                embed=embeds.warn(body), ephemeral=True,
            )
            log.info("認証に失敗: %s（%d回目）", interaction.user, n)
            return

        await _finish_verify(interaction, method="captcha")


class CaptchaAnswerView(GuardedView):
    """画像と一緒に出す「入力する」ボタン（本人にだけ見える）。"""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="文字を入力する", emoji="⌨️", style=discord.ButtonStyle.primary,
    )
    async def open(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(CaptchaModal())


async def _finish_verify(interaction: discord.Interaction, *, method: str) -> None:
    """ロールを付けて、結果を伝える。"""
    from services.server import verify

    try:
        await verify.grant(interaction.user, method=method)
    except verify.VerifyError as e:
        await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
        return
    except Exception:
        log.exception("認証のロール付与に失敗")
        await interaction.followup.send(
            embed=embeds.error("認証できませんでした。管理者にお知らせください。"),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=embeds.ok("認証が完了しました。ごゆっくりお過ごしください。"),
        ephemeral=True,
    )

    # ようこそ案内を1回だけ送る（設定が切れていれば何もしない）
    #   ⚠️ 認証が済んだこの瞬間が、一番届く。
    #      入室直後だと、まだ中が見えていないので読まれない。
    try:
        from services import outreach

        await outreach.welcome(interaction.client, interaction.user)
    except Exception:
        log.warning("ようこそ案内を送れませんでした", exc_info=True)

    # 認証したことを記録先へ流す（設定されていなければ何もしない）
    from core import settings
    from services.server import logs

    cid = settings.get("verify_log_channel") or logs.log_channel_id()
    if not cid:
        return
    how = "画像認証" if method == "captcha" else "ボタン"
    try:
        channel = interaction.client.get_channel(int(cid))
        if channel is not None:
            await channel.send(embed=embeds.guard_log(
                title=f"{E.KEY} 認証されました",
                color=embeds.GREEN,
                lines=[
                    ("どなた", logs.who(interaction.user)),
                    ("方法", how),
                ],
            ))
    except discord.HTTPException:
        log.warning("認証の記録を送れませんでした")


class VerifyPanel(GuardedView):
    """チャンネルに貼る認証パネル。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="認証する", emoji="🔑",
        style=discord.ButtonStyle.success, custom_id="verify:start",
    )
    async def start(
        self, interaction: discord.Interaction, _: discord.ui.Button,
    ) -> None:
        from services.server import verify

        if not verify.enabled():
            await interaction.response.send_message(
                embed=embeds.warn("ただいま認証を受け付けておりません。"),
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=embeds.warn("サーバー内でお使いください。"), ephemeral=True,
            )
            return

        if await verify.already(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                embed=embeds.info("すでに認証がお済みです。"), ephemeral=True,
            )
            return

        short = verify.account_too_new(interaction.user)
        if short is not None:
            await interaction.response.send_message(
                embed=embeds.warn(
                    "恐れ入りますが、Discordアカウントを作成されてから"
                    f"**{verify.min_account_days()}日**以上たってからの"
                    "ご参加をお願いしております。\n"
                    f"あと **{short:.1f}日** お待ちください。"
                ),
                ephemeral=True,
            )
            return

        if await verify.attempts_left(interaction.guild_id, interaction.user.id) <= 0:
            await interaction.response.send_message(
                embed=embeds.warn(
                    "認証の失敗が続いたため、お受けできません。"
                    "管理者にお知らせください。"
                ),
                ephemeral=True,
            )
            return

        if verify.mode() == "captcha":
            code = verify.issue(interaction.user.id)
            await interaction.response.send_message(
                embed=embeds.info(
                    "画像に書かれている文字を、下のボタンから入力してください。\n"
                    "※ 5分で無効になります。"
                ),
                file=verify.render(code),
                view=CaptchaAnswerView(),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await _finish_verify(interaction, method="button")


# main.py の setup_hook が add_view() で復元する
PERSISTENT_VIEWS = [TicketPanel, TicketControls, VerifyPanel]
