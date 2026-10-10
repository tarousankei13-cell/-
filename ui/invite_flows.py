"""
紹介プログラムの操作

利用者はパネルのボタンだけで操作する。
お金が動くので、画面に出す文言は「いくらもらえるか」「なぜ
もらえないか」をはっきり書く。黙って何も起きないのが一番困る。

流れ:
  ① 招待リンクを発行   BOT が本人に代わって Discord の招待を作る
  ② 友だちが参加／コード入力
  ③ 友だちの DM に受取ボタンが届く（押して初めて紐づく）
  ④ 友だちが条件を満たす注文を終える → 達成1名
  ⑤ 達成が所定の人数たまるたびに、紹介者へ特典
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord

import emoji as E
from core import invite as inv
from core import ledger as L
from core import settings
from core import users as user_repo
from db.session import session_scope
from ui import embeds
from ui.gate import GuardedView

log = logging.getLogger("bot.invite_flows")


def _closed() -> discord.Embed:
    return embeds.warn(
        "ただいま紹介プログラムは行っておりません。\n"
        "次回の開催までお待ちください。"
    )


def _reward_line() -> str:
    """「2名様 注文完了ごとに ¥500」のような一行。"""
    every = inv.reward_every()
    amount = inv.reward_amount()
    if every <= 1:
        return f"ご紹介の方が注文を完了されるごとに、**{embeds.yen(amount)}**"
    return (
        f"ご紹介の方が **{every}名様** 注文を完了されるごとに、"
        f"**{embeds.yen(amount)}**"
    )


def _conditions() -> list[str]:
    """被招待者に求める条件。設定が0なら出さない。"""
    out = []
    if inv.min_account_days():
        out.append(f"Discord アカウント作成から **{inv.min_account_days()}日以上**")
    if inv.min_member_hours():
        out.append(
            f"サーバー参加から **{inv.min_member_hours()}時間以上**（手動入力時）"
        )
    out.append("ご自身の招待コードはご利用いただけません")
    return out


# ============================================================
#  ① 招待リンクを発行
# ============================================================

async def issue_link(interaction: discord.Interaction) -> None:
    """
    本人専用の招待リンクを作る（1サーバーにつき1本）。

    ⚠️ Discord 側では「BOT が作った招待」になるので、
       誰のものかは invite_links に控えておく。これが無いと、
       参加してきた人を紹介者に結び付けられない。
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not inv.enabled():
        await interaction.followup.send(embed=_closed(), ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            embed=embeds.warn("サーバーの中からお試しください。"), ephemeral=True
        )
        return

    await user_repo.get_or_create(interaction.user.id)

    channel_id = inv.link_channel_id()
    if not channel_id:
        await interaction.followup.send(
            embed=embeds.warn(
                "招待リンクの発行先が設定されていません。\n"
                "お手数ですが、管理者へお知らせください。\n"
                "（管理者の方へ: `/config campaign channel` でご指定ください）"
            ),
            ephemeral=True,
        )
        return

    # ⚠️ 「招待を作れる場所か」で判定する。種類で判定すると、
    #    カテゴリやフォーラムのように作れない場所を取りこぼす。
    channel = guild.get_channel(int(channel_id))
    if channel is None or not hasattr(channel, "create_invite"):
        await interaction.followup.send(
            embed=embeds.warn(
                "招待リンクの発行先チャンネルが見つかりませんでした。\n"
                "お手数ですが、管理者へお知らせください。"
            ),
            ephemeral=True,
        )
        return

    # ---- すでに持っていれば、それをもう一度見せる ----
    existing = await inv.my_link(interaction.user.id, guild.id)
    if existing is not None:
        alive = await _still_alive(guild, existing.code)
        if alive:
            await interaction.followup.send(
                embed=_link_embed(guild, existing.code, existing.expires_at),
                ephemeral=True,
            )
            return
        # Discord 側で消えていた → 控えも消して作り直す
        await inv.forget_link(existing.code)

    days = inv.link_days()
    try:
        invite = await channel.create_invite(
            max_age=days * 86400,
            max_uses=0,
            unique=True,
            reason=f"紹介プログラム: {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=embeds.warn(
                "招待リンクを作る権限がありませんでした。\n"
                "お手数ですが、管理者へお知らせください。\n"
                "（管理者の方へ: BOT に「招待を作成」の権限をお与えください）"
            ),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        log.exception("招待リンクを作れませんでした")
        await interaction.followup.send(
            embed=embeds.warn(
                "招待リンクを作れませんでした。少し時間をおいてお試しください。"
            ),
            ephemeral=True,
        )
        return

    expires = datetime.now(timezone.utc) + timedelta(days=days)
    await inv.save_link(
        invite.code, guild_id=guild.id, discord_id=interaction.user.id,
        channel_id=channel.id, expires_at=expires,
    )
    await interaction.followup.send(
        embed=_link_embed(guild, invite.code, expires), ephemeral=True
    )


