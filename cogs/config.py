"""設定コマンド。

main.py に直書きするのはトークンとオーナーIDだけで、
それ以外はすべてここから設定する。値は SQLite に保存される。

コマンドの引数名は ASCII に統一している。Discord のオプション名は
小文字英数字が確実に通る範囲なので、表示用の日本語は describe で与える。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mcd.rates import user_pays
from mcd.settings import GROUPS, SPECS, SettingError

from ._shared import BAD, INFO, MONEY, OK, WARN, deny, embed, reply

log = logging.getLogger("bot.config")

WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

CHANNEL_CHOICES = [
    app_commands.Choice(name=spec.label, value=spec.key)
    for spec in SPECS.values()
    if spec.kind == "channel"
]
GROUP_CHOICES = [app_commands.Choice(name=g, value=g) for g in GROUPS]

# /config set と /config reset で選べる項目（チャンネル・ロールは専用コマンド）
SETTABLE = [k for k, s in SPECS.items() if s.kind in ("int", "rate", "str")]


async def owner_gate(interaction: discord.Interaction) -> bool:
    if await interaction.client.is_owner(interaction.user):
        return True
    await deny(interaction, "オーナー限定のコマンドです")
    return False


def fmt_value(spec, value) -> str:
    if spec.kind == "channel":
        return f"<#{value}>" if value else "未設定"
    if spec.kind == "role":
        return f"<@&{value}>" if value else "未設定"
    if spec.kind == "guild":
        return f"`{value}`" if value else "グローバル"
    if spec.kind == "text":
        text = str(value).replace("\n", " ")
        return (text[:60] + "…") if len(text) > 60 else (text or "未設定")
    if spec.kind == "rate":
        return f"{value}%"
    if spec.kind == "str":
        return f"`{value}`" if value else "未設定"
    return f"{value:,}" if isinstance(value, int) else str(value)


class TermsModal(discord.ui.Modal, title="規約の編集"):
    version = discord.ui.TextInput(label="版（変更すると全員に再同意を求めます）", max_length=40)
    text = discord.ui.TextInput(label="本文", style=discord.TextStyle.paragraph, max_length=3500)

    def __init__(self, cog: "Config", version: str, body: str):
        super().__init__()
        self.cog = cog
        self.version.default = version
        self.text.default = body

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_terms(interaction, str(self.version.value), str(self.text.value))


class Config(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="config", description="BOTの設定（オーナー限定）")

    # ------------------------------------------------------------ 共通処理

    async def _log_change(self, interaction: discord.Interaction, key: str, value) -> None:
        cfg = self.bot.cfg
        await asyncio.to_thread(
            self.bot.store.audit, "config.changed", interaction.user.id,
            {"key": key, "value": str(value)[:200]},
        )
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_KEY} 設定を変更しました",
                f"`{key}` → {value}\n実行: {interaction.user.mention}",
                INFO,
            ),
        )

    # ---------------------------------------------------------- セットアップ

    @group.command(name="setup", description="未設定の項目と設定手順を表示します")
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        missing = cfg.missing_required()
        mcd_count = len(await asyncio.to_thread(self.bot.store.list_mcd_accounts, True))
        kyash_count = len(await asyncio.to_thread(self.bot.store.list_kyash_accounts, True))

        done = not missing and mcd_count and kyash_count
        e = embed(
            f"{cfg.E_OK} 設定は完了しています" if done else f"{cfg.E_WARN} 設定が未完了です",
            None,
            OK if done else WARN,
            footer=cfg.BRAND_NAME,
        )

        if missing:
            e.add_field(
                name=f"{cfg.E_NG} 未設定のチャンネル・ロール",
                value="\n".join(f"・{s.label} (`{s.key}`)" for s in missing)[:1020],
                inline=False,
            )
        else:
            e.add_field(name=f"{cfg.E_OK} チャンネル・ロール", value="すべて設定済み", inline=False)

        e.add_field(
            name="アカウント",
            value=(
                f"{cfg.E_OK if mcd_count else cfg.E_NG} マクドナルド {mcd_count} 件\n"
                f"{cfg.E_OK if kyash_count else cfg.E_NG} Kyash {kyash_count} 件"
            ),
            inline=False,
        )
        e.add_field(
            name="設定のしかた",
            value=(
                "```\n"
                "/config channel  項目を選んでチャンネルを指定\n"
                "/config role     注文できるロールを指定\n"
                "/config guild    このサーバーをコマンド同期先にする\n"
                "/config set      数値や文字列の設定\n"
                "/config show     現在の設定を一覧\n"
                "/mcd login       マクドナルドにログイン\n"
                "/kyash login     Kyash にログイン\n"
                "/panel           パネルを設置\n"
                "```"
            ),
            inline=False,
        )
        await reply(interaction, e)

    @group.command(name="show", description="現在の設定を一覧表示します")
    @app_commands.describe(section="表示するグループ（省略で全部）")
    @app_commands.choices(section=GROUP_CHOICES)
    async def show(
        self, interaction: discord.Interaction, section: Optional[app_commands.Choice[str]] = None
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        targets = [section.value] if section else GROUPS
        e = embed(f"{cfg.E_CHART} 現在の設定", None, INFO)

        for name in targets:
            lines = []
            for spec, value in cfg.all_of(name):
                mark = "" if cfg.is_default(spec.key) else " *"
                lines.append(f"`{spec.key}`{mark}\n　{spec.label}: {fmt_value(spec, value)}")
            if lines:
                e.add_field(name=name, value="\n".join(lines)[:1020], inline=False)

        if section is None:
            roles = cfg.role_rates()
            users = cfg.user_rates()
            campaigns = cfg.campaigns()
            e.add_field(
                name="個別の負担率",
                value=(
                    ("ロール: " + ", ".join(f"<@&{r}> {v}%" for r, v in roles.items())
                     if roles else "ロール: なし")
                    + "\n"
                    + ("ユーザー: " + ", ".join(f"<@{u}> {v}%" for u, v in users.items())
                       if users else "ユーザー: なし")
                    + "\n"
                    + ("キャンペーン: " + ", ".join(f"{c.name} {c.rate}%" for c in campaigns)
                       if campaigns else "キャンペーン: なし")
                )[:1020],
                inline=False,
            )
        e.set_footer(text="* が付いている項目は既定値から変更済み")
        await reply(interaction, e)

    # --------------------------------------------------- チャンネル・ロール

    @group.command(name="channel", description="チャンネルを設定します")
    @app_commands.describe(item="設定する項目", channel="割り当てるチャンネル")
    @app_commands.choices(item=CHANNEL_CHOICES)
    async def channel(
        self,
        interaction: discord.Interaction,
        item: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 投稿できないチャンネルを設定すると後で無言で失敗するので、ここで弾く
        me = interaction.guild.me if interaction.guild else None
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.view_channel and perms.send_messages):
                await reply(
                    interaction,
                    embed(
                        f"{cfg.E_NG} そのチャンネルに投稿できません",
                        f"{channel.mention} で BOT に「チャンネルを見る」と"
                        "「メッセージを送信」の権限を与えてください。",
                        BAD,
                    ),
                )
                return

        cfg.set(item.value, channel.id)
        await self._log_change(interaction, item.value, channel.mention)
        await reply(
            interaction, embed(f"{cfg.E_OK} 設定しました", f"{item.name} → {channel.mention}", OK)
        )

    @group.command(name="role", description="注文できるロールを設定します")
    @app_commands.describe(role="注文を許可するロール")
    async def role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg.set("ORDER_ROLE_ID", role.id)
        await self._log_change(interaction, "ORDER_ROLE_ID", role.name)
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 注文できるロールを設定しました",
                f"{role.mention} を持つ人だけがパネルから注文できます。",
                OK,
            ),
        )

    @group.command(name="guild", description="このサーバーをコマンドの同期先にします")
    async def guild(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        if interaction.guild is None:
            await deny(interaction, "サーバー内で実行してください")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg.set("GUILD_ID", interaction.guild.id)
        try:
            await self.bot.sync_commands()
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 同期に失敗しました", str(exc)[:300], BAD))
            return
        await self._log_change(interaction, "GUILD_ID", interaction.guild.name)
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 同期先をこのサーバーにしました",
                "コマンドの変更が即座に反映されるようになります。",
                OK,
            ),
        )

    @group.command(name="sync", description="スラッシュコマンドを再同期します")
    async def sync(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.sync_commands()
        except Exception as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 同期に失敗しました", str(exc)[:300], BAD))
            return
        await reply(interaction, embed(f"{cfg.E_OK} 同期しました", "", OK))

    # ----------------------------------------------------------- 数値・文字列

    @staticmethod
    def _key_choices(current: str) -> list[app_commands.Choice[str]]:
        text = (current or "").lower()
        out: list[app_commands.Choice[str]] = []
        for key in SETTABLE:
            spec = SPECS[key]
            if text and text not in key.lower() and text not in spec.label.lower():
                continue
            out.append(app_commands.Choice(name=f"{spec.label} ({key})"[:100], value=key))
            if len(out) >= 25:
                break
        return out

    @group.command(name="set", description="数値や文字列の設定を変更します")
    @app_commands.describe(item="設定する項目", value="新しい値")
    async def set_cmd(self, interaction: discord.Interaction, item: str, value: str) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        spec = SPECS.get(item)
        if spec is None or item not in SETTABLE:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} 項目が正しくありません",
                    "候補から選んでください。チャンネルとロールは専用コマンドです。",
                    BAD,
                ),
            )
            return

        try:
            saved = cfg.set(item, value)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 設定できません", str(exc), BAD))
            return

        e = embed(f"{cfg.E_OK} 設定しました", f"{spec.label}\n`{item}` → **{saved}**", OK)
        if spec.hint:
            e.add_field(name="補足", value=spec.hint, inline=False)
        if item == "DEFAULT_USER_RATE":
            e.add_field(
                name="例", value=f"定価 590 円 → {user_pays(590, int(saved)):,} 円", inline=False
            )
        await self._log_change(interaction, item, saved)
        await reply(interaction, e)

    @set_cmd.autocomplete("item")
    async def _set_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._key_choices(current)

    @group.command(name="reset", description="設定を既定値に戻します")
    @app_commands.describe(item="戻す項目")
    async def reset(self, interaction: discord.Interaction, item: str) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            value = cfg.reset(item)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 戻せません", str(exc), BAD))
            return
        await self._log_change(interaction, item, f"既定値 {value}")
        await reply(
            interaction, embed(f"{cfg.E_OK} 既定値に戻しました", f"`{item}` → **{value}**", OK)
        )

    @reset.autocomplete("item")
    async def _reset_item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._key_choices(current)

    # ------------------------------------------------------------------ 規約

    @group.command(name="terms", description="規約の版と本文を編集します")
    async def terms(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.send_modal(
            TermsModal(self, str(cfg.TERMS_VERSION), str(cfg.TERMS_TEXT))
        )

    async def save_terms(self, interaction: discord.Interaction, version: str, text: str) -> None:
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        old_version = str(cfg.TERMS_VERSION)
        try:
            cfg.set("TERMS_TEXT", text)
            cfg.set("TERMS_VERSION", version)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 保存できません", str(exc), BAD))
            return

        changed = old_version != version
        await self._log_change(interaction, "TERMS_VERSION", version)
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 規約を保存しました",
                f"版: `{old_version}` → `{version}`"
                + ("\n\n版が変わったので、全員に再同意を求めます。" if changed else ""),
                WARN if changed else OK,
            ),
        )

    # ----------------------------------------------------------- 個別の負担率

    @group.command(name="rate-role", description="ロールごとの負担率を設定します")
    @app_commands.describe(role="対象のロール", rate="定価の何%を払うか。省略で解除")
    async def rate_role(
        self, interaction: discord.Interaction, role: discord.Role, rate: Optional[int] = None
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg.set_role_rate(role.id, rate)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 設定できません", str(exc), BAD))
            return
        await self._log_change(interaction, "ROLE_RATES", f"{role.name}={rate}")
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 設定しました" if rate is not None else f"{cfg.E_OK} 解除しました",
                (
                    f"{role.mention} の負担率を **{rate}%** にしました\n"
                    f"定価 590 円 → {user_pays(590, rate):,} 円"
                    if rate is not None
                    else f"{role.mention} の個別設定を解除しました"
                ),
                OK,
            ),
        )

    @group.command(name="rate-user", description="利用者ごとの負担率を設定します")
    @app_commands.describe(user="対象の利用者", rate="定価の何%を払うか。省略で解除")
    async def rate_user(
        self, interaction: discord.Interaction, user: discord.User, rate: Optional[int] = None
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg.set_user_rate(user.id, rate)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 設定できません", str(exc), BAD))
            return
        await self._log_change(interaction, "USER_RATES", f"{user}={rate}")
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 設定しました" if rate is not None else f"{cfg.E_OK} 解除しました",
                (
                    f"{user.mention} の負担率を **{rate}%** にしました\n"
                    "ユーザー個別設定は最優先で適用されます。"
                    if rate is not None
                    else f"{user.mention} の個別設定を解除しました"
                ),
                OK,
            ),
        )

    # ----------------------------------------------------------- キャンペーン

    @group.command(name="campaign-add", description="時間帯限定の負担率を追加します")
    @app_commands.describe(
        name="キャンペーン名",
        rate="その時間帯の負担率(%)",
        weekdays="0=月 〜 6=日。カンマ区切り。省略で毎日",
        start_hour="開始する時(0-23)",
        end_hour="終了する時(1-24)",
    )
    async def campaign_add(
        self,
        interaction: discord.Interaction,
        name: str,
        rate: int,
        weekdays: Optional[str] = None,
        start_hour: int = 0,
        end_hour: int = 24,
    ) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)

        if weekdays:
            try:
                days = tuple(int(d.strip()) for d in weekdays.split(",") if d.strip())
            except ValueError:
                await reply(
                    interaction,
                    embed(f"{cfg.E_NG} 曜日の指定が不正です", "例: `4,5` （金・土）", BAD),
                )
                return
        else:
            days = tuple(range(7))

        try:
            cfg.add_campaign(name, rate, days, start_hour, end_hour)
        except SettingError as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 追加できません", str(exc), BAD))
            return

        await self._log_change(interaction, "CAMPAIGNS", f"+{name}")
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} キャンペーンを追加しました",
                f"**{name}** ・ 負担率 {rate}%\n"
                f"{'/'.join(WEEKDAY_NAMES[d] for d in sorted(set(days)))} の "
                f"{start_hour}時〜{end_hour}時\n\n"
                f"定価 590 円 → {user_pays(590, rate):,} 円",
                MONEY,
            ),
        )

    @group.command(name="campaign-list", description="キャンペーンの一覧")
    async def campaign_list(self, interaction: discord.Interaction) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = cfg.campaigns()
        if not items:
            await reply(interaction, embed("キャンペーンはありません", "", INFO))
            return

        from mcd.store import now_jst

        now = now_jst()
        lines = [
            f"{cfg.E_OK if c.active_at(now) else '　'} **{c.name}** ・ {c.rate}% ・ "
            f"{'/'.join(WEEKDAY_NAMES[d] for d in c.weekdays)} {c.start_hour}-{c.end_hour}時"
            for c in items
        ]
        await reply(
            interaction,
            embed(
                f"{cfg.E_MONEY} キャンペーン {len(items)} 件",
                "\n".join(lines)[:3800] + f"\n\n{cfg.E_OK} は現在適用中",
                MONEY,
            ),
        )

    @group.command(name="campaign-remove", description="キャンペーンを削除します")
    @app_commands.describe(name="削除するキャンペーン名")
    async def campaign_remove(self, interaction: discord.Interaction, name: str) -> None:
        if not await owner_gate(interaction):
            return
        cfg = self.bot.cfg
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not cfg.remove_campaign(name):
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 見つかりません", f"「{name}」というキャンペーンはありません。", BAD),
            )
            return
        await self._log_change(interaction, "CAMPAIGNS", f"-{name}")
        await reply(interaction, embed(f"{cfg.E_OK} 削除しました", f"「{name}」", OK))

    @campaign_remove.autocomplete("name")
    async def _campaign_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        text = (current or "").lower()
        return [
            app_commands.Choice(name=c.name, value=c.name)
            for c in self.bot.cfg.campaigns()
            if text in c.name.lower()
        ][:25]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Config(bot))
