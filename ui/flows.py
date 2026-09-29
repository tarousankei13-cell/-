"""
利用者の操作フロー

利用者はスラッシュコマンドを使えないため、すべて常設パネルのボタンから始まる。
ここで開くビューは一時的なもので、押した本人にだけ見える（ephemeral）。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import discord
from sqlalchemy import select

import config
import emoji as E
from core import ledger as L
from core import saga, settings, subsidy
from core import users as user_repo
from db.models import Order, User
from db.session import session_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError
from services.mcd.protocol import (
    PICKUP_LABEL, DecodedOrder, ProtocolError, decode_hex,
)
from ui import embeds

log = logging.getLogger("bot.flows")


def role_ids(interaction: discord.Interaction) -> list[int]:
    return [r.id for r in getattr(interaction.user, "roles", [])]


async def _user_rate(interaction: discord.Interaction) -> float:
    """このユーザーの支払い率(%)。表示用。"""
    async with session_scope() as s:
        q = await subsidy.resolve(s, interaction.user.id, role_ids(interaction), 1000)
    return q.user_rate


# ============================================================
#  残高・履歴
# ============================================================

async def show_balance(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    await user_repo.get_or_create(interaction.user.id)
    async with session_scope() as s:
        balance = await L.user_balance(s, interaction.user.id)
        held = await L.held_balance(s, interaction.user.id)
        user = await s.get(User, interaction.user.id)
        q = await subsidy.resolve(s, interaction.user.id, role_ids(interaction), 1000)
    await interaction.followup.send(
        embed=embeds.balance_card(
            balance=balance, held=held, rate=q.user_rate,
            total_orders=user.total_orders if user else 0,
        ),
        ephemeral=True,
    )


async def show_history(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Order)
                .where(Order.discord_id == interaction.user.id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        balance = await L.user_balance(s, interaction.user.id)

    if not rows:
        await interaction.followup.send(
            embed=embeds.info("まだ注文履歴がありません。"), ephemeral=True
        )
        return

    e = discord.Embed(title=f"{E.HISTORY} 注文履歴（最新10件）", color=embeds.BLUE)
    for o in rows:
        mark = {
            saga.COMPLETED: E.OK, saga.NOTIFIED: E.OK, saga.CAPTURED: E.OK,
            saga.REFUNDED: E.NG, saga.MANUAL_REVIEW: E.WARN,
        }.get(o.state, E.LOADING)
        when = o.created_at.strftime("%m/%d %H:%M") if o.created_at else "—"
        e.add_field(
            name=f"{mark} {when}　{o.store_name or o.store_id or '—'}",
            value=(
                f"注文番号 `{o.receipt_number or '—'}`　"
                f"{embeds.yen(o.user_amount)}（定価 {embeds.yen(o.list_price)}）"
            ),
            inline=False,
        )
    e.set_footer(text=f"現在の残高 {embeds.yen(balance)}")
    await interaction.followup.send(embed=e, ephemeral=True)


# ============================================================
#  チャージ
# ============================================================

class ChargeModal(discord.ui.Modal, title="残高チャージ"):
    link = discord.ui.TextInput(
        label="Kyash 送金リンク",
        placeholder="https://kyash.me/payments/xxxxxxxx",
        required=True,
        max_length=255,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.kyash.charge import ChargeError, charge_from_link

        await interaction.response.defer(ephemeral=True, thinking=True)
        await user_repo.get_or_create(interaction.user.id)
        try:
            result = await charge_from_link(interaction.user.id, str(self.link.value))
        except ChargeError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        except Exception:
            log.exception("チャージ処理で予期しないエラー")
            await interaction.followup.send(
                embed=embeds.error(
                    "チャージ処理でエラーが発生しました。管理者にお問い合わせください。"
                ),
                ephemeral=True,
            )
            return

        e = discord.Embed(title=f"{E.OK} チャージ完了", color=embeds.GREEN)
        e.add_field(name=f"{E.YEN} 受取額", value=f"**{embeds.yen(result.amount)}**", inline=True)
        e.add_field(name=f"{E.WALLET} 残高", value=f"**{embeds.yen(result.balance)}**", inline=True)
        if result.sender_name:
            e.add_field(name=f"{E.USER} 送金者", value=result.sender_name, inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)


async def open_charge_modal(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(ChargeModal())


# ============================================================
#  注文 — 入口
# ============================================================

class MethodView(discord.ui.View):
    """注文コードを貼るか、メニューから選ぶか。"""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="注文コード(HEX)を貼る", emoji="📋", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(HexModal())

    @discord.ui.button(label="メニューから選ぶ", emoji=E.BURGER, style=discord.ButtonStyle.success)
    async def from_menu(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from ui import menu_flows

        await menu_flows.start_store_select(interaction, purpose="order")


async def start_order(interaction: discord.Interaction) -> None:
    await user_repo.get_or_create(interaction.user.id)
    async with session_scope() as s:
        balance = await L.user_balance(s, interaction.user.id)
        q = await subsidy.resolve(s, interaction.user.id, role_ids(interaction), 1000)
    await interaction.response.send_message(
        embed=embeds.choose_method(balance, q.user_rate),
        view=MethodView(interaction.user.id),
        ephemeral=True,
    )


async def start_hex_builder(interaction: discord.Interaction) -> None:
    from ui import menu_flows

    await menu_flows.start_store_select(interaction, purpose="hex")


# ============================================================
#  注文 — HEX貼り付け
# ============================================================

class HexModal(discord.ui.Modal, title="注文する"):
    code = discord.ui.TextInput(
        label="注文コード（HEX）",
        style=discord.TextStyle.paragraph,
        placeholder="0a05...",
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_preview(interaction, str(self.code.value))


async def describe_items(store_id: str, decoded: DecodedOrder) -> list[str]:
    """商品コードを日本語名に直す。メニュー未取得なら記号のまま出す。"""
    try:
        menu = await mcd_stores.load_menu(store_id)
    except Exception:
        menu = None
    lines = []
    for item in decoded.items:
        name = (
            menu.products[item.product_code].name
            if menu and item.product_code in menu.products
            else f"商品コード {item.product_code}"
        )
        qty = f" ×{item.quantity}" if item.quantity > 1 else ""
        lines.append(f"**{name}**{qty}　{embeds.yen(item.amount)}" if item.amount else f"**{name}**{qty}")
        for comp in item.components:
            for leaf in comp.walk():
                if leaf is comp:
                    continue
                cname = (
                    menu.products[leaf.product_code].name
                    if menu and leaf.product_code in menu.products
                    else None
                )
                if cname:
                    lines.append(f"　└ {cname}")
    return lines


async def open_preview(interaction: discord.Interaction, hex_text: str) -> None:
    """HEXを解析して確認パネルを出す。ここではまだ金は動かさない。"""
    try:
        decoded = decode_hex(hex_text)
    except ProtocolError as e:
        await interaction.followup.send(
            embed=embeds.error(
                f"注文コードを読み取れませんでした。\n```{e}```\n"
                "コピーし直してもう一度お試しください。"
            ),
            ephemeral=True,
        )
        return

    if not decoded.store_id:
        await interaction.followup.send(
            embed=embeds.error("注文コードに店舗の情報が含まれていません。"), ephemeral=True
        )
        return
    if decoded.total_amount <= 0:
        await interaction.followup.send(
            embed=embeds.error("注文コードに金額が含まれていません。"), ephemeral=True
        )
        return

    max_amount = settings.get("order_max_amount")
    if max_amount and decoded.total_amount > int(max_amount):
        await interaction.followup.send(
            embed=embeds.error(f"1回の注文は {embeds.yen(int(max_amount))} までです。"),
            ephemeral=True,
        )
        return

    # 店舗を解決し、ついでにメニューを新しくしておく（先読み）
    store_name, supported = "", {}
    handle = None
    try:
        handle = await mcd_accounts.pick_account()
        info = await mcd_stores.resolve_store(handle.client, decoded.store_id)
        store_name, supported = info.name, info.delivery_methods
        try:
            await mcd_stores.ensure_menu_fresh(handle.client, decoded.store_id)
        except Exception:
            log.info("メニューの更新に失敗しました（続行します）", exc_info=True)
    except McdError as e:
        log.warning("店舗の解決に失敗しました: %s", e)
    finally:
        if handle:
            await handle.aclose()

    async with session_scope() as s:
        quote = await subsidy.resolve(
            s, interaction.user.id, role_ids(interaction), decoded.total_amount
        )
        balance = await L.user_balance(s, interaction.user.id)

    lines = await describe_items(decoded.store_id, decoded)
    view = ConfirmView(
        owner_id=interaction.user.id, decoded=decoded, quote=quote,
        balance=balance, store_name=store_name, supported=supported,
    )
    await interaction.followup.send(
        embed=view.build_embed(lines), view=view, ephemeral=True
    )
    view.lines = lines


class ConfirmView(discord.ui.View):
    """受取方法を選んでから確定する。"""

    def __init__(
        self, *, owner_id: int, decoded: DecodedOrder, quote: subsidy.Quote,
        balance: int, store_name: str, supported: dict[str, bool],
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.decoded = decoded
        self.quote = quote
        self.balance = balance
        self.store_name = store_name
        self.supported = supported
        self.lines: list[str] = []
        self.pickup: str | None = decoded.pickup_method
        self.idempotency_key = str(uuid.uuid4())
        self._running = False

        options = []
        for method, cfg in config.PICKUP_METHODS.items():
            if not cfg["enabled"]:
                continue
            if supported and not supported.get(method, False):
                continue
            options.append(
                discord.SelectOption(
                    label=cfg["label"], value=method,
                    default=(method == self.pickup),
                )
            )
        if not options:
            options = [discord.SelectOption(label="テイクアウト", value="takeOut", default=True)]
            self.pickup = self.pickup or "takeOut"

        self.select = discord.ui.Select(
            placeholder="受取方法を選んでください", options=options, min_values=1, max_values=1,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.confirm_button = discord.ui.Button(
            label="注文を確定する", emoji=E.OK, style=discord.ButtonStyle.success,
            disabled=self._blocked(),
        )
        self.confirm_button.callback = self._on_confirm
        self.add_item(self.confirm_button)

        cancel = discord.ui.Button(label="キャンセル", emoji=E.NG, style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    def _blocked(self) -> bool:
        return self.pickup is None or self.balance < self.quote.user_amount

    def build_embed(self, lines: list[str] | None = None) -> discord.Embed:
        warning = None
        if self.balance < self.quote.user_amount:
            warning = (
                f"残高が不足しています。\n"
                f"必要 {embeds.yen(self.quote.user_amount)} / "
                f"現在 {embeds.yen(self.balance)}\n"
                f"{E.CHARGE} チャージパネルからチャージしてください。"
            )
        elif self.pickup is None:
            warning = "受取方法を選んでください。"
        return embeds.order_preview(
            store_name=self.store_name, store_id=self.decoded.store_id,
            item_lines=lines if lines is not None else self.lines,
            quote=self.quote, balance=self.balance,
            pickup_label=PICKUP_LABEL.get(self.pickup or "", None),
            warning=warning,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.pickup = self.select.values[0]
        for opt in self.select.options:
            opt.default = opt.value == self.pickup
        self.confirm_button.disabled = self._blocked()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info("注文をキャンセルしました。"), view=None
        )

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        # ボタン連打への備え。冪等キーでもDB側で弾くが、手前でも止める。
        if self._running:
            await interaction.response.send_message(
                embed=embeds.warn("すでに処理中です。完了までお待ちください。"), ephemeral=True
            )
            return
        self._running = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=embeds.progress([], current="hold"), view=None
        )
        self.stop()
        await run_order(
            interaction, self.decoded, self.quote, self.pickup or "takeOut",
            self.store_name, self.idempotency_key,
        )


# ============================================================
#  注文の実行と通知
# ============================================================

async def run_order(
    interaction: discord.Interaction,
    decoded: DecodedOrder,
    quote: subsidy.Quote,
    pickup: str,
    store_name: str,
    idempotency_key: str,
) -> None:
    done: list[str] = []
    detail: dict[str, str] = {}

    async def progress(step: str, text: str) -> None:
        detail[step] = text
        done.append(step)
        order = [k for k, _ in embeds.PROGRESS_STEPS]
        nxt = next((k for k in order if k not in done), None)
        try:
            await interaction.edit_original_response(
                embed=embeds.progress(done, current=nxt, detail=detail)
            )
        except discord.HTTPException:
            pass

    async with session_scope() as s:
        group = ""
        from db.models import StoreCache

        cache = await s.get(StoreCache, decoded.store_id)
        if cache:
            group = cache.group_name

    order_id = await saga.create_order(
        discord_id=interaction.user.id, decoded=decoded, quote=quote,
        pickup_method=pickup, store_name=store_name, group=group,
        idempotency_key=idempotency_key,
    )

    try:
        result = await saga.execute(order_id, progress)
    except Exception:
        log.exception("注文の実行に失敗しました")
        await interaction.edit_original_response(
            embed=embeds.error(
                "注文処理でエラーが発生しました。管理者が確認しますのでお待ちください。"
            )
        )
        return

    if result.needs_review:
        await interaction.edit_original_response(
            embed=embeds.warn(
                "注文の確認に時間がかかっています。\n"
                "管理者が確認していますので、そのままお待ちください。",
                title=f"{E.WARN} 確認中です",
            )
        )
        await notify_admin_review(interaction.client, result)
        return

    if not result.succeeded:
        await interaction.edit_original_response(
            embed=embeds.error(
                f"注文できませんでした。\n```{result.error[:500]}```\n"
                "残高は元に戻っています。"
            )
        )
        return

    # DMへ完了パネルを送る
    sent = await send_completion_dm(interaction, result)
    if sent:
        await interaction.edit_original_response(
            embed=embeds.ok("注文が完了しました。\n詳細と控えを DM にお送りしました。")
        )
    else:
        await interaction.edit_original_response(
            embed=embeds.warn(
                f"注文は完了しましたが、DMを送信できませんでした。\n\n"
                f"{E.RECEIPT} **注文番号　`{result.receipt_number or '—'}`**\n"
                f"{E.STORE} {result.store_name}（`{result.store_id}`）\n\n"
                "DMの受信を許可すると、次回から控えをお送りできます。",
                title=f"{E.WARN} DMを送信できませんでした",
            )
        )

    await saga.mark_notified(result.order_id)
    await post_achievement(interaction, result)


async def send_completion_dm(interaction: discord.Interaction, result: saga.OrderResult) -> bool:
    from services import receipt as receipt_svc

    try:
        image = await receipt_svc.render(result.receipt_number)
    except Exception:
        log.exception("レシート画像の生成に失敗しました")
        image = None

    feedback = bool(settings.get("feedback_gate", False))
    embed = embeds.dm_complete(
        receipt_number=result.receipt_number,
        store_name=result.store_name,
        store_id=result.store_id,
        pickup_label=result.pickup_label,
        list_price=result.list_price,
        user_amount=result.user_amount,
        subsidy_rate=result.subsidy_rate,
        balance=result.balance_after,
        total_orders=result.total_orders,
        feedback_required=feedback,
    )

    view = discord.ui.View()
    if result.receipt_number and result.store_id:
        view.add_item(
            discord.ui.Button(
                label="受け取り画面を開く", emoji=E.RECEIPT, style=discord.ButtonStyle.link,
                url=receipt_svc.receipt_view_url(result.store_id, result.receipt_number),
            )
        )

    kwargs = {"embed": embed}
    if view.children:
        kwargs["view"] = view
    if image is not None:
        kwargs["file"] = discord.File(image, filename="receipt.png")
    else:
        embed.set_image(url=None)

    try:
        await interaction.user.send(**kwargs)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException:
        log.exception("DMの送信に失敗しました")
        return False


async def daily_order_count() -> int:
    """本日成立した注文の件数。実績パネルの「本日◯件目」に使う。"""
    from sqlalchemy import func

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as s:
        count = await s.scalar(
            select(func.count()).select_from(Order).where(
                Order.created_at >= today,
                Order.state.in_([saga.COMPLETED, saga.NOTIFIED, saga.CAPTURED]),
            )
        )
    return int(count or 0)


async def send_achievement(
    client: discord.Client,
    *,
    discord_id: int,
    display_name: str | None = None,
    list_price: int,
    subsidy_rate: float,
    user_amount: int,
    store_name: str | None = None,
    receipt_number: str | None = None,
    pickup_label: str | None = None,
    daily_count: int | None = None,
) -> bool:
    """
    実績チャンネルへ投稿する。

    通常の注文からも、管理者による代理送信からも**この関数だけ**を通す。
    そうしておけば、どちらから送っても見た目が食い違うことがない。
    """
    channel_id = settings.get("channel_achievement")
    if not channel_id:
        return False
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return False

    if daily_count is None:
        daily_count = await daily_order_count()

    async with session_scope() as s:
        user = await s.get(User, discord_id)
    anon = user.anon_code if user else user_repo.anon_code(discord_id)

    embed = embeds.achievement(
        anon_code=anon,
        username=display_name,
        list_price=list_price,
        subsidy_rate=subsidy_rate,
        user_amount=user_amount,
        daily_count=daily_count,
        store_name=store_name,
        receipt_number=receipt_number,
        pickup_label=pickup_label,
        fields=settings.get("achievement_fields", config.ACHIEVEMENT_FIELDS_DEFAULT),
    )
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException:
        log.exception("実績の投稿に失敗しました")
        return False


async def post_achievement(interaction: discord.Interaction, result: saga.OrderResult) -> None:
    """注文が成立したとき、実績チャンネルへ1回だけ投稿する。"""
    await send_achievement(
        interaction.client,
        discord_id=interaction.user.id,
        display_name=interaction.user.display_name,
        list_price=result.list_price,
        subsidy_rate=result.subsidy_rate,
        user_amount=result.user_amount,
        store_name=result.store_name,
        receipt_number=result.receipt_number,
        pickup_label=result.pickup_label,
    )


async def notify_admin_review(client: discord.Client, result: saga.OrderResult) -> None:
    channel_id = settings.get("channel_admin")
    if not channel_id:
        return
    channel = client.get_channel(int(channel_id))
    if channel is None:
        return
    e = discord.Embed(
        title=f"{E.WARN} 要確認の注文が発生しました",
        description=(
            "課金が成立している可能性があるため、自動返金を行っていません。\n"
            "管理パネルの「要確認」から処理してください。"
        ),
        color=embeds.RED,
    )
    e.add_field(name="注文ID", value=f"`{result.order_id}`", inline=False)
    e.add_field(name="金額", value=embeds.yen(result.user_amount), inline=True)
    e.add_field(name="注文番号", value=result.receipt_number or "—", inline=True)
    if result.error:
        e.add_field(name="エラー", value=f"```{result.error[:500]}```", inline=False)
    try:
        await channel.send(embed=e)
    except discord.HTTPException:
        log.exception("管理者通知の送信に失敗しました")
