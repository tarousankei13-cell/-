"""
残高の増減パネル

チャージや注文で残高が動いたとき、**その操作をしたチャンネル**に
誰にでも見える形で出す。注文の操作そのものは本人にしか見えないが、
「誰がいくら使ったか」は共有の財布として見えていたほうが分かりやすい、
という考え方。

⚠️ お金の話を知られたくない人もいる。何を出すかは管理者が選べる
   （`/config balance_panel`）。既定では**残高そのものは出さない**。
   いくら持っているかは、増減額よりもずっと知られたくない情報。

送り先の決め方:
  1. `/config channel balance` が設定されていれば、そのチャンネル
  2. 設定が無ければ、操作したチャンネル（＝利用者の求めた挙動）
  3. DM での操作で 1 も無ければ、何もしない
     （DMで「誰にでも見える」は意味を持たないため）
"""

from __future__ import annotations

import logging

import discord

import config
from core import settings
from core import users as user_repo
from db.models import User
from db.session import session_scope
from ui import embeds

log = logging.getLogger("bot.balance_panel")

# 理由の文言。呼び出し側がばらばらに書くと見た目が揃わないので定数にする。
REASON_CHARGE = "Kyash でチャージ"
REASON_ORDER = "マクドナルドの注文"
REASON_GRANT = "管理者による調整"
REASON_REFUND = "注文の取り消しによる返金"


def enabled() -> bool:
    return bool(settings.get("balance_panel", True))


def fields() -> list[str]:
    v = settings.get("balance_panel_fields", config.BALANCE_PANEL_FIELDS_DEFAULT)
    return list(v) if v else []


def _destination(interaction: discord.Interaction) -> discord.abc.Messageable | None:
    """どこへ出すかを決める。出せないときは None。"""
    configured = settings.get("channel_balance")
    if configured:
        ch = interaction.client.get_channel(int(configured))
        if ch is not None:
            return ch
        # 設定が消えたチャンネルを指している場合は、操作した場所へ落とす
        log.warning("残高パネルの送信先 %s が見つかりません", configured)

    channel = interaction.channel
    if channel is None:
        return None
    # DM・グループDMでは「誰にでも見える」が成り立たないので出さない
    if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
        return None
    if getattr(interaction, "guild", None) is None:
        return None
    return channel


def _can_send(channel: discord.abc.Messageable, guild: discord.Guild | None) -> bool:
    """送れるかどうかを先に確かめる。例外で気づくより静かで速い。"""
    me = getattr(guild, "me", None)
    perms_for = getattr(channel, "permissions_for", None)
    if me is None or perms_for is None:
        return True  # 判定できない環境（テスト等）では止めない
    try:
        p = perms_for(me)
    except Exception:
        return True
    return bool(p.send_messages and p.embed_links)


async def post(
    interaction: discord.Interaction,
    *,
    amount: int,
    balance: int,
    reason: str,
    display_name: str | None = None,
    discord_id: int | None = None,
    total_orders: int = 0,
) -> bool:
    """
    残高の増減を公開パネルで出す。

    amount は増加なら正、減少なら負。
    投稿できたら True。設定で切られている・権限が無い・DM だった、
    といった場合は False を返すだけで、呼び出し側の処理は止めない。

    ⚠️ このパネルは飾りで、注文やチャージの成否とは関係ない。
       ここで何が起きても例外を外へ出さない。
       パネルが出ないせいで注文が失敗したら本末転倒。
    """
    try:
        return await _post(
            interaction, amount=amount, balance=balance, reason=reason,
            display_name=display_name, discord_id=discord_id,
            total_orders=total_orders,
        )
    except Exception:
        log.exception("残高パネルの処理で予期しないエラー")
        return False


async def _post(
    interaction: discord.Interaction,
    *,
    amount: int,
    balance: int,
    reason: str,
    display_name: str | None,
    discord_id: int | None,
    total_orders: int,
) -> bool:
    if not enabled() or amount == 0:
        return False

    channel = _destination(interaction)
    if channel is None:
        return False
    if not _can_send(channel, getattr(interaction, "guild", None)):
        log.info("残高パネルを送る権限がありません（#%s）", getattr(channel, "name", "?"))
        return False

    uid = discord_id if discord_id is not None else interaction.user.id
    if display_name is None and discord_id in (None, interaction.user.id):
        display_name = interaction.user.display_name

    async with session_scope() as s:
        user = await s.get(User, uid)
    anon = user.anon_code if user else user_repo.anon_code(uid)

    embed = embeds.balance_change(
        display_name=display_name,
        anon_code=anon,
        amount=amount,
        balance=balance,
        reason=reason,
        fields=fields(),
        total_orders=total_orders,
    )
    try:
        await channel.send(embed=embed)
        return True
    except discord.Forbidden:
        log.info("残高パネルを送れませんでした（権限不足）")
        return False
    except discord.HTTPException:
        log.exception("残高パネルの送信に失敗しました")
        return False
