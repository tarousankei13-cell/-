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

    def __init__(self, cog: "Reports", report_id: int):
        super().__init__()
        self.cog = cog
        self.report_id = report_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.finalize(interaction, self.report_id, False, str(self.reason.value))


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
            await interaction.response.send_modal(RejectReasonModal(cog, self.report_id))
        else:
            await cog.finalize(interaction, self.report_id, True, "")


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
        if order is None or order["report_status"] not in ("awaiting", "rejected"):
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

    async def finalize(
        self, interaction: discord.Interaction, report_id: int, approve: bool, reason: str
    ) -> None:
        cfg = self.bot.cfg
        report = await asyncio.to_thread(self.bot.store.get_report, report_id)
        if report is None or report["status"] != "pending":
            await interaction.response.send_message("この申請は処理済みです。", ephemeral=True)
            return

        uid = int(report["user_id"])
        order_id = int(report["order_id"])

        if not interaction.response.is_done():
            await interaction.response.defer()

        await asyncio.to_thread(
            self.bot.store.decide_report, report_id, approve, interaction.user.id, reason
        )

        if not approve:
            try:
                await interaction.edit_original_response(
                    embed=embed(
                        f"{cfg.E_NG} 却下しました",
                        f"report #{report_id} ・ <@{uid}>\n理由: {reason}",
                        BAD,
                    ),
                    view=None,
                )
            except Exception:
                pass
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

        try:
            await interaction.edit_original_response(
                embed=embed(
                    f"{cfg.E_OK} 承認して投稿しました",
                    f"report #{report_id} ・ <@{uid}>",
                    OK,
                ),
                view=None,
            )
        except Exception:
            pass

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

    async def _publish(self, report_id: int, uid: int, report, order_id: int) -> Optional[int]:
        """実績チャンネルへ投稿する。原本の転送を試み、駄目なら再アップロードする。"""
        cfg = self.bot.cfg
        channel = self.bot.get_channel(cfg.ACHIEVEMENT_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(cfg.ACHIEVEMENT_CHANNEL_ID)
            except Exception:
                log.warning("実績チャンネルを取得できませんでした")
                return None

        order = await asyncio.to_thread(self.bot.store.get_order, order_id)
        pickup = (order["pickup"] if order else "") or "-"

        forwarded = False
        try:
            source = await self.bot.get_channel(int(report["source_channel_id"])).fetch_message(
                int(report["source_message_id"])
            ) if self.bot.get_channel(int(report["source_channel_id"])) else None
            if source is not None:
                await source.forward(channel)
                forwarded = True
        except Exception:
            forwarded = False

        if not forwarded:
            # 転送できないときは本文と画像を貼り直して同じ見た目にする
            blobs = getattr(self.bot, "_report_blobs", {}).get(report_id, [])
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
