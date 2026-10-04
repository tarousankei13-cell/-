"""
サーバー監視

入退室・ロール変更・削除されたメッセージなどを記録し、
荒らしとスパムを見つけて対処する。

⚠️ ここのイベントは**全部のメッセージ・全部の入退室で呼ばれる**。
   重い処理（DB・通信）を無条件で入れないこと。
   「設定が切れているか」を最初に見て、すぐ戻るようにしてある。

⚠️ 記録先チャンネル自身の出来事は記録しない（記録が記録を呼ぶため）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import config
import emoji as E
from core import settings
from db.models import GuardHit, as_utc
from db.session import session_scope
from services.server import guard, logs, mod
from cogs._checks import admin_only, handle_check_failure
from ui import embeds

log = logging.getLogger("bot.cogs.guard")


async def _record(
    guild_id: int, user_id: int, hit: guard.Hit, action: str,
) -> None:
    """検知と対処を残す。何に反応したかを説明できるようにしておく。"""
    try:
        async with session_scope() as s:
            s.add(GuardHit(
                guild_id=guild_id, user_id=user_id, kind=hit.kind,
                detail=hit.detail[:2000], action=action,
            ))
    except Exception:
        log.warning("検知の記録に失敗しました", exc_info=True)


class GuardCog(commands.Cog):
    """サーバー監視"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="guard", description="サーバー監視（管理者用）",
    )

    # ========================================================
    #  設定コマンド
    # ========================================================

    @group.command(name="setup", description="記録先と、記録する内容を設定します")
    @app_commands.describe(
        log_channel="出来事を記録するチャンネル",
        mod_channel="処分を記録するチャンネル（省略すると上と同じ）",
    )
    @admin_only()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel | None = None,
        mod_channel: discord.TextChannel | None = None,
    ) -> None:
        by = interaction.user.id
        if log_channel is not None:
            await settings.set_value("guard_log_channel", log_channel.id, updated_by=by)
        if mod_channel is not None:
            await settings.set_value("mod_log_channel", mod_channel.id, updated_by=by)
        logs.reset_warnings()
        await interaction.response.send_message(
            embed=embeds.ok(self._status_text(interaction.guild)), ephemeral=True,
        )

    def _status_text(self, guild: discord.Guild | None) -> str:
        chosen = set(settings.get("guard_events") or [])
        lcid = logs.log_channel_id()
        mcid = settings.get("mod_log_channel")

        rows = []
        for key, name in logs.EVENTS.items():
            mark = E.OK if key in chosen else E.WAIT
            extra = ""
            if key in logs.NEEDS_CONTENT and key in chosen:
                if not self._has_content():
                    extra = f"　{E.WARN} 内容は空になります"
            rows.append(f"{mark} {name}{extra}")

        body = (
            f"**記録先**　{f'<#{lcid}>' if lcid else '**未設定**'}\n"
            f"**処分の記録先**　{f'<#{mcid}>' if mcid else '（上と同じ）'}\n\n"
            "**記録する内容**\n" + "\n".join(rows)
        )

        raid = "ON" if settings.get("guard_raid_enabled") else "OFF"
        spam = "ON" if settings.get("guard_spam_enabled") else "OFF"
        ment = int(settings.get("guard_mention_limit", 0) or 0)
        body += (
            f"\n\n**荒らし検知**　{raid}"
            f"（{settings.get('guard_raid_seconds')}秒で"
            f"{settings.get('guard_raid_joins')}人）\n"
            f"**連投検知**　　{spam}"
            f"（{settings.get('guard_spam_seconds')}秒で"
            f"{settings.get('guard_spam_messages')}件）\n"
            f"**メンション上限**　{ment if ment else '無制限'}\n"
            f"**招待リンク**　{'止める' if settings.get('guard_invite_block') else '素通し'}\n"
            f"**NGワード**　{len(settings.get('guard_words') or [])} 語"
        )

        todo = []
        if not lcid:
            todo.append("記録先が未設定です（`/guard setup log_channel:`）")
        if not self._has_members():
            todo.append(
                "入退室の記録には **SERVER MEMBERS INTENT** が必要です。"
                "main.py の `SERVER_MANAGEMENT = True` と、Developer Portal の"
                "設定をご確認ください"
            )
        if not self._has_content():
            needs = [logs.EVENTS[k] for k in logs.NEEDS_CONTENT if k in chosen]
            if needs or settings.get("guard_invite_block") or settings.get("guard_words"):
                todo.append(
                    "メッセージの内容を読む機能には **MESSAGE CONTENT INTENT** が"
                    "必要です。main.py の `MESSAGE_CONTENT = True` にしてください"
                )
        if guild is not None and guild.me is not None:
            p = guild.me.guild_permissions
            lack = [n for n, ok in (
                ("メンバーをタイムアウト", p.moderate_members),
                ("メッセージの管理", p.manage_messages),
            ) if not ok]
            if lack:
                todo.append("BOTに次の権限が足りません：" + "、".join(lack))
        if todo:
            body += f"\n\n{E.WARN} **ご確認ください**\n" + "\n".join(f"・{t}" for t in todo)
        return body

    def _has_members(self) -> bool:
        return bool(self.bot.intents.members)

    def _has_content(self) -> bool:
        return bool(self.bot.intents.message_content)

    @group.command(name="status", description="監視の設定を表示します")
    @admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=embeds.info(
                self._status_text(interaction.guild),
                title=f"{E.SHIELD} サーバー監視",
            ),
            ephemeral=True,
        )

    @group.command(name="events", description="記録する内容を切り替えます")
    @app_commands.describe(event="対象", record="記録するかどうか")
    @app_commands.choices(event=[
        app_commands.Choice(name="入室", value="join"),
        app_commands.Choice(name="退室", value="leave"),
        app_commands.Choice(name="BAN・BAN解除", value="ban"),
        app_commands.Choice(name="ロールの変更", value="role"),
        app_commands.Choice(name="表示名の変更", value="nick"),
        app_commands.Choice(name="発言停止", value="timeout"),
        app_commands.Choice(name="メッセージの削除", value="msgdelete"),
        app_commands.Choice(name="メッセージの編集", value="msgedit"),
        app_commands.Choice(name="チャンネルの作成・削除", value="channel"),
        app_commands.Choice(name="ボイスチャンネルの入退室", value="voice"),
    ])
    @admin_only()
    async def events(
        self, interaction: discord.Interaction,
        event: app_commands.Choice[str], record: bool,
    ) -> None:
        have = list(settings.get("guard_events") or [])
        if record and event.value not in have:
            have.append(event.value)
        elif not record:
            have = [e for e in have if e != event.value]
        await settings.set_value("guard_events", have, updated_by=interaction.user.id)

        note = ""
        if record and event.value in logs.NEEDS_CONTENT and not self._has_content():
            note = (
                f"\n\n{E.WARN} この記録には **MESSAGE CONTENT INTENT** が要ります。"
                "いまの設定では「誰がいつ消したか」だけが残り、"
                "**内容は空**になります。\n"
                "main.py の `MESSAGE_CONTENT = True` にして、"
                "Developer Portal → Bot でも有効にしてください。"
            )
        await interaction.response.send_message(
            embed=embeds.ok(
                f"「{event.name}」を{'記録します' if record else '記録しません'}。" + note
            ),
            ephemeral=True,
        )

    @group.command(name="raid", description="短時間の大量入室への備えを設定します")
    @app_commands.describe(
        enabled="検知するかどうか",
        joins="何人で",
        seconds="何秒以内なら荒らしとみなすか",
        action="見つけたときの動き",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="知らせるだけ", value="notify"),
        app_commands.Choice(name="サーバーの認証レベルを上げる", value="lockdown"),
    ])
    @admin_only()
    async def raid(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        joins: app_commands.Range[int, 2, 50] | None = None,
        seconds: app_commands.Range[int, 3, 300] | None = None,
        action: app_commands.Choice[str] | None = None,
    ) -> None:
        by = interaction.user.id
        await settings.set_value("guard_raid_enabled", enabled, updated_by=by)
        if joins is not None:
            await settings.set_value("guard_raid_joins", joins, updated_by=by)
        if seconds is not None:
            await settings.set_value("guard_raid_seconds", seconds, updated_by=by)
        if action is not None:
            await settings.set_value("guard_raid_action", action.value, updated_by=by)

        body = (
            f"荒らし検知を **{'ON' if enabled else 'OFF'}** にしました。\n"
            f"{settings.get('guard_raid_seconds')}秒以内に "
            f"{settings.get('guard_raid_joins')} 人入室したら"
            f"{'知らせます' if settings.get('guard_raid_action') == 'notify' else '認証レベルを上げます'}。"
        )
        if not self._has_members():
            body += (
                f"\n\n{E.WARN} 入室を見るには **SERVER MEMBERS INTENT** が要ります。"
                "いまの設定では動きません。"
            )
        if settings.get("guard_raid_action") == "lockdown":
            body += (
                f"\n\n{E.INFO} 認証レベルを上げると、電話番号を登録していない方が"
                "発言できなくなります。`/guard unlock` で元に戻せます。"
            )
        await interaction.response.send_message(embed=embeds.ok(body), ephemeral=True)

    @group.command(name="unlock", description="上げた認証レベルを元に戻します")
    @admin_only()
    async def unlock(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.guild.edit(
                verification_level=discord.VerificationLevel.medium,
                reason=f"荒らし対応の解除（{interaction.user}）",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error(
                    "変えられませんでした。BOTに「サーバーの管理」権限が必要です。"
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=embeds.ok("認証レベルを「中」に戻しました。"), ephemeral=True,
        )

    @group.command(name="spam", description="連投・メンション爆撃への備えを設定します")
    @app_commands.describe(
        enabled="連投を見るかどうか",
        messages="何件を",
        seconds="何秒以内に出したら",
        action="見つけたときの動き",
        mention_limit="1通に入れられるメンションの上限（0で無制限）",
        timeout_minutes="自動の発言停止の長さ（分）",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="消す", value="delete"),
        app_commands.Choice(name="発言を止める", value="timeout"),
    ])
    @admin_only()
    async def spam(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        messages: app_commands.Range[int, 2, 50] | None = None,
        seconds: app_commands.Range[int, 1, 120] | None = None,
        action: app_commands.Choice[str] | None = None,
        mention_limit: app_commands.Range[int, 0, 50] | None = None,
        timeout_minutes: app_commands.Range[int, 1, 10080] | None = None,
    ) -> None:
        by = interaction.user.id
        if enabled is not None:
            await settings.set_value("guard_spam_enabled", enabled, updated_by=by)
        if messages is not None:
            await settings.set_value("guard_spam_messages", messages, updated_by=by)
        if seconds is not None:
            await settings.set_value("guard_spam_seconds", seconds, updated_by=by)
        if action is not None:
            await settings.set_value("guard_spam_action", action.value, updated_by=by)
        if mention_limit is not None:
            await settings.set_value(
                "guard_mention_limit", mention_limit, updated_by=by,
            )
        if timeout_minutes is not None:
            await settings.set_value(
                "guard_timeout_minutes", timeout_minutes, updated_by=by,
            )
        body = (
            f"**連投**　{'見る' if settings.get('guard_spam_enabled') else '見ない'}"
            f"（{settings.get('guard_spam_seconds')}秒で"
            f"{settings.get('guard_spam_messages')}件 →"
            f"{'消す' if settings.get('guard_spam_action') == 'delete' else '発言停止'}）\n"
            f"**メンション上限**　"
            f"{settings.get('guard_mention_limit') or '無制限'}\n"
            f"**自動の発言停止**　{settings.get('guard_timeout_minutes')}分\n\n"
            f"{E.INFO} 管理者と、除外ロールをお持ちの方は対象外です。"
        )
        if not self._has_content():
            body += (
                f"\n{E.INFO} いまは内容を読まないため、**投稿の速さ**と"
                "**メンションの数**だけで見ます"
                "（同じ文の繰り返しは見られません）。"
            )
        await interaction.response.send_message(embed=embeds.ok(body), ephemeral=True)

    @group.command(name="words", description="NGワードを設定します")
    @app_commands.describe(
        words="「,」で区切って並べます。空にすると全部消します",
        block_invites="他サーバーの招待リンクを消すかどうか",
    )
    @admin_only()
    async def words(
        self, interaction: discord.Interaction,
        words: str | None = None, block_invites: bool | None = None,
    ) -> None:
        by = interaction.user.id
        if words is not None:
            made = [w.strip() for w in words.split(",") if w.strip()][:200]
            await settings.set_value("guard_words", made, updated_by=by)
        if block_invites is not None:
            await settings.set_value("guard_invite_block", block_invites, updated_by=by)

        have = settings.get("guard_words") or []
        body = (
            f"**NGワード**　{len(have)} 語\n"
            f"**招待リンク**　"
            f"{'消します' if settings.get('guard_invite_block') else '素通しします'}\n\n"
            f"{E.INFO} 全角・半角、大文字・小文字の違いは同じものとして扱います。"
            "字の間に空白を入れる書き方も見ます。"
        )
        if not self._has_content():
            body += (
                f"\n\n{E.WARN} いまは **MESSAGE CONTENT INTENT** が無いため、"
                "この設定は**効きません**。\n"
                "main.py の `MESSAGE_CONTENT = True` にして、"
                "Developer Portal → Bot でも有効にしてください。"
            )
        await interaction.response.send_message(embed=embeds.ok(body), ephemeral=True)

    @group.command(name="exempt", description="検知の対象から外すロールを決めます")
    @app_commands.describe(role="対象のロール", remove="外す場合は True")
    @admin_only()
    async def exempt(
        self, interaction: discord.Interaction,
        role: discord.Role, remove: bool = False,
    ) -> None:
        have = [int(r) for r in (settings.get("guard_exempt_roles") or [])]
        if remove:
            have = [r for r in have if r != role.id]
        elif role.id not in have:
            have.append(role.id)
        await settings.set_value("guard_exempt_roles", have, updated_by=interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.ok(
                f"{role.mention} を検知の対象から"
                f"{'戻しました' if remove else '外しました'}。\n"
                f"{E.INFO} いまの除外ロール　"
                + ("　".join(f"<@&{r}>" for r in have) if have else "（なし）")
            ),
            ephemeral=True,
        )

    @group.command(name="counter", description="メンバー数をチャンネル名に出します")
    @app_commands.describe(
        channel="名前を変えるチャンネル（ボイスチャンネルがおすすめ）",
        text="表示の形。{count} が人数に置き換わります",
    )
    @admin_only()
    async def counter(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.TextChannel | None = None,
        text: str | None = None,
    ) -> None:
        by = interaction.user.id
        if channel is not None:
            await settings.set_value("guard_counter_channel", channel.id, updated_by=by)
        if text is not None:
            if "{count}" not in text:
                await interaction.response.send_message(
                    embed=embeds.error("`{count}` を必ず入れてください。"),
                    ephemeral=True,
                )
                return
            await settings.set_value("guard_counter_format", text[:90], updated_by=by)

        cid = settings.get("guard_counter_channel")
        if not cid:
            await interaction.response.send_message(
                embed=embeds.warn("チャンネルをご指定ください。"), ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await self.update_counter()
        body = (
            f"**チャンネル**　<#{cid}>\n"
            f"**表示**　`{settings.get('guard_counter_format')}`\n\n"
        )
        body += (
            f"{E.OK} いまの表示　**{detail}**\n"
            f"{E.INFO} {config.GUARD_COUNTER_MINUTES}分おきに更新します"
            "（チャンネル名は10分に2回までしか変えられないDiscordの制限のため）。"
            if ok else f"{E.NG} 変えられませんでした（{detail}）"
        )
        await interaction.followup.send(embed=embeds.ok(body), ephemeral=True)

    @group.command(name="recent", description="最近の検知を表示します")
    @admin_only()
    async def recent(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            rows = list((await s.execute(
                select(GuardHit)
                .where(GuardHit.guild_id == interaction.guild_id)
                .order_by(GuardHit.id.desc()).limit(15)
            )).scalars().all())
        if not rows:
            await interaction.followup.send(
                embed=embeds.info("まだ検知はありません。"), ephemeral=True,
            )
            return
        acts = {"delete": "消した", "timeout": "発言停止",
                "kick": "キック", "ban": "BAN", "none": "記録のみ"}
        lines = [
            f"<t:{int(as_utc(r.created_at).timestamp())}:R>　"
            f"**{guard.KIND_LABEL.get(r.kind, r.kind)}**　<@{r.user_id}>　"
            f"{acts.get(r.action, r.action)}\n　{r.detail[:120]}"
            for r in rows
        ]
        await interaction.followup.send(
            embed=embeds.info("\n".join(lines)[:4000],
                              title=f"{E.SIREN} 最近の検知"),
            ephemeral=True,
        )

    # ========================================================
    #  メンバー数カウンター
    # ========================================================

    async def update_counter(self) -> tuple[bool, str]:
        """チャンネル名を今の人数にする。"""
        cid = settings.get("guard_counter_channel")
        if not cid:
            return False, "未設定"
        channel = self.bot.get_channel(int(cid))
        if channel is None:
            return False, "チャンネルが見つかりません"
        guild = channel.guild
        count = guild.member_count or len(guild.members)
        fmt = str(settings.get("guard_counter_format") or config.GUARD_COUNTER_FORMAT)
        try:
            name = fmt.format(count=count)[:100]
        except (KeyError, IndexError):
            name = config.GUARD_COUNTER_FORMAT.format(count=count)
        if channel.name == name:
            return True, name
        try:
            await channel.edit(name=name, reason="メンバー数の更新")
        except discord.Forbidden:
            return False, "権限がありません（チャンネルの管理）"
        except discord.HTTPException as e:
            return False, str(e)[:80]
        return True, name

    # ========================================================
    #  出来事の記録
    # ========================================================

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        # ① 入室の記録
        age = guard.account_age_days(member)
        await logs.send(self.bot, event="join", embed=embeds.guard_log(
            title=f"{E.IN} 入室",
            color=embeds.GREEN,
            lines=[
                ("どなた", logs.who(member)),
                ("アカウント作成", f"<t:{int(member.created_at.timestamp())}:R>"
                 + (f"（{age:.1f}日前）" if age is not None else "")),
                ("いまの人数", f"{member.guild.member_count} 人"),
            ],
        ))

        # ② 作りたてのアカウント
        newish = guard.new_account_hit(member)
        if newish:
            await _record(member.guild.id, member.id, newish, guard.ACT_NONE)
            await logs.send(self.bot, to_mod=True, embed=embeds.guard_log(
                title=f"{E.WARN} 作りたてのアカウントが入室しました",
                color=embeds.YELLOW,
                lines=[("どなた", logs.who(member)), ("内容", newish.detail)],
            ))

        # ③ 荒らし（短時間の大量入室）
        raid = guard.detector.note_join(member.guild.id)
        if raid:
            await self._on_raid(member.guild, raid)

    async def _on_raid(self, g: discord.Guild, hit: guard.Hit) -> None:
        """荒らしを見つけたときの動き。"""
        await _record(g.id, 0, hit, guard.ACT_NONE)
        # 同じ波で何度も鳴らさない
        guard.detector.reset_joins(g.id)

        action = str(settings.get("guard_raid_action", "notify"))
        done = "知らせるだけの設定です"
        if action == "lockdown":
            try:
                await g.edit(
                    verification_level=discord.VerificationLevel.high,
                    reason="短時間の大量入室を検知",
                )
                done = (
                    "サーバーの認証レベルを「高」に上げました。"
                    "`/guard unlock` で戻せます。"
                )
            except discord.Forbidden:
                done = (
                    "認証レベルを上げられませんでした"
                    "（BOTに「サーバーの管理」権限が必要です）"
                )
            except discord.HTTPException as e:
                done = f"認証レベルを上げられませんでした（{e}）"

        await logs.send(self.bot, to_mod=True, embed=embeds.guard_log(
            title=f"{E.SIREN} 短時間に大量の入室がありました",
            color=embeds.RED,
            lines=[("状況", hit.detail), ("BOTの対応", done)],
        ))
        log.warning("荒らしを検知しました: %s（%s）", g.name, hit.detail)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        await logs.send(self.bot, event="leave", embed=embeds.guard_log(
            title=f"{E.OUT} 退室",
            color=embeds.GREY,
            lines=[
                ("どなた", logs.who(member)),
                ("入室していた期間",
                 f"<t:{int(member.joined_at.timestamp())}:R> から"
                 if member.joined_at else "（不明）"),
                ("持っていたロール", logs.trim("　".join(roles) if roles else None)),
            ],
        ))

    @commands.Cog.listener()
    async def on_member_ban(
        self, g: discord.Guild, user: discord.abc.User,
    ) -> None:
        await logs.send(self.bot, event="ban", embed=embeds.guard_log(
            title=f"{E.HAMMER} BANされました",
            color=embeds.RED,
            lines=[("どなた", logs.who(user))],
        ))

    @commands.Cog.listener()
    async def on_member_unban(
        self, g: discord.Guild, user: discord.abc.User,
    ) -> None:
        await logs.send(self.bot, event="ban", embed=embeds.guard_log(
            title=f"{E.OK} BANが解除されました",
            color=embeds.GREEN,
            lines=[("どなた", logs.who(user))],
        ))

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member,
    ) -> None:
        # ロールの変更
        if before.roles != after.roles:
            gained = [r.mention for r in after.roles if r not in before.roles]
            lost = [r.mention for r in before.roles if r not in after.roles]
            rows = [("どなた", logs.who(after))]
            if gained:
                rows.append((f"{E.PLUS} 付いた", logs.trim("　".join(gained))))
            if lost:
                rows.append((f"{E.MINUS} 外れた", logs.trim("　".join(lost))))
            if len(rows) > 1:
                await logs.send(self.bot, event="role", embed=embeds.guard_log(
                    title="ロールが変わりました", color=embeds.BLUE, lines=rows,
                ))

        # 表示名の変更
        if before.nick != after.nick:
            await logs.send(self.bot, event="nick", embed=embeds.guard_log(
                title=f"{E.PENCIL} 表示名が変わりました",
                color=embeds.BLUE,
                lines=[
                    ("どなた", logs.who(after)),
                    ("前", logs.trim(before.nick or before.name)),
                    ("後", logs.trim(after.nick or after.name)),
                ],
            ))

        # 発言停止
        b_to = getattr(before, "timed_out_until", None)
        a_to = getattr(after, "timed_out_until", None)
        if b_to != a_to:
            if a_to is not None:
                rows = [
                    ("どなた", logs.who(after)),
                    ("解除される時刻", f"<t:{int(a_to.timestamp())}:F>"),
                ]
                title, color = f"{E.MUTE} 発言を止められました", embeds.RED
            else:
                rows = [("どなた", logs.who(after))]
                title, color = f"{E.OK} 発言停止が解除されました", embeds.GREEN
            await logs.send(self.bot, event="timeout", embed=embeds.guard_log(
                title=title, color=color, lines=rows,
            ))

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        # ⚠️ 記録先そのものは見ない（記録が記録を呼ぶため）
        if logs.is_log_channel(message.channel.id):
            return
        body = message.content or ""
        if not body and not self.bot.intents.message_content:
            body = "（内容を読む権限が無いため記録できません）"
        rows = [
            ("どなた", logs.who(message.author)),
            ("どこ", f"<#{message.channel.id}>"),
            ("内容", logs.trim(body)),
        ]
        if message.attachments:
            rows.append((
                "添付",
                logs.trim("\n".join(a.filename for a in message.attachments)),
            ))
        await logs.send(self.bot, event="msgdelete", embed=embeds.guard_log(
            title=f"{E.TRASH} メッセージが消されました",
            color=embeds.RED, lines=rows,
        ))

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message,
    ) -> None:
        if after.guild is None or after.author.bot:
            return
        if logs.is_log_channel(after.channel.id):
            return
        if before.content == after.content:
            return          # 埋め込みが後から付いただけ
        await logs.send(self.bot, event="msgedit", embed=embeds.guard_log(
            title=f"{E.PENCIL} メッセージが編集されました",
            color=embeds.BLUE,
            lines=[
                ("どなた", logs.who(after.author)),
                ("どこ", f"<#{after.channel.id}>　[移動]({after.jump_url})"),
                ("前", logs.trim(before.content)),
                ("後", logs.trim(after.content)),
            ],
        ))

    @commands.Cog.listener()
    async def on_guild_channel_create(
        self, channel: discord.abc.GuildChannel,
    ) -> None:
        await logs.send(self.bot, event="channel", embed=embeds.guard_log(
            title=f"{E.PLUS} チャンネルが作られました",
            color=embeds.GREEN,
            lines=[("どこ", f"{channel.mention}　`{channel.name}`")],
        ))

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel,
    ) -> None:
        await logs.send(self.bot, event="channel", embed=embeds.guard_log(
            title=f"{E.MINUS} チャンネルが消されました",
            color=embeds.RED,
            lines=[("どこ", f"`{channel.name}`")],
        ))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member,
        before: discord.VoiceState, after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return          # ミュートの切り替えなどは記録しない
        if after.channel is None:
            what, color = f"{E.OUT} ボイスから退出", embeds.BLUE
            where = f"`{before.channel.name}`"
        elif before.channel is None:
            what, color = f"{E.SPEAK} ボイスに参加", embeds.GREEN
            where = f"`{after.channel.name}`"
        else:
            what, color = f"{E.SPEAK} ボイスを移動", embeds.BLUE
            where = f"`{before.channel.name}` → `{after.channel.name}`"
        await logs.send(self.bot, event="voice", embed=embeds.guard_log(
            title=what, color=color,
            lines=[("どなた", logs.who(member)), ("どこ", where)],
        ))

    # ========================================================
    #  スパム・荒らしの検知
    # ========================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        ⚠️ 全メッセージを通る。軽い条件から順に見て、すぐ戻ること。
        """
        if message.guild is None or message.author.bot:
            return
        if logs.is_log_channel(message.channel.id):
            return

        # 設定が全部切れていれば、ここで戻る（一番多い経路）
        if not (settings.get("guard_spam_enabled")
                or int(settings.get("guard_mention_limit", 0) or 0) > 0
                or settings.get("guard_invite_block")
                or settings.get("guard_words")):
            return

        if guard.exempt(message.author):
            return

        hits = guard.detector.check_message(
            message, has_content=bool(self.bot.intents.message_content),
        )
        if not hits:
            return

        # 一番強い対処に合わせる
        worst = guard.ACT_DELETE if any(
            h.action == guard.ACT_DELETE for h in hits
        ) else guard.ACT_NONE
        if any(h.action == guard.ACT_TIMEOUT for h in hits):
            worst = guard.ACT_TIMEOUT

        done = []
        if worst in (guard.ACT_DELETE, guard.ACT_TIMEOUT):
            try:
                await message.delete()
                done.append("メッセージを消しました")
            except (discord.Forbidden, discord.NotFound):
                done.append("メッセージを消せませんでした（権限不足）")

        if worst == guard.ACT_TIMEOUT:
            minutes = int(settings.get("guard_timeout_minutes", 10) or 10)
            try:
                await message.author.timeout(
                    timedelta(minutes=minutes), reason="自動検知",
                )
                done.append(f"{minutes}分の発言停止にしました")
                await mod.notify(
                    message.author, guild_name=message.guild.name,
                    action=f"{minutes}分の発言停止",
                    reason="／".join(h.label for h in hits),
                    extra="心当たりがない場合は、管理者にお知らせください。",
                )
            except discord.Forbidden:
                done.append("発言を止められませんでした（権限不足）")
            except discord.HTTPException as e:
                done.append(f"発言を止められませんでした（{e}）")

        for h in hits:
            await _record(message.guild.id, message.author.id, h, worst)

        await logs.send(self.bot, to_mod=True, embed=embeds.guard_log(
            title=f"{E.SIREN} 自動で対処しました",
            color=embeds.RED,
            lines=[
                ("どなた", logs.who(message.author)),
                ("どこ", f"<#{message.channel.id}>"),
                ("見つけたもの",
                 "\n".join(f"・{h.label}　{h.detail}" for h in hits)),
                ("BOTの対応", "\n".join(f"・{d}" for d in done) or "記録のみ"),
            ],
            footer="誤りだった場合は /guard exempt でロールを対象外にできます",
        ))

    async def cog_app_command_error(
        self, interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("guard コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GuardCog(bot))
