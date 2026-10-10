"""コマンドの案内（/help）

⚠️ コマンドは 149 個（`/help` 自身を除くと 148 個）ある。
   一覧をそのまま出しても読めないし、Discord のコマンド一覧も
   18 の入口が並ぶだけで「何をしたいときどれを打つのか」が
   分からない。

   そこで **やりたいこと順** に並べ、**その人が使えるものだけ** 出す。

⚠️ 一覧を手で書き写さない。コマンドが増減したときに必ずずれる。
   **実際に登録されているコマンドから作る**（`walk_app_commands`）。
   目的の仕分けだけを表で持ち、表に無いものは「その他」へ落とす。
   こうすれば、新しいコマンドを足しても `/help` から消えない。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import emoji as E
from cogs._checks import is_admin_user
from ui import embeds

log = logging.getLogger("bot.cogs.help")


# 利用者の区分
EVERYONE = "everyone"
ADMIN = "admin"
OWNER = "owner"


class Topic:
    """案内の1かたまり。"""

    def __init__(self, key: str, emoji: str, title: str, hint: str,
                 level: str, prefixes: tuple[str, ...]) -> None:
        self.key = key
        self.emoji = emoji
        self.title = title
        self.hint = hint
        self.level = level
        self.prefixes = prefixes


# ⚠️ 並び順がそのまま画面の順になる。**使う頻度の高いものを上に。**
#    毎日使うものを下に置くと、毎回スクロールさせることになる。
TOPICS: list[Topic] = [
    Topic("order", E.BURGER, "注文・残高",
          "利用者はパネルのボタンで操作します。ここは管理者が代わりに動かすとき用。",
          ADMIN, ("admin grant", "admin refund", "admin user",
                  "admin review", "store ")),
    Topic("account", E.KEY, "マクドナルドのアカウント",
          "注文に使うアカウントとカード。**カード未設定のアカウントは注文に使われません。**",
          ADMIN, ("mcd ",)),
    Topic("money", E.WALLET, "チャージの口座（Kyash・PayPay）",
          "利用者がチャージするときの受け取り口座。",
          ADMIN, ("kyash ", "paypay ")),
    Topic("panel", E.PIN, "パネル",
          "利用者が使うボタンを並べた常設メッセージ。設置と貼り直し。",
          ADMIN, ("panel ",)),
    Topic("trouble", E.SIREN, "困ったとき・調べる",
          "注文が通らない、アカウントが止まった、など。**まずここ。**",
          ADMIN, ("debug ", "stats ", "mcd history", "mcd health",
                  "admin audit")),
    Topic("server", E.SHIELD, "サーバーの管理",
          "お問い合わせ・認証・荒らし対策・処分。",
          ADMIN, ("ticket ", "verify ", "guard ", "mod ",
                  "admin ban", "admin unban")),
    Topic("growth", E.PARTY, "利用を広げる",
          "声かけ・ランキング・キャンペーン。",
          ADMIN, ("growth ", "config campaign",
                  "admin broadcast", "admin achievement")),
    Topic("config", E.GEAR, "設定",
          "補助率・上限・チャンネル・通信など。**ここは設定だけで、お金は動きません。**",
          ADMIN, ("config ", "proxy ")),
    Topic("maint", E.MAINTENANCE, "保守",
          "コマンドの再登録・再起動・バックアップ。",
          OWNER, ("sync", "restart", "admin backup", "admin restore",
                  "menu sync")),
]

# ⚠️ 「最初にやること」。新しく入れた人が必ず迷うので、順番で示す。
FIRST_STEPS = [
    ("/mcd add", "マクドナルドのアカウントを登録（カードも自動で見つけます）"),
    ("/kyash add か /paypay add", "チャージを受け取る口座を登録"),
    ("/panel order", "利用者が注文するパネルを置く"),
    ("/panel charge", "チャージのパネルを置く"),
    ("/config show", "補助率や上限を確認して、必要なら変える"),
]

# ⚠️ 取り違えると害が大きい組。**必ず見分けられるように並べて出す。**
CONFUSING = [
    ("/admin refund", "いますぐ返金する（**お金が動きます**）",
     "/config refund", "返金の設定を変える（お金は動きません）"),
    ("/admin ban", "BOTの利用を止める（Discordには残ります）",
     "/mod ban", "**Discordサーバーから追放**する"),
    ("/menu sync", "マクドナルドのメニューを取り直す",
     "/sync", "スラッシュコマンドをDiscordへ登録し直す"),
    ("/mcd card", "アカウント1件のカードを選ぶ",
     "/mcd cards", "全アカウントのカードを一覧で見る"),
]


def _level_of(user_is_admin: bool, user_is_owner: bool) -> set[str]:
    out = {EVERYONE}
    if user_is_admin or user_is_owner:
        out.add(ADMIN)
    if user_is_owner:
        out.add(OWNER)
    return out


class HelpCog(commands.Cog):
    """コマンドの案内"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _all_commands(self) -> list[tuple[str, str]]:
        """いま登録されているコマンドを (名前, 説明) で全部取る。

        ⚠️ 手書きの一覧を持たない。増減したときに必ずずれる。
        """
        out: list[tuple[str, str]] = []
        for cog in self.bot.cogs.values():
            for c in cog.walk_app_commands():
                if not isinstance(c, app_commands.Command):
                    continue
                # ⚠️ 案内の中に案内自身を並べない（読む人の役に立たない）
                if c.qualified_name == "help":
                    continue
                out.append((c.qualified_name, c.description or ""))
        return sorted(set(out))

    def _sorted_into_topics(self, levels: set[str]):
        """コマンドを目的ごとに仕分ける。

        ⚠️ 規則は **長く一致したほうが勝ち**。
           `mcd health` は「mcd 」(アカウント)にも「mcd health」(困った
           とき)にも当たる。表の順で先に取ると、狙って書いた細かい指定が
           死ぬ。長さで決めれば、細かく書いたほうが必ず効く。

        ⚠️ 表に無いコマンドは**捨てずに「その他」へ**。捨てると、
           新しく足したコマンドが案内から消えて見つけられなくなる。

        ⚠️ 権限が足りない分類のコマンドは「その他」にも入れない。
           入れてしまうと、見せない設定にした `/restart` などが
           「その他」から丸見えになる。
        """
        cmds = self._all_commands()
        owner_of: dict[str, Topic] = {}
        score: dict[str, int] = {}
        for t in TOPICS:
            for p in t.prefixes:
                for q, _ in cmds:
                    if not (q == p.strip() or q.startswith(p)):
                        continue
                    if len(p) > score.get(q, 0):
                        score[q] = len(p)
                        owner_of[q] = t

        buckets: list[tuple[Topic, list[tuple[str, str]]]] = []
        for t in TOPICS:
            if t.level not in levels:
                continue
            hit = [(q, d) for q, d in cmds if owner_of.get(q) is t]
            if hit:
                buckets.append((t, hit))
        rest = [(q, d) for q, d in cmds if q not in owner_of]
        return buckets, rest

    @app_commands.command(
        name="help", description="コマンドの一覧と使い方を表示します"
    )
    @app_commands.describe(
        search="探したい言葉（省略すると全体の案内）",
    )
    async def help_cmd(
        self, interaction: discord.Interaction,
        search: app_commands.Range[str, 1, 40] | None = None,
    ) -> None:
        """⚠️ 誰でも使える。権限で**出す中身を変える**（弾かない）。
           一般の利用者が打ったときに「権限がありません」とだけ
           返すのは、案内としては最悪。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        is_admin = is_admin_user(interaction)
        is_owner = interaction.user.id in (self.bot.owner_ids or set())
        levels = _level_of(is_admin, is_owner)

        if search:
            await interaction.followup.send(
                embed=self._search_embed(search, levels), ephemeral=True)
            return

        if not (is_admin or is_owner):
            await interaction.followup.send(
                embed=self._user_embed(), ephemeral=True)
            return

        await interaction.followup.send(
            embed=self._admin_embed(levels),
            view=HelpView(interaction.user.id, self, levels),
            ephemeral=True,
        )

    # -- 画面 ---------------------------------------------------

    def _user_embed(self) -> discord.Embed:
        """一般の利用者向け。

        ⚠️ コマンドを並べない。利用者はコマンドを打たない作りで、
           パネルのボタンで操作する。並べると混乱させるだけ。
        """
        return discord.Embed(
            title=f"{E.BURGER} ご利用方法",
            description=(
                "**コマンドを打つ必要はありません。**\n"
                "チャンネルに置いてあるパネルのボタンから操作できます。\n\n"
                f"{E.CART} **注文する** → 注文パネルの「注文する」\n"
                f"{E.CHARGE} **チャージする** → チャージパネルの案内どおりに\n"
                f"{E.HISTORY} **履歴を見る** → 注文パネルの「履歴」\n\n"
                f"{E.INFO} パネルが見当たらない場合は、管理者にお尋ねください。"
            ),
            color=embeds.BLUE,
        )

    def _admin_embed(self, levels: set[str]) -> discord.Embed:
        buckets, rest = self._sorted_into_topics(levels)
        total = sum(len(v) for _, v in buckets) + len(rest)

        e = discord.Embed(
            title=f"{E.NOTE} コマンドの案内",
            description=(
                f"使えるコマンドは **{total}** 個あります。\n"
                "下のボタンで、やりたいことから探せます。\n\n"
                f"{E.INFO} 言葉で探すなら `/help search:返金` のように。"
            ),
            color=embeds.BLUE,
        )
        e.add_field(
            name=f"{E.PIN} はじめての設定（この順番で）",
            value="\n".join(
                f"**{i}.** `{cmd}`　{why}"
                for i, (cmd, why) in enumerate(FIRST_STEPS, 1)
            ),
            inline=False,
        )
        e.add_field(
            name=f"{E.WARN} 取り違えやすい組",
            value="\n".join(
                f"`{a}` {aw}\n`{b}` {bw}"
                for a, aw, b, bw in CONFUSING[:2]
            ),
            inline=False,
        )
        e.add_field(
            name="分類",
            value="　".join(f"{t.emoji}{t.title}" for t, _ in buckets)
            + (f"　{E.NOTE}その他" if rest else ""),
            inline=False,
        )
        return e

    def topic_embed(self, key: str, levels: set[str]) -> discord.Embed:
        buckets, rest = self._sorted_into_topics(levels)
        if key == "_other":
            e = discord.Embed(
                title=f"{E.NOTE} その他",
                description="分類に入らなかったコマンドです。",
                color=embeds.BLUE,
            )
            e.add_field(
                name=f"{len(rest)} 個",
                value=self._lines(rest)[:1024] or "—", inline=False,
            )
            return e
        if key == "_confuse":
            e = discord.Embed(
                title=f"{E.WARN} 取り違えやすい組",
                description=(
                    "名前が似ていて、**効果がまったく違う**ものです。\n"
                    "打つ前に確かめてください。"
                ),
                color=embeds.YELLOW,
            )
            for a, aw, b, bw in CONFUSING:
                e.add_field(name=f"{a} ↔ {b}",
                            value=f"`{a}`\n　{aw}\n`{b}`\n　{bw}", inline=False)
            return e

        for t, hit in buckets:
            if t.key != key:
                continue
            e = discord.Embed(
                title=f"{t.emoji} {t.title}",
                description=t.hint,
                color=embeds.BLUE,
            )
            # ⚠️ 1つの欄は1024文字まで。超えると Discord が丸ごと断る。
            #    何個あっても出せるよう、欄を分けて入れる。
            for i, chunk in enumerate(self._chunks(hit)):
                e.add_field(
                    name=f"コマンド（{len(hit)}個）" if i == 0 else "（続き）",
                    value=chunk, inline=False,
                )
            return e
        return embeds.info("その分類にコマンドがありません。")

    def _search_embed(self, word: str, levels: set[str]) -> discord.Embed:
        w = word.strip().lower()
        buckets, rest = self._sorted_into_topics(levels)
        pool = [c for _, hit in buckets for c in hit] + rest
        hit = [(q, d) for q, d in pool if w in q.lower() or w in (d or "").lower()]
        if not hit:
            return discord.Embed(
                title=f"{E.NOTE} 「{word}」は見つかりませんでした",
                description=(
                    "別の言葉でお試しください。\n"
                    f"{E.INFO} `/help` で、やりたいことから探せます。"
                ),
                color=embeds.YELLOW,
            )
        e = discord.Embed(
            title=f"{E.NOTE} 「{word}」の検索結果（{len(hit)}件）",
            color=embeds.BLUE,
        )
        for i, chunk in enumerate(self._chunks(hit[:40])):
            e.add_field(name="​" if i else "見つかったコマンド",
                        value=chunk, inline=False)
        return e

    @staticmethod
    def _lines(cmds) -> str:
        return "\n".join(f"`/{q}`　{d}" for q, d in cmds)

    @staticmethod
    def _chunks(cmds, limit: int = 1000):
        """1024文字の制限に収まるように切り分ける。"""
        out, cur = [], ""
        for q, d in cmds:
            line = f"`/{q}`　{d}\n"
            if len(cur) + len(line) > limit:
                out.append(cur or "—")
                cur = ""
            cur += line
        out.append(cur or "—")
        return out[:5]       # 欄は25個まで。念のため上限


class HelpView(discord.ui.View):
    """分類を選ぶボタン。"""

    def __init__(self, owner_id: int, cog: HelpCog, levels: set[str]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.cog = cog
        self.levels = levels

        buckets, rest = cog._sorted_into_topics(levels)
        options = [
            discord.SelectOption(
                label=t.title[:100], value=t.key,
                emoji=t.emoji, description=f"{len(hit)}個",
            )
            for t, hit in buckets
        ]
        options.append(discord.SelectOption(
            label="取り違えやすい組", value="_confuse", emoji=E.WARN,
            description="名前が似ていて効果が違うもの",
        ))
        if rest:
            options.append(discord.SelectOption(
                label="その他", value="_other", emoji=E.NOTE,
                description=f"{len(rest)}個",
            ))
        sel = discord.ui.Select(
            placeholder="やりたいことを選んでください", options=options[:25],
        )
        sel.callback = self._pick
        self.add_item(sel)
        self._sel = sel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _pick(self, interaction: discord.Interaction) -> None:
        # ⚠️ values[0] と直接書かない。選択が空で届くと IndexError で
        #    落ち、利用者には「インタラクションに失敗しました」しか
        #    出ない。何も選ばれていなければ最初の画面に戻す。
        key = self._sel.values[0] if self._sel.values else ""
        await interaction.response.edit_message(
            embed=(self.cog.topic_embed(key, self.levels) if key
                   else self.cog._admin_embed(self.levels)),
            view=self,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
