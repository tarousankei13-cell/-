"""
関所 — 貸していないサーバーでは、すべての操作を止める

⚠️ **1か所に集める。** コマンドごと・画面ごとに判定を書くと、
   必ずどこかで書き忘れ、そこだけ無料で使われる。

通り道は2つしかない。

  ① スラッシュコマンド → `LicensedTree.interaction_check`
  ② ボタン・選択肢     → `GuardedView.interaction_check`

⚠️ `discord.ui.View` を直に継承すると①②のどちらも通らない。
   `GuardedView` を継承すること。守られていない View が増えていないかは
   `tests/test_lend.py` が毎回数えて止める。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

import config
from core import license as lic
from ui import embeds
from ui.embeds import E

log = logging.getLogger("bot.gate")


def blocked_embed(st: lic.Status) -> discord.Embed:
    """止まっている理由を見せる。"""
    e = discord.Embed(
        title=f"{E.NG} このサーバーではご利用いただけません",
        description=st.message(),
        color=embeds.RED,
    )
    if st.reason == lic.EXPIRED:
        e.set_footer(text="期限を延長すると、そのまま続きからお使いいただけます。")
    return e


async def check(interaction: discord.Interaction) -> bool:
    """使ってよいか。駄目なら理由を出して False。

    ⚠️ 応答を**必ず**返す。黙って False を返すと、利用者には
       「BOTは時間内に応答しませんでした」としか見えない。
    """
    # ⚠️ getattr で読む。関所が AttributeError で落ちると、
    #    止めるはずの場面で例外になり、何が起きたか誰にも分からなくなる。
    st = await lic.status(getattr(interaction, "guild_id", None))
    if st.allowed:
        return True

    e = blocked_embed(st)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
    except discord.HTTPException:
        log.debug("関所の案内を送れませんでした", exc_info=True)
    return False


class LicensedTree(app_commands.CommandTree):
    """すべてのスラッシュコマンドの入口。

    ⚠️ ここを通らないコマンドは存在しない。コマンドを足すたびに
       判定を書く必要がない作りにしてある。
    """

    # ⚠️ ホームが決まる前でも打てないと詰むコマンド。
    #    貸し出しの管理そのものと、最低限の保守。
    ALWAYS: frozenset[str] = frozenset({
        "lend", "sync", "restart",
    })

    def _is_always(self, command) -> bool:
        name = getattr(command, "qualified_name", "") or ""
        head = name.split(" ")[0]
        return head in self.ALWAYS

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cmd = interaction.command
        if cmd is not None and self._is_always(cmd):
            # ⚠️ 素通しにするのは「貸し出しの管理」だけ。
            #    しかもオーナーしか打てないことは各コマンド側で見ている。
            return True
        return await check(interaction)


class GuardedView(discord.ui.View):
    """関所を通る View。**すべての画面はこれを継承する。**

    画面ごとの決まり（開いた本人だけ、など）は `allow()` に書く。
    `interaction_check` は関所のために予約されている。

    ⚠️ `interaction_check` を上書きしないこと。上書きすると関所を
       素通りする。守るべき決まりは `allow()` に書く。
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("timeout", config.VIEW_TIMEOUT)
        super().__init__(*args, **kwargs)

    async def allow(self, interaction: discord.Interaction) -> bool:
        """画面ごとの決まり。既定は誰でも可。"""
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await check(interaction):
            return False
        return await self.allow(interaction)


class OwnerOnlyView(GuardedView):
    """開いた本人だけが押せる画面。よくある形なのでまとめてある。"""

    def __init__(self, owner_id: int, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.owner_id = int(owner_id)

    async def allow(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"),
                ephemeral=True,
            )
            return False
        return True
