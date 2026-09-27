"""プロキシチェック用のスラッシュコマンドと結果表示 UI。"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from checker import (
    ProxyResult,
    ProxyTarget,
    check_many,
    parse_https_judge,
    parse_judge,
    parse_proxy_list,
)
from formatting import (
    alive_list_text,
    clip,
    format_duration,
    format_latency,
    progress_bar,
    report_text,
    result_line,
)
from settings import OPTIONS, SettingsStore

logger = logging.getLogger("proxybot.proxy")

COLOR_PROGRESS = discord.Color(0x5865F2)
COLOR_OK = discord.Color(0x57F287)
COLOR_MIXED = discord.Color(0xFEE75C)
COLOR_FAIL = discord.Color(0xED4245)
COLOR_INFO = discord.Color(0x5865F2)

PAGE_SIZE = 8
PROGRESS_INTERVAL = 2.0
VIEW_TIMEOUT = 900.0
FIELD_LIMIT = 1024

MODE_LABELS = {"alive": ("✅", "生存プロキシ"), "all": ("📋", "全件"), "dead": ("❌", "失敗したプロキシ")}


# --------------------------------------------------------------------------- #
# 小さなヘルパー
# --------------------------------------------------------------------------- #


def scope_of(interaction: discord.Interaction) -> str:
    """設定の保存単位。サーバーごと、DM は実行者ごと。"""
    if interaction.guild_id:
        return f"guild:{interaction.guild_id}"
    return f"user:{interaction.user.id}"


def token_of(target: ProxyTarget) -> str:
    """再チェック用に元の表記へ戻す。"""
    if target.username:
        return f"{target.username}:{target.password or ''}@{target.address}"
    return target.address


def build_files(
    results: Sequence[ProxyResult], *, timeout: float, concurrency: int
) -> list[discord.File]:
    """生存リストと詳細レポートを添付ファイルにする。"""
    files: list[discord.File] = []
    alive = alive_list_text(results).strip()
    if alive:
        files.append(
            discord.File(io.BytesIO(alive.encode("utf-8") + b"\n"), filename="alive_proxies.txt")
        )
    report = report_text(results, timeout=timeout, concurrency=concurrency)
    files.append(discord.File(io.BytesIO(report.encode("utf-8")), filename="proxy_report.txt"))
    return files


def error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=f"⚠️ {title}", description=description, color=COLOR_FAIL)


async def respond(
    interaction: discord.Interaction, embed: discord.Embed, *, ephemeral: bool = True
) -> None:
    """応答済みかどうかを気にせず返信する。"""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# --------------------------------------------------------------------------- #
# 入力ウィンドウ (複数行の貼り付け用)
# --------------------------------------------------------------------------- #


class ProxyInputModal(discord.ui.Modal, title="プロキシチェック"):
    proxies = discord.ui.TextInput(
        label="プロキシ一覧",
        style=discord.TextStyle.paragraph,
        placeholder="123.45.67.89:8080\n98.76.54.32:3128\n… 改行・カンマ・空白区切りでまとめて貼り付け",
        required=True,
        max_length=4000,
    )

    def __init__(self, cog: "ProxyChecker", overrides: dict[str, Any]) -> None:
        super().__init__(timeout=600.0)
        self._cog = cog
        self._overrides = overrides

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._cog.run_check(interaction, str(self.proxies.value), **self._overrides)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("入力ウィンドウでエラー: %s", error)
        await respond(interaction, error_embed("エラー", "入力の処理中に問題が発生しました。"))


# --------------------------------------------------------------------------- #
# 結果表示 (ページ送り / 表示切替 / ファイル出力 / 再チェック)
# --------------------------------------------------------------------------- #


class ResultView(discord.ui.View):
    def __init__(
        self,
        cog: "ProxyChecker",
        results: list[ProxyResult],
        *,
        author: discord.abc.User,
        timeout_sec: float,
        concurrency: int,
        elapsed: float,
        notices: list[str],
        show_dead: bool,
        ephemeral: bool,
        overrides: dict[str, Any],
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.cog = cog
        self.results = results
        self.author = author
        self.timeout_sec = timeout_sec
        self.concurrency = concurrency
        self.elapsed = elapsed
        self.notices = notices
        self.ephemeral = ephemeral
        self.overrides = overrides
        self.message: Optional[discord.Message] = None

        self.alive = sorted(
            (r for r in results if r.ok), key=lambda r: r.latency_ms if r.latency_ms else 0.0
        )
        self.dead = [r for r in results if not r.ok]

        if not self.alive:
            self.mode = "dead"
        elif show_dead and self.dead:
            self.mode = "all"
        else:
            self.mode = "alive"
        self.page = 0
        self._sync()

    # -- データ ---------------------------------------------------------- #

    @property
    def visible(self) -> list[ProxyResult]:
        if self.mode == "alive":
            return self.alive
        if self.mode == "dead":
            return self.dead
        return self.alive + self.dead

    @property
    def page_count(self) -> int:
        return max(1, -(-len(self.visible) // PAGE_SIZE))

    # -- 見た目 ---------------------------------------------------------- #

    def build_embed(self) -> discord.Embed:
        total = len(self.results)
        alive_count = len(self.alive)
        rate = (alive_count / total * 100) if total else 0.0

        if alive_count == total and total:
            color = COLOR_OK
        elif alive_count:
            color = COLOR_MIXED
        else:
            color = COLOR_FAIL

        lines = [f"{progress_bar(alive_count, total)} が生存 (`{alive_count}/{total}` 件)"]
        lines.extend(self.notices)
        embed = discord.Embed(
            title="🛰️ プロキシチェック結果",
            description=clip("\n".join(lines), 4000),
            color=color,
        )

        latencies = [r.latency_ms for r in self.alive if r.latency_ms is not None]
        average = f"{sum(latencies) / len(latencies):.0f}ms" if latencies else "―"
        fastest = f"`{self.alive[0].address}`\n{format_latency(self.alive[0].latency_ms)}" if self.alive else "―"

        embed.add_field(name="✅ 生存", value=f"**{alive_count}** 件", inline=True)
        embed.add_field(name="❌ 失敗", value=f"**{len(self.dead)}** 件", inline=True)
        embed.add_field(name="📊 成功率", value=f"**{rate:.1f}%**", inline=True)
        embed.add_field(name="⚡ 平均応答", value=f"**{average}**", inline=True)
        embed.add_field(name="🏁 最速", value=fastest, inline=True)

        https_ok = sum(1 for r in self.alive if r.https)
        checked_https = any(r.https is not None for r in self.alive)
        embed.add_field(
            name="🔒 HTTPS対応",
            value=f"**{https_ok}** 件" if checked_https else "未判定",
            inline=True,
        )

        visible = self.visible
        icon, label = MODE_LABELS[self.mode]
        if visible:
            self.page = min(self.page, self.page_count - 1)
            start = self.page * PAGE_SIZE
            chunk = visible[start : start + PAGE_SIZE]
            body = "\n".join(result_line(r, start + i + 1) for i, r in enumerate(chunk))
            name = f"{icon} {label}  ({start + 1}〜{start + len(chunk)} / {len(visible)} 件)"
            embed.add_field(name=name, value=clip(body, FIELD_LIMIT), inline=False)
        else:
            embed.add_field(name=f"{icon} {label}", value="該当なし", inline=False)

        embed.set_footer(
            text=(
                f"所要 {format_duration(self.elapsed)} ・ タイムアウト {self.timeout_sec:.1f}秒 ・ "
                f"同時 {self.concurrency}台 ・ 実行 {self.author.display_name}"
            ),
            icon_url=self.author.display_avatar.url,
        )
        return embed

    def _sync(self) -> None:
        """ページ送りボタンなどの状態を現在の表示に合わせる。"""
        multi_page = self.page_count > 1
        self.prev_page.disabled = not multi_page or self.page <= 0
        self.next_page.disabled = not multi_page or self.page >= self.page_count - 1
        self.page_label.label = f"{self.page + 1} / {self.page_count}"
        self.recheck.disabled = not self.dead
        for option in self.mode_select.options:
            option.default = option.value == self.mode

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # -- 操作 ------------------------------------------------------------ #

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message(
            embed=error_embed("操作できません", "このボタンはコマンドを実行した人だけが使えます。"),
            ephemeral=True,
        )
        return False

    @discord.ui.select(
        placeholder="表示を切り替える",
        row=0,
        options=[
            discord.SelectOption(label="生存のみ", value="alive", emoji="✅", description="成功したプロキシだけ表示"),
            discord.SelectOption(label="全件", value="all", emoji="📋", description="成功と失敗をまとめて表示"),
            discord.SelectOption(label="失敗のみ", value="dead", emoji="❌", description="失敗したプロキシと理由を表示"),
        ],
    )
    async def mode_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.mode = select.values[0]
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        await self._refresh(interaction)

    @discord.ui.button(label="テキストで受け取る", emoji="📄", style=discord.ButtonStyle.success, row=2)
    async def download(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📄 結果ファイル",
                description="`alive_proxies.txt` は生存プロキシの一覧、`proxy_report.txt` は全件の詳細です。",
                color=COLOR_INFO,
            ),
            files=build_files(self.results, timeout=self.timeout_sec, concurrency=self.concurrency),
            ephemeral=True,
        )

    @discord.ui.button(label="失敗分を再チェック", emoji="🔄", style=discord.ButtonStyle.primary, row=2)
    async def recheck(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        text = " ".join(token_of(r.target) for r in self.dead)
        if not text:
            await interaction.response.send_message(
                embed=error_embed("対象なし", "失敗したプロキシはありません。"), ephemeral=True
            )
            return
        await self.cog.run_check(interaction, text, **self.overrides)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


class ProxyChecker(commands.Cog):
    """HTTP / HTTPS プロキシの判定コマンド。"""

    config = app_commands.Group(
        name="config",
        description="チェッカーの設定 (トークン以外はすべてここで設定します)",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = SettingsStore()
        self._busy: set[int] = set()

    # ------------------------------------------------------------------ #
    # /check
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="check",
        description="HTTP/HTTPSプロキシの生存・応答速度・出口IP・国名をまとめて判定します",
    )
    @app_commands.describe(
        proxies="ip:port を改行・カンマ・空白区切りで貼り付け (省略すると入力ウィンドウが開きます)",
        timeout="今回だけタイムアウトを変更 (秒)",
        concurrency="今回だけ同時実行数を変更",
        https_check="HTTPS (CONNECT) 対応も判定するか",
        private="結果を自分だけに表示する",
    )
    async def check(
        self,
        interaction: discord.Interaction,
        proxies: Optional[str] = None,
        timeout: Optional[app_commands.Range[float, 1.0, 30.0]] = None,
        concurrency: Optional[app_commands.Range[int, 1, 200]] = None,
        https_check: Optional[bool] = None,
        private: Optional[bool] = None,
    ) -> None:
        overrides: dict[str, Any] = {
            "timeout": timeout,
            "concurrency": concurrency,
            "https_check": https_check,
            "private": private,
        }
        if proxies is None or not proxies.strip():
            await interaction.response.send_modal(ProxyInputModal(self, overrides))
            return
        await self.run_check(interaction, proxies, **overrides)

    async def run_check(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        *,
        timeout: Optional[float] = None,
        concurrency: Optional[int] = None,
        https_check: Optional[bool] = None,
        private: Optional[bool] = None,
    ) -> None:
        conf = self.store.get(scope_of(interaction))
        ephemeral = bool(conf["ephemeral"]) if private is None else bool(private)
        eff_timeout = float(timeout) if timeout is not None else float(conf["timeout"])
        eff_conc = int(concurrency) if concurrency is not None else int(conf["concurrency"])
        eff_https = bool(https_check) if https_check is not None else bool(conf["https_check"])

        targets, invalid = parse_proxy_list(raw_text)
        if not targets:
            await respond(
                interaction,
                error_embed(
                    "プロキシを読み取れませんでした",
                    "`123.45.67.89:8080` のように **ip:port** で指定してください。\n"
                    "改行・カンマ・空白区切りで複数まとめて渡せます。",
                ),
            )
            return

        notices: list[str] = []
        if invalid:
            shown = " ".join(f"`{token}`" for token in invalid[:5])
            more = f" ほか{len(invalid) - 5}件" if len(invalid) > 5 else ""
            notices.append(f"⚠️ 形式を読めなかった入力 {len(invalid)}件: {shown}{more}")

        limit = int(conf["max_proxies"])
        if len(targets) > limit:
            notices.append(
                f"⚠️ 上限 {limit}件を超えたため先頭 {limit}件のみ検査しました "
                "(`/config set max_proxies:` で変更できます)"
            )
            targets = targets[:limit]

        try:
            judge = parse_judge(str(conf["judge_url"]))
            https_judge = parse_https_judge(str(conf["https_judge_url"]))
        except ValueError as exc:
            await respond(
                interaction,
                error_embed(
                    "判定用URLの設定が不正です",
                    f"{exc}\n`/config set judge_url:` で設定し直すか `/config reset` で既定に戻してください。",
                ),
            )
            return

        if interaction.user.id in self._busy:
            await respond(
                interaction,
                error_embed("すでに実行中です", "前回のチェックが終わるまで少し待ってください。"),
            )
            return

        self._busy.add(interaction.user.id)
        try:
            await interaction.response.defer(ephemeral=ephemeral)

            total = len(targets)
            state = {"done": 0, "alive": 0}
            started = time.perf_counter()
            message = await interaction.followup.send(
                embed=self._progress_embed(state, total, 0.0), wait=True
            )

            stop = asyncio.Event()
            progress_task = asyncio.create_task(
                self._progress_loop(message, state, total, started, stop)
            )

            def on_result(result: ProxyResult) -> None:
                state["done"] += 1
                if result.ok:
                    state["alive"] += 1

            try:
                results = await check_many(
                    targets,
                    judge=judge,
                    https_judge=https_judge,
                    timeout=eff_timeout,
                    concurrency=eff_conc,
                    https_check=eff_https,
                    retries=int(conf["retries"]),
                    on_result=on_result,
                )
            finally:
                stop.set()
                await asyncio.gather(progress_task, return_exceptions=True)

            elapsed = time.perf_counter() - started
            view = ResultView(
                self,
                results,
                author=interaction.user,
                timeout_sec=eff_timeout,
                concurrency=eff_conc,
                elapsed=elapsed,
                notices=notices,
                show_dead=bool(conf["show_dead"]),
                ephemeral=ephemeral,
                overrides={
                    "timeout": timeout,
                    "concurrency": concurrency,
                    "https_check": https_check,
                    "private": private,
                },
            )
            try:
                await message.edit(embed=view.build_embed(), view=view)
                view.message = message
            except discord.HTTPException as exc:
                logger.warning("結果の表示に失敗: %s", exc)

            if bool(conf["attach_file"]) and any(r.ok for r in results):
                await interaction.followup.send(
                    files=build_files(results, timeout=eff_timeout, concurrency=eff_conc),
                    ephemeral=ephemeral,
                )

            logger.info(
                "check: %s件 / 生存 %s件 (%.1f秒) by %s",
                total,
                state["alive"],
                elapsed,
                interaction.user,
            )
        finally:
            self._busy.discard(interaction.user.id)

    def _progress_embed(self, state: dict[str, int], total: int, elapsed: float) -> discord.Embed:
        done = state["done"]
        embed = discord.Embed(
            title="🛰️ プロキシをチェック中…",
            description=f"{progress_bar(done, total)}\n`{done}/{total}` 件完了",
            color=COLOR_PROGRESS,
        )
        embed.add_field(name="✅ 生存", value=f"**{state['alive']}** 件", inline=True)
        embed.add_field(name="❌ 失敗", value=f"**{done - state['alive']}** 件", inline=True)
        embed.add_field(name="⏱️ 経過", value=format_duration(elapsed), inline=True)
        return embed

    async def _progress_loop(
        self,
        message: discord.Message,
        state: dict[str, int],
        total: int,
        started: float,
        stop: asyncio.Event,
    ) -> None:
        """一定間隔で進捗を書き換える (レート制限に配慮して2秒ごと)。"""
        last_done = -1
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            if state["done"] == last_done:
                continue
            last_done = state["done"]
            try:
                await message.edit(
                    embed=self._progress_embed(state, total, time.perf_counter() - started)
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    # /config
    # ------------------------------------------------------------------ #

    @config.command(name="view", description="現在の設定を表示します")
    async def config_view(self, interaction: discord.Interaction) -> None:
        scope = scope_of(interaction)
        conf = self.store.get(scope)
        changed = self.store.customized_keys(scope)

        embed = discord.Embed(
            title="⚙️ 現在の設定",
            description=(
                "`/config set` で変更、`/config reset` で既定値に戻します。\n"
                "★ が付いている項目は既定値から変更されています。"
            ),
            color=COLOR_INFO,
        )
        for option in OPTIONS:
            mark = "★ " if option.key in changed else ""
            embed.add_field(
                name=f"{mark}{option.label}",
                value=(
                    f"**{clip(option.format(conf[option.key]), 200)}**\n"
                    f"`{option.key}` ・ {option.description}"
                ),
                inline=False,
            )
        scope_text = (
            f"このサーバー ({interaction.guild.name})"
            if interaction.guild
            else "あなたの DM"
        )
        embed.set_footer(text=f"設定の適用範囲: {scope_text}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="set", description="設定を変更します (変更したい項目だけ指定)")
    @app_commands.describe(
        timeout="プロキシ1台に待つ最大秒数 (1〜30)",
        concurrency="同時に検査する台数 (1〜200)",
        retries="失敗時の再試行回数 (0〜3)",
        https_check="HTTPS (CONNECT) 対応も判定する",
        max_proxies="1回で受け付ける最大件数 (1〜1000)",
        show_dead="結果一覧に失敗したプロキシも表示する",
        attach_file="生存リストの .txt を自動添付する",
        ephemeral="結果を実行者だけに表示する",
        judge_url="出口IP・国名の判定に使う http:// のエンドポイント",
        https_judge_url="CONNECTで判定するときに使う https:// のエンドポイント",
    )
    async def config_set(
        self,
        interaction: discord.Interaction,
        timeout: Optional[app_commands.Range[float, 1.0, 30.0]] = None,
        concurrency: Optional[app_commands.Range[int, 1, 200]] = None,
        retries: Optional[app_commands.Range[int, 0, 3]] = None,
        https_check: Optional[bool] = None,
        max_proxies: Optional[app_commands.Range[int, 1, 1000]] = None,
        show_dead: Optional[bool] = None,
        attach_file: Optional[bool] = None,
        ephemeral: Optional[bool] = None,
        judge_url: Optional[str] = None,
        https_judge_url: Optional[str] = None,
    ) -> None:
        changes: dict[str, Any] = {
            "timeout": timeout,
            "concurrency": concurrency,
            "retries": retries,
            "https_check": https_check,
            "max_proxies": max_proxies,
            "show_dead": show_dead,
            "attach_file": attach_file,
            "ephemeral": ephemeral,
            "judge_url": judge_url,
            "https_judge_url": https_judge_url,
        }
        if all(value is None for value in changes.values()):
            await interaction.response.send_message(
                embed=error_embed(
                    "変更する項目がありません",
                    "変更したいオプションを1つ以上指定してください。現在値は `/config view` で確認できます。",
                ),
                ephemeral=True,
            )
            return

        applied, notes = self.store.apply(scope_of(interaction), changes)

        embed = discord.Embed(
            title="⚙️ 設定を更新しました" if applied else "⚠️ 設定は変更されていません",
            color=COLOR_OK if applied else COLOR_FAIL,
        )
        if applied:
            embed.description = "\n".join(
                f"• **{option.label}** → `{option.format(applied[option.key])}`"
                for option in OPTIONS
                if option.key in applied
            )
        if notes:
            embed.add_field(name="補足", value=clip("\n".join(f"• {note}" for note in notes), FIELD_LIMIT), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="reset", description="設定を既定値に戻します")
    async def config_reset(self, interaction: discord.Interaction) -> None:
        changed = self.store.reset(scope_of(interaction))
        embed = discord.Embed(
            title="⚙️ 設定を既定値に戻しました" if changed else "⚙️ すでに既定値です",
            description="`/config view` で確認できます。",
            color=COLOR_OK if changed else COLOR_INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /help
    # ------------------------------------------------------------------ #

    @app_commands.command(name="help", description="使い方を表示します")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🛰️ プロキシチェッカーの使い方",
            description=(
                "HTTP / HTTPS プロキシの **生存 ・ 応答速度 ・ 出口IP ・ 国名** を判定します。\n"
                "外部のプロキシ用ライブラリは使わず、生の TCP 接続に HTTP を直接書き込んで確認しています。"
            ),
            color=COLOR_INFO,
        )
        embed.add_field(
            name="① チェックする",
            value=(
                "`/check proxies: 1.2.3.4:8080, 5.6.7.8:3128`\n"
                "引数なしで `/check` を実行すると、**複数行そのまま貼れる入力ウィンドウ**が開きます。"
            ),
            inline=False,
        )
        embed.add_field(
            name="② 対応している書き方",
            value=(
                "`ip:port` / `http://ip:port` / `user:pass@ip:port` / `ip:port:user:pass`\n"
                "区切りは改行・カンマ・空白・セミコロンのどれでも可。重複は自動で除去します。"
            ),
            inline=False,
        )
        embed.add_field(
            name="③ 結果の見かた",
            value=(
                "🟢 <0.5秒 ・ 🟡 <1.5秒 ・ 🟠 <3秒 ・ 🔴 それ以上\n"
                "`🔒 HTTPS可` は CONNECT で HTTPS 中継もできたプロキシ、\n"
                "`🚇 CONNECT専用` は平文HTTPの中継は拒否するがHTTPSなら使えるプロキシです。\n"
                "セレクトメニューで表示切替、◀▶ でページ送り、📄 でテキスト出力、🔄 で失敗分だけ再チェックできます。"
            ),
            inline=False,
        )
        embed.add_field(
            name="④ 設定 (トークン以外はすべてコマンド)",
            value=(
                "`/config view` 現在の設定を表示\n"
                "`/config set timeout: 5 concurrency: 80` のように変更\n"
                "`/config reset` 既定値に戻す\n"
                "変更できる項目: " + " ".join(f"`{option.key}`" for option in OPTIONS)
            ),
            inline=False,
        )
        embed.set_footer(text="自分が利用を許可されたプロキシに対してのみ使用してください。")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # エラー処理
    # ------------------------------------------------------------------ #

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            embed = error_embed("権限がありません", "この操作には「サーバー管理」権限が必要です。")
        elif isinstance(error, app_commands.CommandOnCooldown):
            embed = error_embed("クールダウン中", f"{error.retry_after:.0f} 秒後にもう一度お試しください。")
        else:
            logger.exception("コマンドでエラー: %s", error)
            embed = error_embed("エラーが発生しました", "処理中に問題が起きました。もう一度お試しください。")
        try:
            await respond(interaction, embed)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProxyChecker(bot))
