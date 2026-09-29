"""注文パネルと注文フロー。

利用者はパネルのボタンだけで完結する。管理はスラッシュコマンド側。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from mcd.cards import render_pickup_card
from mcd.logsetup import event, new_trace
from mcd.mcdclient import NoAccountAvailable, PaymentUncertain, hex_digest
from mcd.rates import resolve_rate, user_pays
from mcd.store import (
    DuplicateHex,
    InsufficientBalance,
    K_ORDER,
    K_REFUND,
    now_jst,
)

from ._shared import (
    BAD,
    INFO,
    MONEY,
    OK,
    WARN,
    deny,
    dm,
    embed,
    has_order_role,
    post,
    queue_line,
    reply,
    role_ids,
    yen,
)

log = logging.getLogger("bot.panel")

HEX_RE = re.compile(r"^[0-9a-fA-F\s]+$")


def clean_hex(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").strip()


# ====================================================================== 規約


class TermsView(discord.ui.View):
    """機能15: 規約同意。同意日時とバージョンを台帳に残す。"""

    def __init__(self, cog: "Panel", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="同意して始める", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.cog.bot.cfg
        await asyncio.to_thread(
            self.cog.bot.store.agree_terms, self.user_id, cfg.TERMS_VERSION
        )
        await asyncio.to_thread(
            self.cog.bot.store.audit,
            "terms.agreed",
            self.user_id,
            {"version": cfg.TERMS_VERSION},
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=embed(
                f"{cfg.E_OK} 規約に同意しました",
                "もう一度「注文する」を押してください。",
                OK,
            ),
            view=self,
        )


# ==================================================================== パネル


class PanelView(discord.ui.View):
    """常設パネル。再起動後も生き続けるよう timeout=None + 固定 custom_id。"""

    def __init__(self, cog: "Panel"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="注文する", style=discord.ButtonStyle.success, custom_id="panel:order", row=0
    )
    async def order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.start_order(interaction)

    @discord.ui.button(
        label="チャージ", style=discord.ButtonStyle.primary, custom_id="panel:charge", row=0
    )
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        charge_cog = self.cog.bot.get_cog("Charge")
        if charge_cog is None:
            await deny(interaction, "チャージ機能が読み込まれていません")
            return
        await charge_cog.start_charge(interaction)

    @discord.ui.button(
        label="残高・履歴", style=discord.ButtonStyle.secondary, custom_id="panel:balance", row=1
    )
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_balance(interaction)

    @discord.ui.button(
        label="紹介コード", style=discord.ButtonStyle.secondary, custom_id="panel:referral", row=1
    )
    async def referral(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        referral_cog = self.cog.bot.get_cog("Referral")
        if referral_cog is None:
            await deny(interaction, "紹介機能が読み込まれていません")
            return
        await referral_cog.show_referral(interaction)


# ============================================================== hex 入力モーダル


class HexModal(discord.ui.Modal, title="注文コードの入力"):
    code = discord.ui.TextInput(
        label="注文コード (hex)",
        style=discord.TextStyle.paragraph,
        placeholder="0a0531303532381a...",
        required=True,
        max_length=4000,
    )

    def __init__(self, cog: "Panel"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.preview_order(interaction, str(self.code.value))


# ============================================================ 決済の確認ビュー


class ConfirmOrderView(discord.ui.View):
    def __init__(self, cog: "Panel", user_id: int, payload: dict, timeout: float):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id
        self.payload = payload

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("本人のみ操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="決済する", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        cfg = self.cog.bot.cfg
        await interaction.response.edit_message(
            embed=embed(
                f"{cfg.E_CLOCK} 決済しています",
                f"完了までお待ちください。\n{queue_line(self.cog.bot.mcd)}",
                INFO,
            ),
            view=self,
        )
        await self.cog.execute_order(interaction, self.payload)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            embed=embed("キャンセルしました", "決済は行われていません。", INFO), view=self
        )


# ========================================================== 上限超過の承認ボタン


class OrderApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ordapv:(?P<oid>\d+):(?P<act>ok|ng)",
):
    """再起動をまたいでも押せる承認ボタン。"""

    def __init__(self, order_id: int, action: str):
        self.order_id = order_id
        self.action = action
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="承認して決済" if approve else "却下",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"ordapv:{order_id}:{action}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["oid"]), match["act"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        cog: Optional[Panel] = bot.get_cog("Panel")
        if cog is None:
            await interaction.response.send_message("読み込み中です。", ephemeral=True)
            return
        await cog.handle_order_approval(interaction, self.order_id, self.action == "ok")


# ====================================================================== cog


class Panel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(PanelView(self))
        self.bot.add_dynamic_items(OrderApproveButton)

    # ------------------------------------------------------------ パネル設置

    def panel_embed(self) -> discord.Embed:
        cfg = self.bot.cfg
        e = embed(
            f"{cfg.E_FOOD} モバイルオーダー",
            "注文コードを貼り付けて注文できます。\n"
            "残高は Kyash の送金リンクでチャージしてください。",
            OK,
            footer=cfg.BRAND_NAME,
        )
        e.add_field(
            name="ご利用の流れ",
            value=(
                "1. 「チャージ」で残高を入れる\n"
                "2. 「注文する」で注文コードを貼る\n"
                "3. 内容を確認して決済\n"
                f"4. 注文番号が DM に届く\n"
                f"5. {cfg.E_MEMO} 感想を DM で送る（次回注文の条件）"
            ),
            inline=False,
        )
        e.add_field(name="標準の負担率", value=f"定価の {cfg.DEFAULT_USER_RATE}%", inline=True)
        e.add_field(name="混雑状況", value=queue_line(self.bot.mcd), inline=True)
        e.add_field(
            name=f"{cfg.E_WARN} 注意",
            value="チャージした残高は返金できません。",
            inline=False,
        )
        return e

    @app_commands.command(name="panel", description="注文パネルを設置します（オーナー限定）")
    async def panel_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await deny(interaction, "このコマンドはオーナー限定です")
            return
        await interaction.response.send_message(
            embed=self.panel_embed(), view=PanelView(self)
        )
        await asyncio.to_thread(
            self.bot.store.audit, "panel.posted", interaction.user.id,
            {"channel": interaction.channel_id},
        )

    # ------------------------------------------------------------ 事前チェック

    async def _gate(self, interaction: discord.Interaction) -> bool:
        """注文に進んでよいかの入口チェック。通らなければ False。"""
        cfg = self.bot.cfg
        uid = interaction.user.id

        if not has_order_role(interaction.user, cfg.ORDER_ROLE_ID):
            await deny(
                interaction,
                f"{cfg.E_LOCK} 権限がありません",
                "注文にはロールが必要です。サーバー管理者にお問い合わせください。",
            )
            return False

        row = await asyncio.to_thread(self.bot.store.ensure_user, uid)

        if int(row["banned"]):
            await deny(interaction, f"{cfg.E_LOCK} 利用停止中です", "オーナーにお問い合わせください。")
            return False

        if row["terms_version"] != cfg.TERMS_VERSION:
            e = embed(f"{cfg.E_MEMO} ご利用規約", cfg.TERMS_TEXT, WARN, footer=f"版 {cfg.TERMS_VERSION}")
            await reply(interaction, e, view=TermsView(self, uid))
            return False

        blocking = await asyncio.to_thread(self.bot.store.blocking_order, uid)
        if blocking is not None:
            status = blocking["report_status"]
            hint = {
                "awaiting": "BOT へ DM で感想を送ってください。",
                "submitted": "感想は受付済みです。オーナーの承認をお待ちください。",
                "rejected": "感想が却下されました。DM で送り直してください。",
            }.get(status, "")
            await deny(
                interaction,
                f"{cfg.E_LOCK} 前回の感想が未完了です",
                f"注文 #{blocking['id']}（注文番号 {blocking['receipt_number'] or '-'}）\n{hint}",
            )
            return False

        return True

    async def start_order(self, interaction: discord.Interaction) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.send_modal(HexModal(self))

    # ------------------------------------------------------------ プレビュー

    async def preview_order(self, interaction: discord.Interaction, raw: str) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        code = clean_hex(raw)

        if not code or not HEX_RE.match(code) or len(code) % 2:
            await deny(
                interaction,
                f"{cfg.E_NG} 注文コードの形式が正しくありません",
                "16進数の文字列をそのまま貼り付けてください。",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            decoded = self.bot.mcd.decode(code)
        except Exception as exc:
            log.warning("decode 失敗: %s", exc)
            await reply(
                interaction,
                embed(f"{cfg.E_NG} 注文コードを解析できませんでした", str(exc)[:300], BAD),
            )
            return

        if not decoded.store_id or not decoded.amount_cents:
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} 注文コードから内容を読み取れませんでした",
                    "店舗または金額が取得できません。コードを確認してください。",
                    BAD,
                ),
            )
            return

        decision = resolve_rate(
            cfg.DEFAULT_USER_RATE, cfg.ROLE_RATES, cfg.USER_RATES, cfg.CAMPAIGNS,
            uid, role_ids(interaction.user),
        )
        face = int(decoded.amount_cents)
        pay = user_pays(face, decision.rate)
        store_name = await self.bot.mcd.store_name(decoded.store_id)
        balance = await asyncio.to_thread(self.bot.store.balance, uid)

        payload = {
            "hex": code,
            "decoded": decoded,
            "face": face,
            "rate": decision.rate,
            "pay": pay,
            "store_name": store_name,
            "source": decision.source,
        }

        e = embed(
            f"{cfg.E_FOOD} この内容で決済しますか？",
            None,
            WARN,
            footer=f"{cfg.BRAND_NAME} ・ 料率: {decision.source}",
        )
        e.add_field(
            name="店舗",
            value=f"{store_name or '-'}\n`{decoded.store_id}`",
            inline=True,
        )
        e.add_field(name="受取方法", value=decoded.pickup_method or "-", inline=True)
        e.add_field(name="商品", value=f"{len(decoded.products)} 点", inline=True)
        e.add_field(
            name="お支払い額",
            value=f"**{yen(pay)}**（定価 {yen(face)} の {decision.rate}%）",
            inline=False,
        )
        e.add_field(
            name="残高",
            value=f"{yen(balance)} → {yen(balance - pay)}",
            inline=True,
        )
        e.add_field(name="混雑状況", value=queue_line(self.bot.mcd), inline=True)

        # --- 残高不足（機能1: 不足分の請求リンクを出す）---
        if balance < pay:
            await self._offer_shortfall(interaction, uid, pay - balance, e)
            return

        # --- 上限超過はオーナー承認へ ---
        if face > cfg.ORDER_FACE_LIMIT:
            await self._request_approval(interaction, payload, e)
            return

        await reply(
            interaction, e, view=ConfirmOrderView(self, uid, payload, cfg.ORDER_TIMEOUT_SECONDS)
        )

    # -------------------------------------------- 機能1: 不足分の請求リンク

    async def _offer_shortfall(
        self, interaction: discord.Interaction, uid: int, shortfall: int, base: discord.Embed
    ) -> None:
        cfg = self.bot.cfg
        base.color = BAD
        base.title = f"{cfg.E_MONEY} 残高が足りません"

        link = ""
        error = ""
        if shortfall >= 1:
            try:
                link = await self.bot.kyash.create_claim_link(
                    shortfall, f"{cfg.BRAND_NAME} 残高チャージ（不足分）"
                )
            except Exception as exc:
                error = str(exc)[:200]
                log.warning("請求リンクの作成に失敗: %s", exc)

        if link:
            base.add_field(
                name=f"{cfg.E_MAIL} 不足分の請求リンク",
                value=(
                    f"不足 **{yen(shortfall)}** の請求リンクを作成しました。\n"
                    f"Kyash アプリで開いて支払うと、自動ではなく **「チャージ」から"
                    f"送金リンクを貼る** 形で残高に反映されます。\n{link}"
                ),
                inline=False,
            )
        else:
            base.add_field(
                name=f"{cfg.E_MAIL} チャージのお願い",
                value=(
                    f"不足 **{yen(shortfall)}** です。パネルの「チャージ」から"
                    f"Kyash の送金リンクを貼ってください。"
                    + (f"\n（請求リンクの自動作成に失敗しました: {error}）" if error else "")
                ),
                inline=False,
            )
        await reply(interaction, base)

    # ------------------------------------------------ 上限超過のオーナー承認

    async def _request_approval(
        self, interaction: discord.Interaction, payload: dict, base: discord.Embed
    ) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        decoded = payload["decoded"]
        trace = new_trace("ord")

        try:
            order_id = await asyncio.to_thread(
                self.bot.store.reserve_order,
                trace, uid, hex_digest(payload["hex"]), decoded.store_id,
                payload["store_name"], decoded.pickup_method, payload["face"],
                payload["rate"], payload["pay"], len(decoded.products), payload["hex"],
            )
        except DuplicateHex as exc:
            await reply(interaction, embed(f"{cfg.E_NG} 使用済みの注文コードです", str(exc), BAD))
            return

        view = discord.ui.View(timeout=None)
        view.add_item(OrderApproveButton(order_id, "ok"))
        view.add_item(OrderApproveButton(order_id, "ng"))

        approval = embed(
            f"{cfg.E_WARN} 上限超過の注文が申請されました",
            f"定価が上限 {yen(cfg.ORDER_FACE_LIMIT)} を超えています。",
            WARN,
            footer=f"注文 #{order_id} ・ 有効期限 {cfg.APPROVAL_TIMEOUT_MINUTES} 分",
        )
        approval.add_field(name="申請者", value=f"<@{uid}>", inline=True)
        approval.add_field(name="定価", value=yen(payload["face"]), inline=True)
        approval.add_field(name="利用者負担", value=yen(payload["pay"]), inline=True)
        approval.add_field(
            name="店舗", value=f"{payload['store_name'] or '-'} (`{decoded.store_id}`)", inline=True
        )
        approval.add_field(name="受取方法", value=decoded.pickup_method or "-", inline=True)
        approval.add_field(
            name="オーナー負担", value=yen(payload["face"] - payload["pay"]), inline=True
        )

        posted = await post(self.bot, cfg.APPROVAL_CHANNEL_ID, embed=approval, view=view)
        if posted is None:
            await asyncio.to_thread(self.bot.store.release_hex, order_id)
            await reply(
                interaction,
                embed(
                    f"{cfg.E_NG} 承認チャンネルが設定されていません",
                    "main.py の APPROVAL_CHANNEL_ID を設定してください。",
                    BAD,
                ),
            )
            return

        base.color = WARN
        base.title = f"{cfg.E_CLOCK} オーナーの承認待ちです"
        base.add_field(
            name="ご案内",
            value=(
                f"定価が上限 {yen(cfg.ORDER_FACE_LIMIT)} を超えているため、"
                f"オーナーの承認後に決済されます。結果は DM でお知らせします。"
            ),
            inline=False,
        )
        await reply(interaction, base)

    async def handle_order_approval(
        self, interaction: discord.Interaction, order_id: int, approve: bool
    ) -> None:
        cfg = self.bot.cfg
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("オーナー限定の操作です。", ephemeral=True)
            return

        order = await asyncio.to_thread(self.bot.store.get_order, order_id)
        if order is None or order["status"] != "pending":
            await interaction.response.send_message(
                "この申請は既に処理済み、または期限切れです。", ephemeral=True
            )
            return

        uid = int(order["user_id"])

        if not approve:
            await asyncio.to_thread(self.bot.store.release_hex, order_id)
            await asyncio.to_thread(
                self.bot.store.audit, "order.approval.rejected", interaction.user.id,
                {"order": order_id, "user": uid},
            )
            await interaction.response.edit_message(
                embed=embed(f"{cfg.E_NG} 却下しました", f"注文 #{order_id}", BAD), view=None
            )
            await dm(
                self.bot, uid,
                e=embed(
                    f"{cfg.E_NG} 注文が却下されました",
                    "上限超過の申請がオーナーにより却下されました。",
                    BAD,
                ),
            )
            return

        await interaction.response.edit_message(
            embed=embed(f"{cfg.E_CLOCK} 承認しました。決済しています…", f"注文 #{order_id}", INFO),
            view=None,
        )

        try:
            decoded = self.bot.mcd.decode(order["raw_hex"] or "")
        except Exception as exc:
            await asyncio.to_thread(
                self.bot.store.finish_order_failed, order_id, f"decode: {exc}"
            )
            await interaction.edit_original_response(
                embed=embed(f"{cfg.E_NG} 解析に失敗しました", str(exc)[:300], BAD), view=None
            )
            return

        payload = {
            "hex": order["raw_hex"],
            "decoded": decoded,
            "face": int(order["face_amount"]),
            "rate": int(order["rate"]),
            "pay": int(order["paid_amount"]),
            "store_name": order["store_name"] or "",
            "source": "承認済み",
        }
        result = await self._pay_and_notify(uid, payload, order_id=order_id)
        await interaction.edit_original_response(
            embed=embed(
                f"{cfg.E_OK} 決済しました" if result else f"{cfg.E_NG} 決済に失敗しました",
                f"注文 #{order_id} ・ <@{uid}>",
                OK if result else BAD,
            ),
            view=None,
        )

    # ------------------------------------------------------------ 決済の実行

    async def execute_order(self, interaction: discord.Interaction, payload: dict) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        ok = await self._pay_and_notify(uid, payload)
        try:
            await interaction.edit_original_response(
                embed=embed(
                    f"{cfg.E_OK} 決済が完了しました" if ok else f"{cfg.E_NG} 決済できませんでした",
                    "詳細は DM をご確認ください。" if ok else "詳細は DM をご確認ください。",
                    OK if ok else BAD,
                ),
                view=None,
            )
        except Exception:
            pass

    async def _pay_and_notify(
        self, uid: int, payload: dict, order_id: Optional[int] = None
    ) -> bool:
        """残高を引いて決済し、結果を通知する。戻り値は成功したかどうか。"""
        cfg = self.bot.cfg
        store = self.bot.store
        decoded = payload["decoded"]
        face, rate, pay = payload["face"], payload["rate"], payload["pay"]
        trace = new_trace("ord")

        # 1) 枠の確保（同じ hex の二重決済をここで止める）
        if order_id is None:
            try:
                order_id = await asyncio.to_thread(
                    store.reserve_order, trace, uid, hex_digest(payload["hex"]),
                    decoded.store_id, payload["store_name"], decoded.pickup_method,
                    face, rate, pay, len(decoded.products), payload["hex"],
                )
            except DuplicateHex as exc:
                await dm(self.bot, uid, e=embed(f"{cfg.E_NG} 使用済みの注文コードです", str(exc), BAD))
                return False

        # 2) 不正検知（注文の頻度・金額）
        verdict = await asyncio.to_thread(self.bot.fraud.check_order, uid, face)
        if verdict.findings:
            await asyncio.to_thread(self.bot.fraud.record, uid, verdict, "order")
            await self._alert_fraud(uid, verdict, f"注文 #{order_id}")

        # 3) 残高を引く（決済の前に引き、失敗したら戻す）
        try:
            balance_after = await asyncio.to_thread(
                store.debit, uid, K_ORDER, pay, f"order:{order_id}",
                f"{payload['store_name']} {face}円の{rate}%",
            )
        except InsufficientBalance as exc:
            await asyncio.to_thread(store.release_hex, order_id)
            await dm(
                self.bot, uid,
                e=embed(
                    f"{cfg.E_MONEY} 残高が足りません",
                    f"不足 {yen(exc.shortfall)} です。チャージしてからお試しください。",
                    BAD,
                ),
            )
            return False

        # 4) 決済
        try:
            paid = await self.bot.mcd.pay(payload["hex"], decoded, trace)
        except PaymentUncertain as exc:
            await asyncio.to_thread(
                store.finish_order_failed, order_id, str(exc), unknown=True
            )
            await self._alert_uncertain(uid, order_id, payload, str(exc))
            await dm(
                self.bot, uid,
                e=embed(
                    f"{cfg.E_WARN} 決済の結果を確認しています",
                    "通信が途中で途切れたため、決済が成立したか確認中です。\n"
                    "残高は一旦引き落としたままにしています。"
                    "結果が分かり次第オーナーからご連絡します。",
                    WARN,
                ),
            )
            return False
        except Exception as exc:
            # 課金前に落ちたと分かっている場合だけ、hex を解放して再試行できるようにする
            safe = bool(getattr(exc, "safe_to_retry", False))
            await asyncio.to_thread(
                store.credit, uid, K_REFUND, pay, f"order:{order_id}", "決済失敗による返金"
            )
            await asyncio.to_thread(store.finish_order_failed, order_id, str(exc))
            if safe:
                await asyncio.to_thread(store.release_hex, order_id)
            event(
                log, "order.failed", level=logging.WARNING,
                trace=trace, user=uid, order=order_id, error=str(exc)[:300], safe_retry=safe,
            )
            await dm(
                self.bot, uid,
                e=embed(
                    f"{cfg.E_NG} 決済できませんでした",
                    f"{str(exc)[:400]}\n\n残高 {yen(pay)} は返金しました。"
                    + ("\n同じコードでもう一度お試しいただけます。" if safe else
                       "\nこのコードは再利用できません。オーナーにお問い合わせください。"),
                    BAD,
                ),
            )
            await self.bot.send_log(
                cfg.LOG_ERRORS_CHANNEL_ID,
                embed(
                    f"{cfg.E_NG} 決済失敗",
                    f"<@{uid}> ・ 注文 #{order_id}\n```{str(exc)[:900]}```",
                    BAD,
                ),
            )
            return False

        # 5) 成功
        await asyncio.to_thread(
            store.finish_order_ok, order_id, paid.account_id, paid.receipt_number,
            paid.short_code, paid.order_token, paid.group,
        )
        user_row = await asyncio.to_thread(store.get_user, uid)
        total_orders = int(user_row["total_orders"]) if user_row else 1

        await self._send_receipt(
            uid, order_id, paid, payload, balance_after, total_orders
        )
        await self._post_stage1(uid, decoded.pickup_method)
        await self.bot.send_log(
            cfg.LOG_ORDERS_CHANNEL_ID,
            self._order_log_embed(uid, order_id, paid, payload, trace),
        )
        await asyncio.to_thread(
            store.audit, "order.paid", uid,
            {
                "order": order_id, "receipt": paid.receipt_number, "face": face,
                "paid": pay, "rate": rate, "account": paid.account_label,
            },
            trace,
        )

        buzzer_cog = self.bot.get_cog("Tasks")
        if buzzer_cog is not None:
            buzzer_cog.watch_buzzer(uid, order_id, paid)

        return True

    # ------------------------------------------------------------ 通知まわり

    async def _send_receipt(
        self, uid: int, order_id: int, paid, payload: dict, balance_after: int, total_orders: int
    ) -> None:
        cfg = self.bot.cfg
        decoded = payload["decoded"]
        when = now_jst().strftime("%Y-%m-%d %H:%M")

        png = await asyncio.to_thread(
            render_pickup_card,
            paid.receipt_number or "----",
            paid.store_name or payload["store_name"] or "",
            decoded.store_id,
            decoded.pickup_method or "",
            when,
            paid.short_code,
            cfg.BRAND_NAME,
        )
        file = discord.File(__import__("io").BytesIO(png), filename="pickup.png")

        e = embed(
            "注文完了！ 受け取り番号を保存してください",
            "マクドナルドへの注文が確定しました。\n"
            "下の番号カードをスクリーンショットで保存してください。",
            OK,
            footer=cfg.BRAND_NAME,
        )
        e.add_field(name="注文番号", value=f"```\n{paid.receipt_number or '----'}\n```", inline=False)
        e.add_field(
            name="店舗",
            value=f"{paid.store_name or payload['store_name'] or '-'}\n`{decoded.store_id}`",
            inline=True,
        )
        e.add_field(name="受取方法", value=decoded.pickup_method or "-", inline=True)
        if paid.short_code:
            e.add_field(name="照合コード", value=f"`{paid.short_code}`", inline=True)
        e.add_field(
            name="お支払い額",
            value=f"**{yen(payload['pay'])}**（定価 {yen(payload['face'])} の {payload['rate']}%）",
            inline=False,
        )
        e.add_field(name="残高（残）", value=f"`{balance_after}` 円", inline=True)
        e.add_field(
            name=f"{cfg.E_FOOD} 累計注文",
            value=(
                f"{cfg.E_GIFT} 初注文ありがとうございます！\n累計 1 回"
                if total_orders <= 1
                else f"累計 {total_orders} 回"
            ),
            inline=True,
        )
        e.add_field(
            name=f"{cfg.E_MEMO} 感想を送ってください（次回注文の必須条件）",
            value=(
                "**この BOT への DM で感想を送らないと、次回の注文が受け付けられません。**\n"
                f"・{cfg.E_MAIL} 感想のみ（テキスト）でも OK\n"
                f"・{cfg.E_CAMERA} 画像も添えると **残高 +{cfg.PHOTO_BONUS} 円**\n"
                f"・{cfg.E_WARN} 画像だけ（感想なし）では解禁されません"
            ),
            inline=False,
        )
        e.set_image(url="attachment://pickup.png")

        sent = await dm(self.bot, uid, e=e, files=[file])
        if sent is None:
            await self.bot.send_log(
                cfg.LOG_ERRORS_CHANNEL_ID,
                embed(
                    f"{cfg.E_WARN} DM を送れませんでした",
                    f"<@{uid}> ・ 注文 #{order_id} ・ 注文番号 **{paid.receipt_number}**\n"
                    "DM が拒否されています。番号を直接お伝えください。",
                    WARN,
                ),
            )

    async def _post_stage1(self, uid: int, pickup: str) -> None:
        """1段階目の実績。番号・店舗・金額は出さない。"""
        cfg = self.bot.cfg
        e = embed(
            f"{cfg.E_CAMERA} 商品レポート",
            f"<@{uid}> が注文しました！",
            discord.Color.from_rgb(230, 126, 34),
            footer=f"{cfg.BRAND_NAME} ・ 実績",
        )
        e.add_field(name="受取方法", value=pickup or "-", inline=False)
        e.timestamp = now_jst()
        await post(self.bot, cfg.ACHIEVEMENT_CHANNEL_ID, embed=e)

    def _order_log_embed(self, uid, order_id, paid, payload, trace) -> discord.Embed:
        cfg = self.bot.cfg
        e = embed(
            f"{cfg.E_OK} 注文成立",
            f"<@{uid}> ・ 注文 #{order_id}",
            OK,
            footer=f"trace {trace}",
        )
        e.add_field(name="注文番号", value=paid.receipt_number or "-", inline=True)
        e.add_field(name="店舗", value=f"{paid.store_name or '-'} ({payload['decoded'].store_id})", inline=True)
        e.add_field(name="受取", value=payload["decoded"].pickup_method or "-", inline=True)
        e.add_field(name="定価", value=yen(payload["face"]), inline=True)
        e.add_field(name="利用者負担", value=f"{yen(payload['pay'])} ({payload['rate']}%)", inline=True)
        e.add_field(name="オーナー負担", value=yen(payload["face"] - payload["pay"]), inline=True)
        e.add_field(name="使用アカウント", value=f"{paid.account_label} / {paid.group}", inline=True)
        return e

    async def _alert_fraud(self, uid: int, verdict, context: str) -> None:
        cfg = self.bot.cfg
        if verdict.score < 40:
            return
        await post(
            self.bot,
            cfg.APPROVAL_CHANNEL_ID,
            embed=embed(
                f"{cfg.E_FLAG} 不正検知アラート (スコア {verdict.score})",
                f"<@{uid}> ・ {context}\n\n{verdict.reasons()}",
                WARN,
            ),
        )

    async def _alert_uncertain(self, uid: int, order_id: int, payload: dict, message: str) -> None:
        cfg = self.bot.cfg
        owner_mentions = " ".join(f"<@{oid}>" for oid in (self.bot.owner_ids or []))
        await post(
            self.bot,
            cfg.LOG_ERRORS_CHANNEL_ID,
            content=owner_mentions or None,
            embed=embed(
                f"{cfg.E_WARN} 決済の成否が不明です",
                f"<@{uid}> ・ 注文 #{order_id}\n"
                f"定価 {yen(payload['face'])} / 引き落とし {yen(payload['pay'])}\n\n"
                f"```{message[:600]}```\n"
                "マクドナルドのアプリで実際に注文が入っているか確認し、"
                f"`/mcd adjust` で残高を調整してください。",
                BAD,
            ),
        )

    # ------------------------------------------------------------ 残高・履歴

    async def show_balance(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.cfg
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)

        row = await asyncio.to_thread(self.bot.store.ensure_user, uid)
        ledger = await asyncio.to_thread(self.bot.store.ledger_of, uid, 8)
        orders = await asyncio.to_thread(self.bot.store.orders_of, uid, 5)

        e = embed(f"{cfg.E_MONEY} 残高と履歴", None, MONEY, footer=cfg.BRAND_NAME)
        e.add_field(name="残高", value=f"**{yen(row['balance'])}**", inline=True)
        e.add_field(name="累計注文", value=f"{row['total_orders']} 回", inline=True)
        saved = int(row["total_face"]) - int(row["total_paid"])
        e.add_field(name="これまでの割引", value=yen(saved), inline=True)

        if ledger:
            lines = []
            for entry in ledger:
                sign = "+" if int(entry["amount"]) > 0 else ""
                label = {
                    "charge": "チャージ", "order": "注文", "refund": "返金",
                    "referral": "紹介報酬", "photo_bonus": "画像ボーナス", "adjust": "調整",
                }.get(entry["kind"], entry["kind"])
                lines.append(
                    f"`{entry['created_at'][5:16]}` {label} {sign}{int(entry['amount']):,} → {int(entry['balance_after']):,}"
                )
            e.add_field(name="残高の動き", value="\n".join(lines)[:1000], inline=False)

        if orders:
            lines = []
            for order in orders:
                mark = {"paid": cfg.E_OK, "failed": cfg.E_NG, "unknown": cfg.E_WARN}.get(
                    order["status"], cfg.E_CLOCK
                )
                lines.append(
                    f"{mark} `{order['created_at'][5:16]}` {order['store_name'] or order['store_id']} "
                    f"/ {yen(order['paid_amount'])} / 番号 {order['receipt_number'] or '-'}"
                )
            e.add_field(name="注文履歴", value="\n".join(lines)[:1000], inline=False)

        await reply(interaction, e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Panel(bot))
