"""Kyash の送金リンクによる残高チャージ。

防御は3段。
1. 請求リンクを弾く（受け取りではなく支払いになってしまうため）
2. link_uuid を DB の UNIQUE 制約で先に押さえる（二重計上の防止）
3. 受け取り結果と申告額を突き合わせ、実際に入った額を採用する
"""
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from mcd.kyashclient import ClaimLinkRejected, NoKyashAccount
from mcd.logsetup import event, new_trace
from mcd.store import K_BONUS, DuplicateLink

from ._shared import BAD, MONEY, OK, WARN, deny, dm, embed, has_order_role, post, reply, yen

log = logging.getLogger("bot.charge")

LINK_RE = re.compile(r"(?:https?://)?kyash\.me/payments/([A-Za-z0-9\-_]+)")


def extract_link(raw: str) -> str:
    text = (raw or "").strip()
    match = LINK_RE.search(text)
    if match:
        return f"https://kyash.me/payments/{match.group(1)}"
    # URL を貼らずに ID だけ貼られた場合も拾う
    token = text.split()[-1] if text.split() else ""
    if token and re.fullmatch(r"[A-Za-z0-9\-_]{6,}", token):
        return f"https://kyash.me/payments/{token}"
    return ""


class ChargeModal(discord.ui.Modal, title="残高チャージ"):
    link = discord.ui.TextInput(
        label="Kyash の送金リンク",
        style=discord.TextStyle.short,
        placeholder="https://kyash.me/payments/xxxxxxxx",
        required=True,
        max_length=300,
    )

    def __init__(self, cog: "Charge"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.process_charge(interaction, str(self.link.value))


class Charge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------ 入口

    async def start_charge(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id

        if not has_order_role(interaction.user, cfg.ORDER_ROLE_ID):
            await deny(interaction, f"{cfg.E_LOCK} 権限がありません", "チャージにはロールが必要です。")
            return

        row = await asyncio.to_thread(self.bot.store.ensure_user, uid)
        if int(row["banned"]):
            await deny(interaction, f"{cfg.E_LOCK} 利用停止中です")
            return
        if row["terms_version"] != cfg.TERMS_VERSION:
            await deny(
                interaction,
                f"{cfg.E_MEMO} 先に規約への同意が必要です",
                "パネルの「注文する」を一度押して、規約に同意してください。",
            )
            return

        await interaction.response.send_modal(ChargeModal(self))

    # ------------------------------------------------------------ 本処理

    async def process_charge(self, interaction: discord.Interaction, raw: str) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        trace = new_trace("chg")

        url = extract_link(raw)
        if not url:
            await deny(
                interaction,
                f"{cfg.E_NG} リンクを認識できませんでした",
                "`https://kyash.me/payments/...` の形式で貼り付けてください。",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # --- 1) 受け取らずに中身だけ確認する ---
        try:
            info = await self.bot.kyash.inspect_link(url)
        except ClaimLinkRejected as exc:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} 請求リンクは使えません",
                    f"{exc}\n\nKyash アプリで **送金リンク** を作成して貼り付けてください。",
                    BAD,
                ),
            )
            return
        except NoKyashAccount as exc:
            await reply(interaction, embed(f"{cfg.E_NG} チャージを受け付けられません", str(exc), BAD))
            return
        except Exception as exc:
            log.warning("link_check 失敗: %s", exc)
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} リンクを確認できませんでした",
                    f"{str(exc)[:300]}\n\n既に受け取り済み、または期限切れの可能性があります。",
                    BAD,
                ),
            )
            return

        amount = int(info.amount)
        if amount < cfg.MIN_CHARGE or amount > cfg.MAX_CHARGE:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} チャージ額が範囲外です",
                    f"このリンクは {yen(amount)} です。\n"
                    f"1 回のチャージは {yen(cfg.MIN_CHARGE)} 〜 {yen(cfg.MAX_CHARGE)} です。",
                    BAD,
                ),
            )
            return

        # --- 2) link_uuid を先に押さえる（同じリンクの二重計上を防ぐ）---
        reserved = await asyncio.to_thread(self.bot.store.reserve_link, info.uuid)
        if not reserved:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} このリンクは既に使用されています",
                    "別のリンクを作成してください。",
                    BAD,
                ),
            )
            return

        # --- 3) 受け取る ---
        try:
            received = await self.bot.kyash.receive(url, trace=trace, info=info)
        except Exception as exc:
            await asyncio.to_thread(self.bot.store.drop_reserved_link, info.uuid)
            log.warning("link_recieve 失敗: %s", exc)
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} 受け取りに失敗しました",
                    f"{str(exc)[:300]}\n\n残高は変化していません。",
                    BAD,
                ),
            )
            return

        # --- 4) 台帳へ反映 ---
        try:
            charge_id, balance = await asyncio.to_thread(
                self.bot.store.fill_reserved_link,
                info.uuid, uid, received.account_id, received.amount,
                received.sender_name, received.sender_public_id,
            )
        except DuplicateLink:
            await reply(
                interaction,
                embed(f"{cfg.E_NG} このリンクは既に処理済みです", "", BAD),
            )
            return

        event(
            log, "charge.done", trace=trace, user=uid, account=received.account_id,
            amount=received.amount, charge=charge_id,
        )

        # --- チャージボーナス（まとめて入れてもらうと受け取り回数が減る）---
        bonus, percent = cfg.charge_bonus_for(received.amount)
        if bonus > 0:
            balance = await asyncio.to_thread(
                self.bot.store.credit, uid, K_BONUS, bonus,
                f"charge:{charge_id}", f"チャージボーナス {percent}%",
            )
            await self.bot.send_log(
                cfg.LOG_MONEY_CHANNEL_ID,
                embed(
                    f"{cfg.E_GIFT} チャージボーナス",
                    f"<@{uid}> +{yen(bonus)}（{percent}% ・ 残高 {yen(balance)}）",
                    MONEY,
                ),
            )

        # --- 5) 多重アカウント検知（機能17）---
        verdict = await asyncio.to_thread(
            self.bot.fraud.check_charge, uid, received.sender_public_id, received.sender_name
        )
        if verdict.findings:
            await asyncio.to_thread(self.bot.fraud.record, uid, verdict, "charge")
            if verdict.score >= 40:
                await post(
                    self.bot,
                    cfg.APPROVAL_CHANNEL_ID,
                    embed=embed(
                        f"{cfg.E_FLAG} 多重アカウントの疑い (スコア {verdict.score})",
                        f"<@{uid}> のチャージ\n\n{verdict.reasons()}",
                        WARN,
                    ),
                )

        # --- 6) 通知 ---
        e = embed(
            f"{cfg.E_MONEY} チャージが完了しました",
            None,
            OK,
            footer=f"{cfg.BRAND_NAME} ・ 返金はできません",
        )
        e.add_field(name="チャージ額", value=f"**{yen(received.amount)}**", inline=True)
        if bonus > 0:
            e.add_field(
                name=f"{cfg.E_GIFT} ボーナス", value=f"**+{yen(bonus)}**（{percent}%）", inline=True
            )
        e.add_field(name="残高", value=f"**{yen(balance)}**", inline=True)
        if received.sender_name:
            e.add_field(name="送金元", value=received.sender_name, inline=True)
        if received.amount != received.declared_amount:
            e.add_field(
                name=f"{cfg.E_WARN} 金額の差異",
                value=f"表示 {yen(received.declared_amount)} / 実際 {yen(received.amount)}",
                inline=False,
            )
        await reply(interaction, e)

        await self.bot.send_log(
            cfg.LOG_MONEY_CHANNEL_ID,
            embed(
                f"{cfg.E_MONEY} チャージ",
                f"<@{uid}> が {yen(received.amount)} をチャージ（残高 {yen(balance)}）\n"
                f"Kyash: {received.sender_name or '-'} / 受取口座: {received.account_label}",
                MONEY,
                footer=f"charge #{charge_id} ・ trace {trace}",
            ),
        )
        await asyncio.to_thread(
            self.bot.store.audit, "charge.done", uid,
            {
                "charge": charge_id, "amount": received.amount,
                "sender": received.sender_name, "account": received.account_label,
            },
            trace,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Charge(bot))
