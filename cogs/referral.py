"""紹介制度（機能3）。

自作自演で残高を刷られないよう、次の順で締めている。

* コードを使う時点で、自己紹介・循環・重複・紹介元の実績なし・上限超過を拒否
* Discord アカウントの作成日、サーバー参加からの経過時間、
  紹介元と同じ Kyash アカウントからのチャージを減点対象にする
* 報酬の付与は「紹介された人が初注文して、実績の承認まで終わった時点」まで遅らせる
* 付与の直前にもう一度判定し、スコアが高ければオーナー承認に回す
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mcd.store import K_REFERRAL

from ._shared import BAD, INFO, MONEY, OK, WARN, deny, dm, embed, post, reply, yen

log = logging.getLogger("bot.referral")

PENDING_KEY = "pending_referral"


class ReferralModal(discord.ui.Modal, title="紹介コードの入力"):
    code = discord.ui.TextInput(
        label="紹介コード",
        style=discord.TextStyle.short,
        placeholder="ABCD2345",
        required=True,
        max_length=16,
    )

    def __init__(self, cog: "Referral"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.claim_code(interaction, str(self.code.value))


class ReferralView(discord.ui.View):
    def __init__(self, cog: "Referral", user_id: int, can_claim: bool):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        if not can_claim:
            self.enter.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="紹介コードを入力", style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ReferralModal(self.cog))


class ReferralApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"refapv:(?P<uid>\d+):(?P<act>ok|ng)",
):
    def __init__(self, invitee_id: int, action: str):
        self.invitee_id = invitee_id
        self.action = action
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="紹介を承認" if approve else "却下",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"refapv:{invitee_id}:{action}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["uid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Optional[Referral] = interaction.client.get_cog("Referral")
        if cog is None:
            await interaction.response.send_message("読み込み中です。", ephemeral=True)
            return
        await cog.decide_pending(interaction, self.invitee_id, self.action == "ok")


class Referral(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReferralApproveButton)

    # ------------------------------------------------------------ 表示

    async def show_referral(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)

        row = await asyncio.to_thread(self.bot.store.ensure_user, uid)
        invited = await asyncio.to_thread(self.bot.store.count_referred, uid)
        rewarded = await asyncio.to_thread(self.bot.store.count_referred, uid, True)

        can_claim = row["referred_by"] is None and int(row["total_orders"]) == 0
        e = embed(f"{cfg.E_GIFT} 紹介制度", None, MONEY, footer=cfg.BRAND_NAME)
        e.add_field(name="あなたの紹介コード", value=f"```\n{row['referral_code']}\n```", inline=False)
        e.add_field(name="紹介した人数", value=f"{invited} 人（報酬確定 {rewarded} 人）", inline=True)
        e.add_field(
            name="報酬",
            value=f"紹介した人 {yen(cfg.REFERRAL_BONUS_INVITER)} / された人 {yen(cfg.REFERRAL_BONUS_INVITEE)}",
            inline=True,
        )
        e.add_field(
            name="報酬が入るタイミング",
            value=(
                "紹介された方が **初回の注文を済ませ、感想の承認まで完了した時点** で"
                "双方に付与されます。"
            ),
            inline=False,
        )
        if row["referred_by"]:
            e.add_field(name="あなたの紹介元", value=f"<@{int(row['referred_by'])}>", inline=False)
        elif not can_claim:
            e.add_field(
                name=f"{cfg.E_WARN} 紹介コードの入力について",
                value="初回注文より前にのみ入力できます。",
                inline=False,
            )

        await reply(interaction, e, view=ReferralView(self, uid, can_claim))

    # ------------------------------------------------------------ コード適用

    async def claim_code(self, interaction: discord.Interaction, raw: str) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        code = (raw or "").strip().upper()

        await interaction.response.defer(ephemeral=True, thinking=True)

        me = await asyncio.to_thread(self.bot.store.ensure_user, uid)
        if me["referred_by"] is not None:
            await reply(interaction, embed(f"{cfg.E_NG} 既に紹介元が登録されています", "", BAD))
            return
        if int(me["total_orders"]) > 0:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 初回注文より前にのみ入力できます", "", BAD),
            )
            return

        inviter = await asyncio.to_thread(self.bot.store.user_by_referral_code, code)
        if inviter is None:
            await reply(interaction, embed(f"{cfg.E_NG} 紹介コードが見つかりません", "", BAD))
            return

        inviter_id = int(inviter["user_id"])
        member = interaction.user
        verdict = await asyncio.to_thread(
            self.bot.fraud.check_referral_claim,
            inviter_id,
            uid,
            getattr(member, "created_at", None),
            getattr(member, "joined_at", None),
        )

        if verdict.blocked:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} この紹介コードは使用できません", verdict.reasons(), BAD),
            )
            await asyncio.to_thread(self.bot.fraud.record, uid, verdict, "referral.claim")
            return

        if verdict.findings:
            await asyncio.to_thread(self.bot.fraud.record, uid, verdict, "referral.claim")

        # スコアが高ければオーナー承認へ回す
        if verdict.score >= cfg.FRAUD_REVIEW_SCORE:
            await asyncio.to_thread(
                self.bot.store.set_kv, f"{PENDING_KEY}:{uid}", inviter_id
            )
            view = discord.ui.View(timeout=None)
            view.add_item(ReferralApproveButton(uid, "ok"))
            view.add_item(ReferralApproveButton(uid, "ng"))
            await post(
                self.bot,
                cfg.APPROVAL_CHANNEL_ID,
                embed=embed(
                    f"{cfg.E_FLAG} 紹介の確認が必要です (スコア {verdict.score})",
                    f"紹介元 <@{inviter_id}> → 紹介された人 <@{uid}>\n\n{verdict.reasons()}",
                    WARN,
                ),
                view=view,
            )
            await reply(
                interaction,
                embed(
                    f"{cfg.E_CLOCK} 確認中です",
                    "紹介の登録にはオーナーの確認が必要です。結果は DM でお知らせします。",
                    WARN,
                ),
            )
            return

        await self._link(uid, inviter_id)
        await reply(
            interaction,
            embed(
                f"{cfg.E_OK} 紹介コードを登録しました",
                f"紹介元: <@{inviter_id}>\n"
                f"初回の注文と感想の承認が完了すると、双方に報酬が入ります。",
                OK,
            ),
        )

    async def _link(self, invitee_id: int, inviter_id: int) -> None:
        await asyncio.to_thread(self.bot.store.set_referrer, invitee_id, inviter_id)
        await asyncio.to_thread(
            self.bot.store.audit, "referral.linked", invitee_id, {"inviter": inviter_id}
        )
        await self.bot.send_log(
            self.bot.cfg.LOG_ADMIN_CHANNEL_ID,
            embed(
                f"{self.bot.cfg.E_GIFT} 紹介の登録",
                f"<@{inviter_id}> → <@{invitee_id}>",
                INFO,
            ),
        )

    async def decide_pending(
        self, interaction: discord.Interaction, invitee_id: int, approve: bool
    ) -> None:
        cfg = self.bot.cfg
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("オーナー限定の操作です。", ephemeral=True)
            return

        inviter_id = await asyncio.to_thread(
            self.bot.store.get_kv, f"{PENDING_KEY}:{invitee_id}", None
        )
        if inviter_id is None:
            await interaction.response.send_message("この申請は処理済みです。", ephemeral=True)
            return

        await asyncio.to_thread(self.bot.store.set_kv, f"{PENDING_KEY}:{invitee_id}", None)

        if approve:
            await self._link(invitee_id, int(inviter_id))
            await interaction.response.edit_message(
                embed=embed(f"{cfg.E_OK} 紹介を承認しました", f"<@{inviter_id}> → <@{invitee_id}>", OK),
                view=None,
            )
            await dm(
                self.bot, invitee_id,
                e=embed(f"{cfg.E_OK} 紹介コードが登録されました", f"紹介元: <@{inviter_id}>", OK),
            )
        else:
            await interaction.response.edit_message(
                embed=embed(f"{cfg.E_NG} 紹介を却下しました", f"<@{invitee_id}>", BAD), view=None
            )
            await dm(
                self.bot, invitee_id,
                e=embed(f"{cfg.E_NG} 紹介コードは登録されませんでした", "", BAD),
            )

    # ------------------------------------------------------------ 報酬付与

    async def maybe_payout(self, invitee_id: int) -> None:
        """実績の承認が済んだタイミングで呼ぶ。条件を満たせば双方に付与する。"""
        cfg = self.bot.cfg
        row = await asyncio.to_thread(self.bot.store.get_user, invitee_id)
        if row is None or row["referred_by"] is None or int(row["referral_reward_paid"]):
            return

        inviter_id = int(row["referred_by"])
        verdict = await asyncio.to_thread(
            self.bot.fraud.check_referral_payout, inviter_id, invitee_id
        )

        if verdict.blocked:
            log.info("紹介報酬を見送りました (invitee=%s): %s", invitee_id, verdict.summary)
            return

        if verdict.score >= cfg.FRAUD_REVIEW_SCORE:
            await asyncio.to_thread(self.bot.fraud.record, invitee_id, verdict, "referral.payout")
            await post(
                self.bot,
                cfg.APPROVAL_CHANNEL_ID,
                embed=embed(
                    f"{cfg.E_FLAG} 紹介報酬を保留しました (スコア {verdict.score})",
                    f"<@{inviter_id}> → <@{invitee_id}>\n\n{verdict.reasons()}\n\n"
                    f"問題なければ `/mcd adjust` で手動付与してください。",
                    WARN,
                ),
            )
            return

        await asyncio.to_thread(
            self.bot.store.credit, inviter_id, K_REFERRAL, cfg.REFERRAL_BONUS_INVITER,
            f"referral:{invitee_id}", "紹介報酬（紹介した側）",
        )
        await asyncio.to_thread(
            self.bot.store.credit, invitee_id, K_REFERRAL, cfg.REFERRAL_BONUS_INVITEE,
            f"referral:{inviter_id}", "紹介報酬（紹介された側）",
        )
        await asyncio.to_thread(self.bot.store.mark_referral_paid, invitee_id)

        await asyncio.to_thread(
            self.bot.store.audit, "referral.paid", invitee_id,
            {"inviter": inviter_id, "amount": cfg.REFERRAL_BONUS_INVITER},
        )
        await dm(
            self.bot, inviter_id,
            e=embed(
                f"{cfg.E_GIFT} 紹介報酬が入りました",
                f"<@{invitee_id}> さんの初回注文が完了しました。\n"
                f"残高に {yen(cfg.REFERRAL_BONUS_INVITER)} を追加しました。",
                MONEY,
            ),
        )
        await dm(
            self.bot, invitee_id,
            e=embed(
                f"{cfg.E_GIFT} 紹介報酬が入りました",
                f"残高に {yen(cfg.REFERRAL_BONUS_INVITEE)} を追加しました。",
                MONEY,
            ),
        )
        await self.bot.send_log(
            cfg.LOG_MONEY_CHANNEL_ID,
            embed(
                f"{cfg.E_GIFT} 紹介報酬",
                f"<@{inviter_id}> +{yen(cfg.REFERRAL_BONUS_INVITER)} / "
                f"<@{invitee_id}> +{yen(cfg.REFERRAL_BONUS_INVITEE)}",
                MONEY,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Referral(bot))
