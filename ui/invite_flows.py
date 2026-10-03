"""
招待キャンペーンの操作

利用者はパネルのボタンだけで操作する。
お金が動くので、画面に出す文言は「いくらもらえるか」「なぜ
もらえないか」をはっきり書く。黙って何も起きないのが一番困る。
"""

from __future__ import annotations

import logging

import discord

import config
import emoji as E
from core import invite as inv
from core import ledger as L
from core import settings
from core import users as user_repo
from db.models import Invite, User
from db.session import session_scope
from ui import embeds

log = logging.getLogger("bot.invite_flows")


async def show_my_code(interaction: discord.Interaction) -> None:
    """自分の招待コードと、いまの成果を本人にだけ見せる。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    await user_repo.get_or_create(interaction.user.id)

    if not inv.enabled():
        await interaction.followup.send(
            embed=embeds.info("いまは招待キャンペーンを行っていません。"),
            ephemeral=True,
        )
        return

    code = await inv.ensure_code(interaction.user.id)
    total = await inv.count_for(interaction.user.id)
    done = await inv.count_for(interaction.user.id, rewarded_only=True)
    limit = int(settings.get("invite_max_per_user", config.INVITE_MAX_PER_USER))

    e = discord.Embed(
        title=f"{E.KEY} あなたの招待コード",
        description=f"# `{code}`\nこの6文字をお友だちに伝えてください。",
        color=embeds.GREEN,
    )
    e.add_field(
        name=f"{E.USER} 紐づいた方",
        value=f"{total} 名" + (f" / {limit} 名まで" if limit else ""),
        inline=True,
    )
    e.add_field(name=f"{E.OK} 特典を受け取った分", value=f"{done} 名", inline=True)
    if total > done:
        cond = (
            "お友だちのはじめての注文をお待ちしています。"
            if inv.condition() == "first_order"
            else "まもなく反映されます。"
        )
        e.add_field(name=f"{E.LOADING} お待ちいただいている分",
                    value=f"{total - done} 名　{cond}", inline=False)
    e.set_footer(text="この画面はあなたにしか見えていません")
    await interaction.followup.send(embed=e, ephemeral=True)


class CodeModal(discord.ui.Modal, title="招待コードの入力"):
    code = discord.ui.TextInput(
        label="6文字の招待コード",
        placeholder="例: A3F9KM",
        required=True, min_length=4, max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await user_repo.get_or_create(interaction.user.id)
        await finish_link(interaction, str(self.code.value), source="code")


async def open_code_modal(interaction: discord.Interaction) -> None:
    if not inv.enabled():
        await interaction.response.send_message(
            embed=embeds.info("いまは招待キャンペーンを行っていません。"),
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(CodeModal())


async def finish_link(
    interaction: discord.Interaction, code: str, *, source: str
) -> None:
    """コードを確かめて紐づけ、条件を満たしていれば特典を渡す。"""
    inviter_id = await inv.find_inviter(code)
    if inviter_id is None:
        await interaction.followup.send(
            embed=embeds.error(
                "その招待コードは見つかりませんでした。\n"
                "6文字をもう一度ご確認ください。"
            ),
            ephemeral=True,
        )
        return

    try:
        await inv.link(interaction.user.id, inviter_id, source=source)
    except inv.InviteError as e:
        await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
        return
    except Exception:
        log.exception("招待の紐づけに失敗しました")
        await interaction.followup.send(
            embed=embeds.error("登録できませんでした。管理者にお問い合わせください。"),
            ephemeral=True,
        )
        return

    paid = await inv.grant_if_ready(interaction.user.id)
    await _tell_result(interaction, inviter_id, paid)
    await announce(interaction.client, interaction.user.id, inviter_id, paid)


async def _tell_result(
    interaction: discord.Interaction, inviter_id: int, paid: list[tuple[int, int]]
) -> None:
    mine = sum(amount for uid, amount in paid if uid == interaction.user.id)
    e = discord.Embed(
        title=f"{E.PARTY} 招待コードを登録しました",
        color=embeds.GREEN,
    )
    if mine:
        async with session_scope() as s:
            balance = await L.user_balance(s, interaction.user.id)
        e.description = (
            f"**{embeds.yen(mine)}** を残高に追加しました。\n"
            f"現在の残高: **{embeds.yen(balance)}**"
        )
    elif inv.condition() == "first_order":
        e.description = (
            "ようこそ！\n"
            "はじめてのご注文が済むと、招待してくださった方に特典が入ります。"
        )
    else:
        e.description = "ようこそ！"
    await interaction.followup.send(embed=e, ephemeral=True)


async def announce(
    client: discord.Client, invitee_id: int, inviter_id: int,
    paid: list[tuple[int, int]],
) -> None:
    """
    招待が成立したことを知らせる。

    ・招待した人へDM（残高が増えたことは本人に伝わるべき）
    ・設定されていれば、お祝いチャンネルへも投稿する
    """
    amount = sum(a for uid, a in paid if uid == inviter_id)
    if amount:
        try:
            user = client.get_user(inviter_id) or await client.fetch_user(inviter_id)
            await user.send(
                embed=embeds.ok(
                    f"{E.PARTY} 招待が成立しました。\n"
                    f"**{embeds.yen(amount)}** を残高に追加しました。",
                )
            )
        except (discord.HTTPException, AttributeError):
            log.info("招待の成立を招待者へDMできませんでした（%s）", inviter_id)

    channel_id = settings.get("channel_invite")
    if not channel_id or not amount:
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return
    # ⚠️ 誰が誰を招待したかは個人のつながりなので、名前は出さない。
    #    盛り上がりだけ共有する。
    st = await inv.stats()
    try:
        await channel.send(
            embed=discord.Embed(
                title=f"{E.PARTY} 招待が成立しました",
                description=f"これまでに **{st.rewarded} 件** 成立しています。",
                color=embeds.GREEN,
            )
        )
    except discord.HTTPException:
        log.exception("招待の成立を投稿できませんでした")


async def show_status(interaction: discord.Interaction) -> None:
    """キャンペーンの状況。誰が見ても同じものを出す（個人名は出さない）。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    st = await inv.stats()
    e = discord.Embed(title=f"{E.CHART} 招待キャンペーンの状況", color=embeds.BLUE)
    if not inv.enabled():
        e.description = "いまは開催していません。"
        await interaction.followup.send(embed=e, ephemeral=True)
        return

    e.add_field(name=f"{E.OK} 成立した招待", value=f"{st.rewarded} 件", inline=True)
    if st.pending:
        e.add_field(name=f"{E.LOADING} 条件待ち", value=f"{st.pending} 件", inline=True)
    left = st.budget_left
    if left >= 0:
        e.add_field(
            name=f"{E.YEN} のこり",
            value=f"**{embeds.yen(left)}** 分",
            inline=True,
        )
        if left < inv.reward_amount():
            e.set_footer(text="まもなく終了します")

    top = await inv.ranking(5)
    if top:
        # 匿名コードで出す。誰が誰を誘ったかは本人の交友関係なので伏せる。
        lines = []
        async with session_scope() as s:
            for i, (uid, n) in enumerate(top, 1):
                row = await s.get(User, uid)
                tag = row.anon_code if row else user_repo.anon_code(uid)
                mark = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
                lines.append(f"{mark} `{tag}`　{n} 名")
        e.add_field(name=f"{E.CHART} ランキング", value="\n".join(lines), inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)