async def _still_alive(guild: discord.Guild, code: str) -> bool:
    """その招待が Discord 側にまだあるか。確かめられなければ「ある」とみなす。"""
    try:
        return any(i.code == code for i in await guild.invites())
    except (discord.Forbidden, discord.HTTPException):
        return True


def _link_embed(
    guild: discord.Guild, code: str, expires_at: datetime | None
) -> discord.Embed:
    e = discord.Embed(
        title=f"{E.CHARGE} 招待リンクを発行しました",
        description=(
            f"**{guild.name}** 用の招待リンクです。\n"
            "このリンクから参加された方には、**自動的にあなたのプロモーション"
            "コードが適用**されます。\n"
            "（自動の場合も、ご友人が DM の受取ボタンを押す必要があります）"
        ),
        color=embeds.GREEN,
    )
    e.add_field(name="サーバー", value=guild.name, inline=False)
    e.add_field(name="コード", value=f"`{code}`", inline=True)
    if expires_at is not None:
        stamp = int(expires_at.timestamp())
        e.add_field(
            name="有効期限",
            value=f"`{inv.link_days()} 日`（<t:{stamp}:R>）",
            inline=True,
        )
    e.add_field(
        name="招待URL", value=f"https://discord.gg/{code}", inline=False
    )
    e.add_field(
        name=f"{E.GIFT} 報酬",
        value=(
            f"・{_reward_line()} があなたの残高に加算されます\n"
            f"・{inv.reward_every()}名様ごとに**繰り返し**発生します"
            + (
                f"\n・ご紹介の方の1回目のご注文は、定価 "
                f"**{embeds.yen(inv.min_order())}以上** が対象です"
                if inv.min_order() else ""
            )
        ),
        inline=False,
    )
    e.add_field(
        name=f"{E.UNLOCK} コードを使う側の条件",
        value="・" + "\n・".join(_conditions()),
        inline=False,
    )
    e.set_footer(text="1人1リンクまで（サーバーごと）")
    return e


# ============================================================
#  ② プロモコードの手入力
# ============================================================

