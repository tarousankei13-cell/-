"""実績（商品レポート）の2段階フロー。

1段階目: 注文が確定した時点で、プライバシーに配慮した内容を自動投稿する（panel.py 側）
2段階目: 利用者が DM で感想を送る → 本人が承諾 → オーナーが承認 → 実績チャンネルへ投稿

2段階目が終わるまで次の注文はできない。画像を添えると残高ボーナスが付く。
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mcd.cards import dhash
from mcd.store import K_PHOTO, now_jst

from ._shared import BAD, INFO, MONEY, OK, WARN, deny, dm, embed, post, reply, yen

log = logging.getLogger("bot.reports")

REPORT_COLOR = discord.Color.from_rgb(230, 126, 34)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


# ============================================================== 本人の承諾


class SubmitView(discord.ui.View):
    def __init__(self, cog: "Reports", user_id: int, payload: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.user_id = user_id
        self.payload = payload

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="この内容で投稿する", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)
        await self.cog.submit_report(interaction, self.payload)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        cfg = self.cog.bot.cfg
        await interaction.response.edit_message(
            embed=embed(
                "投稿を取りやめました",
                f"感想はまだ提出されていません。{cfg.E_MEMO} もう一度 DM を送ってください。",
                INFO,
            ),
            view=self,
        )


# ============================================================ オーナーの承認


class RejectReasonModal(discord.ui.Modal, title="却下の理由"):
    reason = discord.ui.TextInput(
        label="理由（本人に通知されます）",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=400,
    )

    def __init__(
        self, cog: "Reports", report_id: int, source: Optional[discord.Message] = None
    ):
        super().__init__()
        self.cog = cog
        self.report_id = report_id
        # モーダルの interaction では承認メッセージを直接編集できないので、
        # 押されたボタンが乗っていたメッセージを持ち回る
        self.source = source

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.finalize(
            interaction, self.report_id, False, str(self.reason.value), self.source
        )


class ReportDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"repapv:(?P<rid>\d+):(?P<act>ok|ng)",
):
    def __init__(self, report_id: int, action: str):
        self.report_id = report_id
        self.action = action
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="承認して投稿" if approve else "却下",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"repapv:{report_id}:{action}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["rid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Optional[Reports] = interaction.client.get_cog("Reports")
        if cog is None:
            await interaction.response.send_message("読み込み中です。", ephemeral=True)
            return
        if not await interaction.client.is_owner(interaction.user):
            await interaction.response.send_message("オーナー限定の操作です。", ephemeral=True)
            return
        if self.action == "ng":
            await interaction.response.send_modal(
                RejectReasonModal(cog, self.report_id, interaction.message)
            )
        else:
            await cog.finalize(
                interaction, self.report_id, True, "", interaction.message
            )


class FlagButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"repflag:(?P<rid>\d+)",
):
    """実績チャンネルに残る通報ボタン。"""

    def __init__(self, report_id: int):
        self.report_id = report_id
        super().__init__(
            discord.ui.Button(
                label="報告",
                style=discord.ButtonStyle.danger,
                custom_id=f"repflag:{report_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Optional[Reports] = interaction.client.get_cog("Reports")
        if cog is None:
            await interaction.response.send_message("読み込み中です。", ephemeral=True)
            return
        await cog.flag_report(interaction, self.report_id)


# ====================================================================== cog


class Reports(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReportDecisionButton, FlagButton)

    # ------------------------------------------------------ DM の受け取り

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is not None:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        await self.handle_dm(message)

    async def handle_dm(self, message: discord.Message) -> None:
        cfg = self.bot.cfg
        uid = message.author.id

        order = await asyncio.to_thread(self.bot.store.blocking_order, uid)
        if order is None:
            return
        if order["report_status"] == "submitted":
            # 送りっぱなしで無反応だと不安になるので状態だけ返す
            await message.reply(
                embed=embed(
                    f"{cfg.E_CLOCK} 承認をお待ちください",
                    f"注文 #{order['id']} の感想は受付済みです。\n"
                    "オーナーの承認が完了すると次の注文ができるようになります。",
                    INFO,
                )
            )
            return
        if order["report_status"] not in ("awaiting", "rejected"):
            return

        text = (message.content or "").strip()
        images = [
            a for a in message.attachments
            if (a.content_type or "").startswith("image/") and a.size <= MAX_IMAGE_BYTES
        ]

        if not text:
            await message.reply(
                embed=embed(
                    f"{cfg.E_WARN} 感想の文章が必要です",
                    "画像だけでは解禁されません。感想を文章で書いて送ってください。",
                    WARN,
                )
            )
            return

        if len(text) < 5:
            await message.reply(
                embed=embed(
                    f"{cfg.E_WARN} 感想が短すぎます",
                    "5 文字以上でお願いします。",
                    WARN,
                )
            )
            return

        # --- 画像の使い回しチェック ---
        hashes: list[str] = []
        blobs: list[tuple[str, bytes]] = []
        for attachment in images:
            try:
                data = await attachment.read()
            except Exception:
                continue
            try:
                phash = await asyncio.to_thread(dhash, data)
            except Exception:
                continue
            verdict = await asyncio.to_thread(self.bot.fraud.check_image, uid, phash)
            if verdict.blocked:
                await asyncio.to_thread(self.bot.fraud.record, uid, verdict, "report.image")
                await message.reply(
                    embed=embed(
                        f"{cfg.E_NG} 使い回しの画像が含まれています",
                        f"{verdict.reasons()}\n\n今回の注文で撮影した画像を添えてください。",
                        BAD,
                    )
                )
                await post(
                    self.bot,
                    cfg.APPROVAL_CHANNEL_ID,
                    embed=embed(
                        f"{cfg.E_FLAG} 画像の使い回しを検知",
                        f"<@{uid}> ・ 注文 #{order['id']}\n\n{verdict.reasons()}",
                        WARN,
                    ),
                )
                return
            hashes.append(phash)
            blobs.append((attachment.filename, data))

        payload = {
            "order_id": int(order["id"]),
            "content": text[:1800],
            "hashes": hashes,
            "blobs": blobs,
            "pickup": order["pickup"] or "",
            "source_channel_id": message.channel.id,
            "source_message_id": message.id,
        }

        if blobs:
            bonus = (
                f"\n{cfg.E_CAMERA} 画像 {len(blobs)} 枚 → "
                f"承認されると **+{yen(cfg.PHOTO_BONUS)}**"
            )
        else:
            bonus = (
                f"\n{cfg.E_CAMERA} 画像なし"
                f"（画像を添えると +{yen(cfg.PHOTO_BONUS)}）"
            )

        preview = embed(
            f"{cfg.E_MEMO} この内容で投稿しますか？",
            f"注文 #{order['id']} の感想として実績チャンネルに投稿します。{bonus}",
            WARN,
            footer=f"{cfg.BRAND_NAME} ・ オーナーの承認後に公開されます",
        )
        preview.add_field(name="感想", value=text[:1000], inline=False)
        preview.add_field(name="受取方法", value=order["pickup"] or "-", inline=True)

        await message.reply(embed=preview, view=SubmitView(self, uid, payload))

    # ------------------------------------------------------- 本人承諾のあと

    async def submit_report(self, interaction: discord.Interaction, payload: dict) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id

        report_id = await asyncio.to_thread(
            self.bot.store.create_report,
            payload["order_id"], uid, payload["content"], len(payload["blobs"]),
            payload["hashes"], payload["source_channel_id"], payload["source_message_id"],
        )
        self.bot._report_blobs = getattr(self.bot, "_report_blobs", {})
        self.bot._report_blobs[report_id] = payload["blobs"]

        view = discord.ui.View(timeout=None)
        view.add_item(ReportDecisionButton(report_id, "ok"))
        view.add_item(ReportDecisionButton(report_id, "ng"))

        approval = embed(
            f"{cfg.E_MEMO} 商品レポートの承認待ち",
            payload["content"][:1500],
            WARN,
            footer=f"report #{report_id} ・ order #{payload['order_id']}",
        )
        approval.add_field(name="投稿者", value=f"<@{uid}>", inline=True)
        approval.add_field(name="受取方法", value=payload["pickup"] or "-", inline=True)
        approval.add_field(
            name="画像",
            value=f"{len(payload['blobs'])} 枚"
            + (f"（承認で +{yen(cfg.PHOTO_BONUS)}）" if payload["blobs"] else ""),
            inline=True,
        )

        files = [
            discord.File(io.BytesIO(data), filename=name)
            for name, data in payload["blobs"][:10]
        ]
        posted = await post(
            self.bot, cfg.APPROVAL_CHANNEL_ID, embed=approval, view=view, files=files
        )
        if posted is None:
            await interaction.followup.send(
                embed=embed(
                    f"{cfg.E_NG} 承認チャンネルが未設定です",
                    "オーナーに連絡してください。",
                    BAD,
                )
            )
            return

        # 承認待ち投稿の場所を残す。再起動でメモリ上の画像が消えても、
        # 承認時にここから添付を取り直せる。
        await asyncio.to_thread(
            self.bot.store.set_report_approval_message,
            report_id, posted.channel.id, posted.id,
        )

        await interaction.followup.send(
            embed=embed(
                f"{cfg.E_OK} 感想を受け付けました",
                "オーナーの承認が完了すると次の注文ができるようになります。\n"
                "結果は DM でお知らせします。",
                OK,
            )
        )
        await asyncio.to_thread(
            self.bot.store.audit, "report.submitted", uid,
            {"report": report_id, "order": payload["order_id"], "images": len(payload["blobs"])},
        )

    # ---------------------------------------------------------- 承認・却下

    @staticmethod
    async def _ack(interaction: discord.Interaction, e: discord.Embed) -> None:
        """defer(thinking=True) で出した仮メッセージを結果で埋める。

        followup.send を使うと仮メッセージとは別にもう1通出てしまうため、
        元の応答を差し替える。
        """
        try:
            await interaction.edit_original_response(embed=e)
        except Exception:
            try:
                await interaction.followup.send(embed=e, ephemeral=True)
            except Exception:
                log.debug("操作結果を返せませんでした", exc_info=True)

    async def _close_approval(
        self, source: Optional[discord.Message], e: discord.Embed
    ) -> None:
        """承認待ちの投稿をボタンごと結果表示に差し替える。"""
        if source is None:
            return
        try:
            await source.edit(embed=e, view=None)
        except Exception:
            log.debug("承認メッセージを更新できませんでした", exc_info=True)

    async def finalize(
        self,
        interaction: discord.Interaction,
        report_id: int,
        approve: bool,
        reason: str,
        source: Optional[discord.Message] = None,
    ) -> None:
        cfg = self.bot.cfg
        report = await asyncio.to_thread(self.bot.store.get_report, report_id)
        if report is None or report["status"] != "pending":
            if not interaction.response.is_done():
                await interaction.response.send_message("この申請は処理済みです。", ephemeral=True)
            else:
                await interaction.followup.send("この申請は処理済みです。", ephemeral=True)
            return

        uid = int(report["user_id"])
        order_id = int(report["order_id"])

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        await asyncio.to_thread(
            self.bot.store.decide_report, report_id, approve, interaction.user.id, reason
        )

        if not approve:
            await self._close_approval(
                source,
                embed(
                    f"{cfg.E_NG} 却下しました",
                    f"report #{report_id} ・ <@{uid}>\n理由: {reason}",
                    BAD,
                ),
            )
            await self._ack(
                interaction, embed(f"{cfg.E_NG} 却下しました", f"report #{report_id}", BAD)
            )
            await dm(
                self.bot, uid,
                e=embed(
                    f"{cfg.E_NG} 感想が却下されました",
                    f"理由: {reason}\n\nもう一度 DM で感想を送り直してください。\n"
                    "承認されるまで次の注文はできません。",
                    BAD,
                ),
            )
            await asyncio.to_thread(
                self.bot.store.audit, "report.rejected", interaction.user.id,
                {"report": report_id, "user": uid, "reason": reason},
            )
            return

        # --- 実績チャンネルへ投稿 ---
        message_id = await self._publish(report_id, uid, report, order_id)
        if message_id:
            await asyncio.to_thread(self.bot.store.set_report_message, report_id, message_id)

        # --- 画像ハッシュを記録（以後の使い回し検知に使う）---
        import json

        for phash in json.loads(report["image_hashes"] or "[]"):
            await asyncio.to_thread(
                self.bot.store.remember_image_hash, phash, uid, report_id
            )

        # --- 画像ボーナス ---
        bonus_paid = 0
        if int(report["image_count"]) > 0 and not int(report["photo_bonus_paid"]):
            balance = await asyncio.to_thread(
                self.bot.store.credit, uid, K_PHOTO, cfg.PHOTO_BONUS,
                f"report:{report_id}", "商品レポートへの画像添付",
            )
            await asyncio.to_thread(self.bot.store.mark_photo_bonus_paid, report_id)
            bonus_paid = cfg.PHOTO_BONUS
            await self.bot.send_log(
                cfg.LOG_MONEY_CHANNEL_ID,
                embed(
                    f"{cfg.E_CAMERA} 画像ボーナス",
                    f"<@{uid}> +{yen(cfg.PHOTO_BONUS)}（残高 {yen(balance)}）",
                    MONEY,
                ),
            )

        await self._close_approval(
            source,
            embed(f"{cfg.E_OK} 承認して投稿しました", f"report #{report_id} ・ <@{uid}>", OK),
        )
        await self._ack(
            interaction, embed(f"{cfg.E_OK} 承認しました", f"report #{report_id}", OK)
        )

        await dm(
            self.bot, uid,
            e=embed(
                f"{cfg.E_OK} 感想が承認されました",
                "実績チャンネルに投稿しました。次の注文ができるようになりました。"
                + (f"\n{cfg.E_CAMERA} 画像ボーナス **+{yen(bonus_paid)}**" if bonus_paid else ""),
                OK,
            ),
        )
        await asyncio.to_thread(
            self.bot.store.audit, "report.approved", interaction.user.id,
            {"report": report_id, "user": uid, "bonus": bonus_paid},
        )

        # --- 初回注文なら紹介報酬の判定へ ---
        referral = self.bot.get_cog("Referral")
        if referral is not None:
            try:
                await referral.maybe_payout(uid)
            except Exception:
                log.exception("紹介報酬の判定に失敗しました (user=%s)", uid)

    async def _resolve_channel(self, channel_id: int):
        """DM チャンネルはキャッシュに乗っていないことが多いので取り直す。"""
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def _recover_blobs(self, report_id: int, report) -> list[tuple[str, bytes]]:
        """投稿する画像を集める。

        まずメモリ上のものを使い、無ければ承認待ち投稿の添付から取り直す。
        BOT が再起動してもボーナス付きの実績が画像なしにならないようにする。
        """
        blobs = getattr(self.bot, "_report_blobs", {}).get(report_id)
        if blobs:
            return blobs

        channel = await self._resolve_channel(int(report["approval_channel_id"] or 0))
        if channel is None or not report["approval_message_id"]:
            return []
        try:
            message = await channel.fetch_message(int(report["approval_message_id"]))
        except Exception:
            log.warning("承認メッセージから画像を取り直せませんでした (report=%s)", report_id)
            return []

        recovered: list[tuple[str, bytes]] = []
        for attachment in message.attachments:
            if not (attachment.content_type or "").startswith("image/"):
                continue
            try:
                recovered.append((attachment.filename, await attachment.read()))
            except Exception:
                continue
        return recovered

    async def _publish(self, report_id: int, uid: int, report, order_id: int) -> Optional[int]:
        """実績チャンネルへ投稿する。原本の転送を試み、駄目なら再アップロードする。"""
        cfg = self.bot.cfg
        channel = await self._resolve_channel(cfg.ACHIEVEMENT_CHANNEL_ID)
        if channel is None:
            log.warning("実績チャンネルを取得できませんでした")
            return None

        order = await asyncio.to_thread(self.bot.store.get_order, order_id)
        pickup = (order["pickup"] if order else "") or "-"

        forwarded = False
        try:
            dm_channel = await self._resolve_channel(int(report["source_channel_id"] or 0))
            if dm_channel is not None:
                original = await dm_channel.fetch_message(int(report["source_message_id"]))
                await original.forward(channel)
                forwarded = True
        except Exception:
            log.debug("原本の転送に失敗しました。再アップロードに切り替えます", exc_info=True)
            forwarded = False

        if not forwarded:
            # 転送できないときは本文と画像を貼り直して同じ見た目にする
            blobs = await self._recover_blobs(report_id, report)
            files = [discord.File(io.BytesIO(data), filename=name) for name, data in blobs[:10]]
            quoted = "\n".join(f"> {line}" for line in str(report["content"]).splitlines())
            try:
                await channel.send(content=quoted[:1900] or None, files=files)
            except Exception:
                log.exception("実績の再アップロードに失敗しました")

        e = embed(
            f"{cfg.E_CAMERA} 商品レポート",
            f"<@{uid}> が注文した商品のレポートです！",
            REPORT_COLOR,
            footer=f"{cfg.BRAND_NAME} ・ 商品レポート",
        )
        e.add_field(name="受取方法", value=pickup, inline=False)
        e.add_field(
            name=f"{cfg.E_MAIL} 投稿内容",
            value=(
                "上のメッセージはユーザーから DM された原本を **転送** したものです。"
                if forwarded
                else "上のメッセージはユーザーから DM された内容を再掲したものです。"
            ),
            inline=False,
        )
        e.timestamp = now_jst()

        view = discord.ui.View(timeout=None)
        view.add_item(FlagButton(report_id))

        try:
            sent = await channel.send(embed=e, view=view)
        except Exception:
            log.exception("実績の投稿に失敗しました")
            return None
        finally:
            getattr(self.bot, "_report_blobs", {}).pop(report_id, None)

        return sent.id

    # ------------------------------------------------------------ 通報

    async def flag_report(self, interaction: discord.Interaction, report_id: int) -> None:
        cfg = self.bot.cfg
        report = await asyncio.to_thread(self.bot.store.get_report, report_id)
        if report is None:
            await interaction.response.send_message("該当の投稿が見つかりません。", ephemeral=True)
            return

        await asyncio.to_thread(
            self.bot.store.add_fraud_flag,
            int(report["user_id"]), "user_report", 30,
            f"<@{interaction.user.id}> が report #{report_id} を報告しました",
        )
        await post(
            self.bot,
            cfg.APPROVAL_CHANNEL_ID,
            embed=embed(
                f"{cfg.E_FLAG} 投稿が報告されました",
                f"report #{report_id} ・ 投稿者 <@{int(report['user_id'])}>\n"
                f"報告者 <@{interaction.user.id}>",
                WARN,
            ),
        )
        await interaction.response.send_message(
            "報告を受け付けました。オーナーが確認します。", ephemeral=True
        )

    # ------------------------------------------------------------ コマンド

    # --------------------------------------------------------- 代理での投稿

    @app_commands.command(
        name="proxyreport", description="利用者の感想を代理で実績に投稿します（オーナー限定）"
    )
    @app_commands.describe(
        user="感想を送ってきた利用者",
        content="感想の本文",
        order_id="対象の注文ID（省略すると未完了の注文に紐づけます）",
        image1="添付する画像",
        image2="添付する画像",
        image3="添付する画像",
    )
    async def proxy_report(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        content: str,
        order_id: Optional[int] = None,
        image1: Optional[discord.Attachment] = None,
        image2: Optional[discord.Attachment] = None,
        image3: Optional[discord.Attachment] = None,
    ) -> None:
        cfg = self.bot.cfg
        if not await self.bot.is_owner(interaction.user):
            await deny(interaction, "オーナー限定のコマンドです")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # --- 対象の注文を決める ---
        if order_id is not None:
            order = await asyncio.to_thread(self.bot.store.get_order, int(order_id))
            if order is None or int(order["user_id"]) != user.id:
                await reply(
                    interaction,
                    embed(
                        f"{cfg.E_NG} 注文が見つかりません",
                        f"注文 #{order_id} は {user.mention} のものではありません。",
                        BAD,
                    ),
                )
                return
        else:
            order = await asyncio.to_thread(self.bot.store.blocking_order, user.id)
            if order is None:
                await reply(
                    interaction,
                    embed(
                        f"{cfg.E_NG} 対象の注文がありません",
                        f"{user.mention} に感想待ちの注文はありません。\n"
                        "過去の注文に紐づける場合は order_id を指定してください。",
                        BAD,
                    ),
                )
                return

        if str(order["report_status"]) == "approved":
            await reply(
                interaction,
                embed(f"{cfg.E_NG} この注文は既に完了しています", f"注文 #{order['id']}", BAD),
            )
            return

        text = content.strip()
        if len(text) < 5:
            await reply(
                interaction, embed(f"{cfg.E_NG} 感想が短すぎます", "5 文字以上でお願いします。", BAD)
            )
            return

        # --- 画像を取り込む。使い回しは通常の投稿と同じ基準で弾く ---
        hashes: list[str] = []
        blobs: list[tuple[str, bytes]] = []
        for attachment in (image1, image2, image3):
            if attachment is None:
                continue
            if not (attachment.content_type or "").startswith("image/"):
                await reply(
                    interaction,
                    embed(f"{cfg.E_NG} 画像ではないファイルが含まれています", attachment.filename, BAD),
                )
                return
            if attachment.size > MAX_IMAGE_BYTES:
                await reply(
                    interaction,
                    embed(f"{cfg.E_NG} 画像が大きすぎます", attachment.filename, BAD),
                )
                return
            data = await attachment.read()
            try:
                phash = await asyncio.to_thread(dhash, data)
            except Exception:
                await reply(
                    interaction,
                    embed(f"{cfg.E_NG} 画像を読み取れませんでした", attachment.filename, BAD),
                )
                return
            verdict = await asyncio.to_thread(self.bot.fraud.check_image, user.id, phash)
            if verdict.blocked:
                await reply(
                    interaction,
                    embed(
                        f"{cfg.E_NG} 使い回しの画像が含まれています",
                        verdict.reasons(),
                        BAD,
                    ),
                )
                return
            hashes.append(phash)
            blobs.append((attachment.filename, data))

        # --- 通常の投稿と同じ経路で登録する ---
        report_id = await asyncio.to_thread(
            self.bot.store.create_report,
            int(order["id"]), user.id, text[:1800], len(blobs), hashes,
            interaction.channel_id or 0, 0,
        )
        self.bot._report_blobs = getattr(self.bot, "_report_blobs", {})
        self.bot._report_blobs[report_id] = blobs

        await asyncio.to_thread(
            self.bot.store.audit, "report.proxy_submitted", interaction.user.id,
            {
                "report": report_id, "order": int(order["id"]), "user": user.id,
                "images": len(blobs),
            },
        )
        # 代理であることは管理ログにだけ残す。実績チャンネルには出さない。
        await self.bot.send_log(
            cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{cfg.E_MEMO} 代理で感想を登録しました",
                f"対象: {user.mention} ・ 注文 #{order['id']} ・ report #{report_id}\n"
                f"実行: {interaction.user.mention}\n画像 {len(blobs)} 枚\n\n{text[:600]}",
                WARN,
            ),
        )

        await self.finalize_proxy(interaction, report_id, user.id, int(order["id"]))

    async def finalize_proxy(
        self, interaction: discord.Interaction, report_id: int, uid: int, order_id: int
    ) -> None:
        """代理投稿をそのまま承認済みとして実績チャンネルへ出す。

        見た目は通常の実績と同じ。代理かどうかは監査ログと管理ログにだけ残る。
        """
        cfg = self.bot.cfg
        await asyncio.to_thread(
            self.bot.store.decide_report, report_id, True, interaction.user.id, ""
        )
        report = await asyncio.to_thread(self.bot.store.get_report, report_id)

        message_id = await self._publish(report_id, uid, report, order_id)
        if message_id:
            await asyncio.to_thread(self.bot.store.set_report_message, report_id, message_id)

        import json

        for phash in json.loads(report["image_hashes"] or "[]"):
            await asyncio.to_thread(self.bot.store.remember_image_hash, phash, uid, report_id)

        bonus_paid = 0
        if int(report["image_count"]) > 0 and not int(report["photo_bonus_paid"]):
            balance = await asyncio.to_thread(
                self.bot.store.credit, uid, K_PHOTO, cfg.PHOTO_BONUS,
                f"report:{report_id}", "商品レポートへの画像添付",
            )
            await asyncio.to_thread(self.bot.store.mark_photo_bonus_paid, report_id)
            bonus_paid = cfg.PHOTO_BONUS
            await self.bot.send_log(
                cfg.LOG_MONEY_CHANNEL_ID,
                embed(
                    f"{cfg.E_CAMERA} 画像ボーナス",
                    f"<@{uid}> +{yen(cfg.PHOTO_BONUS)}（残高 {yen(balance)}）",
                    MONEY,
                ),
            )

        await self._ack(
            interaction,
            embed(
                f"{cfg.E_OK} 代理で投稿しました",
                f"<@{uid}> ・ 注文 #{order_id} ・ report #{report_id}\n"
                "実績チャンネルには通常の投稿と同じ形で出ています。\n"
                "代理であることは管理ログにのみ記録しました。"
                + (f"\n画像ボーナス +{yen(bonus_paid)} を付与" if bonus_paid else ""),
                OK,
            ),
        )
        await dm(
            self.bot, uid,
            e=embed(
                f"{cfg.E_OK} 感想が承認されました",
                "実績チャンネルに投稿しました。次の注文ができるようになりました。"
                + (f"\n{cfg.E_CAMERA} 画像ボーナス **+{yen(bonus_paid)}**" if bonus_paid else ""),
                OK,
            ),
        )

        referral = self.bot.get_cog("Referral")
        if referral is not None:
            try:
                await referral.maybe_payout(uid)
            except Exception:
                log.exception("紹介報酬の判定に失敗しました (user=%s)", uid)

    # --------------------------------------------------------- アーカイブ検索

    @app_commands.command(name="archive", description="過去の商品レポートを検索します")
    @app_commands.describe(
        user="投稿者で絞る", store_id="店舗IDで絞る", keyword="本文に含まれる語で絞る",
        count="表示件数",
    )
    async def archive(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User] = None,
        store_id: Optional[str] = None,
        keyword: Optional[str] = None,
        count: int = 10,
    ) -> None:
        cfg = self.bot.cfg
        if not await self.bot.is_owner(interaction.user):
            await deny(interaction, "オーナー限定のコマンドです")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(
            self.bot.store.search_reports,
            user.id if user else None,
            (store_id or "").strip(),
            (keyword or "").strip(),
            max(1, min(count, 25)),
        )
        if not rows:
            await reply(
                interaction,
                embed("該当する商品レポートはありません", "条件を変えてお試しください。", INFO),
            )
            return

        e = embed(
            f"{cfg.E_CAMERA} 商品レポート {len(rows)} 件",
            None,
            REPORT_COLOR,
            footer=cfg.BRAND_NAME,
        )
        conditions = []
        if user:
            conditions.append(f"投稿者 {user.mention}")
        if store_id:
            conditions.append(f"店舗 `{store_id}`")
        if keyword:
            conditions.append(f"語句「{keyword}」")
        if conditions:
            e.description = "条件: " + " / ".join(conditions)

        for row in rows[:10]:
            body = str(row["content"]).replace("\n", " ")
            e.add_field(
                name=(
                    f"#{row['id']} ・ {str(row['created_at'])[5:16]} ・ "
                    f"{row['o_store_name'] or row['o_store_id'] or '-'}"
                    + (f" ・ 画像{row['image_count']}枚" if row["image_count"] else "")
                )[:250],
                value=(f"<@{row['user_id']}>\n{body[:200]}" + ("…" if len(body) > 200 else ""))[:1020],
                inline=False,
            )
        if len(rows) > 10:
            e.set_footer(text=f"{cfg.BRAND_NAME} ・ 他 {len(rows) - 10} 件（count で増やせます）")
        await reply(interaction, e)

    @app_commands.command(name="reports", description="承認待ちの商品レポートを一覧表示します")
    async def reports_cmd(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        if not await self.bot.is_owner(interaction.user):
            await deny(interaction, "オーナー限定のコマンドです")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        pending = await asyncio.to_thread(self.bot.store.pending_reports)
        if not pending:
            await reply(interaction, embed(f"{cfg.E_OK} 承認待ちはありません", "", OK))
            return

        lines = [
            f"`#{r['id']}` <@{r['user_id']}> ・ 画像 {r['image_count']} 枚 ・ {r['created_at'][5:16]}"
            for r in pending[:25]
        ]
        await reply(
            interaction,
            embed(
                f"{cfg.E_MEMO} 承認待ちの商品レポート {len(pending)} 件",
                "\n".join(lines),
                WARN,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
