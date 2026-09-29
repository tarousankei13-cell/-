"""cog 間で使い回す小物。

ファイル名が _ で始まるので main.py の自動ロード対象にはならない。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import discord
from discord import app_commands

log = logging.getLogger("bot.ui")

OK = discord.Color.from_rgb(32, 150, 100)
WARN = discord.Color.from_rgb(220, 150, 30)
BAD = discord.Color.from_rgb(200, 60, 55)
INFO = discord.Color.from_rgb(70, 110, 200)
MONEY = discord.Color.from_rgb(180, 140, 40)


def yen(value: int | float) -> str:
    return f"{int(value):,} 円"


def embed(
    title: str,
    description: str = "",
    color: discord.Color = INFO,
    footer: str = "",
) -> discord.Embed:
    e = discord.Embed(title=title, description=description or None, color=color)
    if footer:
        e.set_footer(text=footer)
    return e


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return await interaction.client.is_owner(interaction.user)

    return app_commands.check(predicate)


def is_owner_id(bot: Any, user_id: int) -> bool:
    ids = bot.owner_ids or set()
    return user_id in ids or user_id == getattr(bot, "owner_id", None)


def has_order_role(user: discord.abc.User, role_id: int) -> bool:
    if not role_id:
        return True
    roles = getattr(user, "roles", None)
    if roles is None:
        return False
    return any(r.id == role_id for r in roles)


def role_ids(user: discord.abc.User) -> set[int]:
    return {r.id for r in getattr(user, "roles", [])}


async def reply(
    interaction: discord.Interaction,
    e: discord.Embed,
    *,
    ephemeral: bool = True,
    view: Optional[discord.ui.View] = None,
    files: Optional[list[discord.File]] = None,
) -> None:
    kwargs: dict[str, Any] = {"embed": e, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if files:
        kwargs["files"] = files
    if interaction.response.is_done():
        kwargs.pop("ephemeral", None)
        await interaction.followup.send(**kwargs, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(**kwargs)


async def deny(interaction: discord.Interaction, title: str, description: str = "") -> None:
    await reply(interaction, embed(title, description, BAD), ephemeral=True)


async def dm(
    bot: Any,
    user_id: int,
    *,
    content: str = "",
    e: Optional[discord.Embed] = None,
    files: Optional[list[discord.File]] = None,
    view: Optional[discord.ui.View] = None,
) -> Optional[discord.Message]:
    """DM を送る。拒否設定などで失敗したら None を返す（例外は投げない）。"""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        kwargs: dict[str, Any] = {}
        if content:
            kwargs["content"] = content
        if e is not None:
            kwargs["embed"] = e
        if files:
            kwargs["files"] = files
        if view is not None:
            kwargs["view"] = view
        return await user.send(**kwargs)
    except discord.Forbidden:
        log.info("DM を拒否されました (user=%s)", user_id)
    except Exception:
        log.exception("DM の送信に失敗しました (user=%s)", user_id)
    return None


async def fetch_channel(bot: Any, channel_id: int):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        log.warning("チャンネル %s を取得できませんでした", channel_id)
        return None


async def post(bot: Any, channel_id: int, **kwargs) -> Optional[discord.Message]:
    channel = await fetch_channel(bot, channel_id)
    if channel is None:
        return None
    try:
        return await channel.send(**kwargs)
    except Exception:
        log.exception("投稿に失敗しました (channel=%s)", channel_id)
        return None


def field_rows(e: discord.Embed, rows: Iterable[tuple[str, str, bool]]) -> discord.Embed:
    for name, value, inline in rows:
        e.add_field(name=name, value=value or "-", inline=inline)
    return e


def queue_line(pool: Any) -> str:
    """待ち行列の一行表示（機能6）。"""
    inflight = getattr(pool, "inflight", 0)
    waiting = getattr(pool, "waiting", 0)
    accounts = pool.available_count()
    if accounts == 0:
        return "現在ご利用いただけません（アカウント未設定）"
    if inflight == 0 and waiting == 0:
        return f"待ちなし（使用可能アカウント {accounts} 件）"
    estimate = max(1, (waiting + max(0, inflight - accounts)) * 15)
    return (
        f"処理中 {inflight} 件 / 順番待ち {waiting} 人"
        f"（目安 約 {estimate} 秒 ・ 使用可能アカウント {accounts} 件）"
    )


class ConfirmView(discord.ui.View):
    """はい / いいえ だけの汎用ビュー。押した人しか操作できない。"""

    def __init__(self, user_id: int, *, timeout: float = 120, yes_label: str = "はい",
                 no_label: str = "キャンセル", yes_style: discord.ButtonStyle = discord.ButtonStyle.success):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value: Optional[bool] = None
        self.interaction: Optional[discord.Interaction] = None
        self._yes.label = yes_label
        self._yes.style = yes_style
        self._no.label = no_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この操作はコマンドを実行した本人だけが行えます。", ephemeral=True
            )
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="はい", style=discord.ButtonStyle.success)
    async def _yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        self.interaction = interaction
        self._disable()
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def _no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        self.interaction = interaction
        self._disable()
        self.stop()