class CodeModal(discord.ui.Modal, title="プロモコードの入力"):
    code = discord.ui.TextInput(
        label="招待コード",
        placeholder="例: vsqvB5wp / A7K2MX / https://discord.gg/xxxx",
        min_length=4, max_length=120, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await finish_link(interaction, str(self.code.value), source="code")


async def open_code_modal(interaction: discord.Interaction) -> None:
    if not inv.enabled():
        await interaction.response.send_message(embed=_closed(), ephemeral=True)
        return
    await interaction.response.send_modal(CodeModal())


async def finish_link(
    interaction: discord.Interaction, code: str, *, source: str = "code"
) -> None:
    """入力されたコードで紐づける。失敗の理由はそのまま見せる。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    await user_repo.get_or_create(interaction.user.id)

    inviter_id = await inv.find_inviter(code)
    if inviter_id is None:
        await interaction.followup.send(
            embed=embeds.warn(
                "そのコードは見つかりませんでした。\n"
                "入力に誤りがないか、ご確認ください。"
            ),
            ephemeral=True,
        )
        return

    member = interaction.user
    try:
        inv.check_eligibility(
            account_created=getattr(member, "created_at", None),
            joined_at=getattr(member, "joined_at", None),
            manual=(source == "code"),
        )
        await inv.link(
            interaction.user.id, inviter_id, source=source,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
    except inv.InviteError as e:
        await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
        return

    sent = await send_claim_dm(interaction.client, interaction.user.id, inviter_id)
    e = discord.Embed(
        title=f"{E.OK} プロモコードを登録しました",
        description=(
            "DM に受取ボタンをお送りしました。\n"
            "**ボタンを押していただくと有効になります。**"
            if sent else
            "DM をお送りできませんでした。\n"
            "下のボタンから、この場でお受け取りください。"
        ),
        color=embeds.GREEN,
    )
    await interaction.followup.send(
        embed=e, view=None if sent else ClaimView(), ephemeral=True
    )


# ============================================================
#  ③ DM の受取ボタン
# ============================================================

class ClaimView(GuardedView):
    """
    招待された方が押す受取ボタン。

    ⚠️ 押すまで人数に入らない。知らないうちに誰かの実績にされることを
       防ぐための、本人の意思表示。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="プロモコードを受け取る", emoji=E.GIFT,
        style=discord.ButtonStyle.success, custom_id="invite:claim",
    )
    async def claim(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            inviter_id = await inv.claim(interaction.user.id)
        except inv.InviteError as e:
            await interaction.followup.send(embed=embeds.warn(str(e)), ephemeral=True)
            return

        # 条件が「参加しただけ」なら、ここで達成になることがある
        paid = await inv.grant_if_ready(interaction.user.id)
        if paid and inviter_id:
            await announce(interaction.client, interaction.user.id, inviter_id, paid)

        low = inv.min_order()
        nxt = (
            f"はじめてのご注文（定価 **{embeds.yen(low)}以上**）が済むと、"
            "ご紹介いただいた方の実績になります。"
            if low else
            "はじめてのご注文が済むと、ご紹介いただいた方の実績になります。"
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.PARTY} プロモコードを受け取りました",
                description=f"ようこそ！\n{nxt}",
                color=embeds.GREEN,
            ),
            ephemeral=True,
        )


async def send_claim_dm(
    client: discord.Client, invitee_id: int, inviter_id: int
) -> bool:
    """受取ボタン付きのDMを送る。送れたかどうかを返す。"""
    try:
        user = client.get_user(invitee_id) or await client.fetch_user(invitee_id)
        e = discord.Embed(
            title=f"{E.GIFT} プロモコードのお受け取り",
            description=(
                "ご紹介のプロモコードが適用されました。\n"
                "下のボタンを押して、お受け取りください。"
            ),
            color=embeds.GREEN,
        )
        e.add_field(
            name=f"{E.INFO} このあと",
            value=(
                f"はじめてのご注文"
                + (f"（定価 {embeds.yen(inv.min_order())}以上）" if inv.min_order() else "")
                + "が済むと、ご紹介いただいた方の実績になります。"
            ),
            inline=False,
        )
        await user.send(embed=e, view=ClaimView())
        return True
    except (discord.HTTPException, AttributeError):
        log.info("受取ボタンのDMを送れませんでした（%s）", invitee_id)
        return False


async def resend_dm(interaction: discord.Interaction) -> None:
    """DM を受け取れなかった方のために、もう一度送る。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    inviter_id = None
    claimed = False
    async with session_scope() as s:
        from db.models import Invite

        row = await s.get(Invite, interaction.user.id)
        if row is not None:
            inviter_id = int(row.inviter_id)
            claimed = bool(row.claimed)
    if inviter_id is None:
        await interaction.followup.send(
            embed=embeds.warn(
                "お受け取りいただく紹介が見つかりませんでした。\n"
                f"先に「{E.TICKET} プロモコード入力」からコードをご登録ください。"
            ),
            ephemeral=True,
        )
        return
    if claimed:
        await interaction.followup.send(
            embed=embeds.ok("すでにお受け取りいただいています。"), ephemeral=True
        )
        return

    sent = await send_claim_dm(interaction.client, interaction.user.id, inviter_id)
    if sent:
        await interaction.followup.send(
            embed=embeds.ok("DM をお送りしました。ご確認ください。"), ephemeral=True
        )
    else:
        await interaction.followup.send(
            embed=embeds.warn(
                "DM をお送りできませんでした。\n"
                "サーバーの設定で DM を受け取れない可能性があります。\n"
                "下のボタンから、この場でお受け取りください。"
            ),
            view=ClaimView(),
            ephemeral=True,
        )


# ============================================================
#  ④ 紹介状況・通知設定
# ============================================================

async def show_status(interaction: discord.Interaction) -> None:
    """自分の紹介状況。個人名は出さない。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not inv.enabled():
        await interaction.followup.send(embed=_closed(), ephemeral=True)
        return

    uid = interaction.user.id
    await user_repo.get_or_create(uid)
    linked = await inv.count_for(uid)
    reached = await inv.reached_count(uid)
    earned = await inv.earned_by(uid)
    every = inv.reward_every()
    remain = (every - (reached % every)) % every

    e = discord.Embed(title=f"{E.CHART} あなたの紹介状況", color=embeds.BLUE)
    e.add_field(name="ご紹介いただいた方", value=f"**{linked}** 名", inline=True)
    e.add_field(name="達成（注文完了）", value=f"**{reached}** 名", inline=True)
    e.add_field(name="受け取った特典", value=f"**{embeds.yen(earned)}**", inline=True)
    if remain:
        e.add_field(
            name=f"{E.GIFT} 次の特典まで",
            value=f"あと **{remain}** 名で **{embeds.yen(inv.reward_amount())}**",
            inline=False,
        )
    else:
        e.add_field(
            name=f"{E.GIFT} 次の特典まで",
            value=f"あと **{every}** 名で **{embeds.yen(inv.reward_amount())}**",
            inline=False,
        )
    limit = inv.max_per_user()
    if limit:
        e.set_footer(text=f"お一人あたり {limit} 名までが特典の対象です")
    await interaction.followup.send(embed=e, ephemeral=True)


async def toggle_notify(interaction: discord.Interaction) -> None:
    """紹介のお知らせを受け取るかどうかを切り替える。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    await user_repo.get_or_create(interaction.user.id)
    now = await inv.notify_enabled(interaction.user.id)
    after = await inv.set_notify(interaction.user.id, not now)
    await interaction.followup.send(
        embed=embeds.ok(
            f"{E.BELL} 紹介のお知らせを **受け取る** ようにしました。\n"
            "ご紹介が成立したときと、特典が入ったときに DM でお知らせします。"
            if after else
            f"{E.BELL} 紹介のお知らせを **受け取らない** ようにしました。\n"
            "特典は、お知らせの有無にかかわらず残高に入ります。"
        ),
        ephemeral=True,
    )


# ============================================================
#  お知らせ
# ============================================================

async def announce(
    client: discord.Client, invitee_id: int, inviter_id: int,
    paid: list[tuple[int, int]],
) -> None:
    """
    特典が入ったことを知らせる。

    ・紹介した方へDM（残高が増えたことは本人に伝わるべき）
    ・設定されていれば、お祝いチャンネルへも投稿する
    """
    amount = sum(a for uid, a in paid if uid == inviter_id)
    if amount and await inv.notify_enabled(inviter_id):
        try:
            user = client.get_user(inviter_id) or await client.fetch_user(inviter_id)
            reached = await inv.reached_count(inviter_id)
            await user.send(
                embed=discord.Embed(
                    title=f"{E.PARTY} 紹介の特典が入りました",
                    description=(
                        f"**{embeds.yen(amount)}** を残高に追加しました。\n"
                        f"これまでの達成: **{reached}** 名"
                    ),
                    color=embeds.GREEN,
                )
            )
        except (discord.HTTPException, AttributeError):
            log.info("特典の案内を紹介者へDMできませんでした（%s）", inviter_id)

    channel_id = settings.get("channel_invite")
    if not channel_id or not amount:
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return
    # ⚠️ 誰が誰を紹介したかは個人のつながりなので、名前は出さない。
    #    盛り上がりだけ共有する。
    st = await inv.stats()
    try:
        await channel.send(
            embed=discord.Embed(
                title=f"{E.PARTY} 紹介の特典が成立しました",
                description=f"これまでに **{st.rewarded} 件** 成立しています。",
                color=embeds.GREEN,
            )
        )
    except discord.HTTPException:
        log.exception("紹介の成立を投稿できませんでした")
