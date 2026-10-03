"""
利用者の操作フロー

利用者はスラッシュコマンドを使えないため、すべて常設パネルのボタンから始まる。
ここで開くビューは一時的なもので、押した本人にだけ見える（ephemeral）。
"""

from __future__ import annotations

import asyncio
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
from core import fraud
from core import queue as order_gate
from core.telemetry import traced
from db.models import as_utc, Order, User
from db.session import session_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import availability, slot_bridge
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError
from services.mcd.protocol import (
    PICKUP_LABEL, DecodedOrder, ProtocolError, decode_hex,
)
from ui import balance_panel, embeds

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
        created = as_utc(o.created_at)
        when = created.astimezone(config.JST).strftime("%m/%d %H:%M") if created else "—"
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

    @traced("チャージ")
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

        # 残高が増えたことを、使ったチャンネルに誰でも見える形で出す
        await balance_panel.post(
            interaction,
            amount=result.amount,
            balance=result.balance,
            reason=balance_panel.REASON_CHARGE,
        )


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


async def has_usable_account() -> bool:
    """注文に使えるマクドナルドアカウントがあるか。"""
    from sqlalchemy import func

    from db.models import McdAccount
    from services.mcd.accounts import USABLE

    async with session_scope() as s:
        n = await s.scalar(
            select(func.count()).select_from(McdAccount).where(
                McdAccount.status.in_(USABLE),
                McdAccount.card_id.isnot(None),
                McdAccount.card_id != "",
            )
        )
    return bool(n)


async def _no_account_notice(interaction: discord.Interaction) -> None:
    """
    アカウント未登録のときの案内。

    何も設定していない状態でいきなり注文されると、
    分かりにくいエラーになってしまうため、手前で止めて伝える。
    """
    embed = embeds.warn(
        "ただいま注文を受け付けできません。\n"
        "管理者にお問い合わせください。",
        title=f"{E.NG} 注文を受け付けできません",
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def start_order(interaction: discord.Interaction) -> None:
    """
    注文の入口。

    注文方式が片方に絞られている場合は、選ぶ画面を挟まずに直接そちらへ進む。
    """
    await user_repo.get_or_create(interaction.user.id)

    if not await has_usable_account():
        log.warning("使用できるマクドナルドアカウントがありません")
        await _no_account_notice(interaction)
        return

    mode = settings.get("order_mode", "both")

    if mode == "hex":
        await interaction.response.send_modal(HexModal())
        return
    if mode == "menu":
        from ui import menu_flows

        await menu_flows.start_store_select(interaction, purpose="order")
        return

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

    if not await has_usable_account():
        await _no_account_notice(interaction)
        return
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

    # 実際に通った注文コードから、選択枠の中間ノードを学ぶ。
    # この情報はメニューカタログに載っていないため、
    # 貼られたコードが唯一の手がかりになる（docs/09 §2）。
    try:
        learned = slot_bridge.learn_from_order(decoded.items)
        if learned:
            log.info("注文コードから選択枠の構造を %d 件おぼえました", learned)
    except Exception:
        log.debug("選択枠の学習に失敗しました（注文には影響しません）", exc_info=True)

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
    store_group = ""
    handle = None
    try:
        handle = await mcd_accounts.pick_account()
        info = await mcd_stores.resolve_store(handle.client, decoded.store_id)
        store_name, supported = info.name, info.delivery_methods
        store_group = info.group
        try:
            await mcd_stores.ensure_menu_fresh(handle.client, decoded.store_id)
        except Exception:
            log.info("メニューの更新に失敗しました（続行します）", exc_info=True)
    except McdError as e:
        log.warning("店舗の解決に失敗しました: %s", e)
    finally:
        if handle:
            await handle.aclose()

    # 受取方法を選んでいる間に、裏で注文の下ごしらえをしておく
    if store_group:
        asyncio.create_task(mcd_accounts.warm_up(decoded.store_id, store_group))

    # 注文コードの店舗が、いま注文を受け付けているか確かめる。
    # 残高を確保する前にここで止める。
    av = await availability.check_store(decoded.store_id)
    if not av.orderable:
        await interaction.followup.send(
            embed=embeds.store_unavailable(store_name, decoded.store_id, av),
            ephemeral=True,
        )
        return
    # いまの時間に使える受取方法だけを選ばせる
    if av.methods:
        supported = {m: (m in av.methods) for m in (supported or {})} or {
            m: True for m in av.methods
        }

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
    view.lines = lines   # 送信前に入れる（受取方法を選び直したときに消えないように）
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


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
        # 注文コードに書かれていた受取方法が候補に無い場合、そのままだと
        # 選び直せないまま確定できてしまう。候補に足しておく。
        if self.pickup and not any(o.value == self.pickup for o in options):
            label = config.PICKUP_METHODS.get(self.pickup, {}).get("label", self.pickup)
            options.insert(
                0, discord.SelectOption(label=label, value=self.pickup, default=True)
            )
        if not options:
            options = [discord.SelectOption(label="テイクアウト", value="takeOut", default=True)]
            self.pickup = self.pickup or "takeOut"

        self.select = discord.ui.Select(
            placeholder="受取方法を選んでください", options=options[:25],
            min_values=1, max_values=1,
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

async def wait_for_recovery(interaction: discord.Interaction) -> bool:
    """
    マクドナルド側が落ちていたら、復帰を少し待つ。

    戻り値が False なら、待っても復帰しなかったということ。
    呼び出し側は注文を取り消すこと。

    ⚠️ 判断には外形監視の結果を使う。ここで改めて通信はしない
       （落ちている相手にさらに要求を足さないため）。
    """
    from services import monitor

    healthy = [h for h in monitor.snapshot() if h.ok]
    if healthy or not monitor.snapshot():
        # 生きている配信元がある、またはまだ一度も確認していない
        return True

    waited = 0.0
    step = 5.0
    limit = float(config.ORDER_OUTAGE_WAIT_SECONDS)
    try:
        await interaction.edit_original_response(
            embed=embeds.warn(
                f"{E.LOADING} マクドナルドへ接続できない状態です。\n"
                "復旧を待っていますので、そのままお待ちください。\n\n"
                f"{E.INFO} 最大 {limit:.0f} 秒お待ちします。",
                title=f"{E.WARN} 接続を待っています",
            )
        )
    except discord.HTTPException:
        pass

    while waited < limit:
        await asyncio.sleep(step)
        waited += step
        try:
            report = await monitor.check()
        except Exception:
            continue
        if report.healthy:
            log.info("接続が復帰したため注文を続けます（%.0f秒待ちました）", waited)
            return True

    log.warning("復帰しなかったため注文を取り消します（%.0f秒）", waited)
    await interaction.edit_original_response(
        embed=embeds.error(
            "マクドナルドへ接続できませんでした。\n"
            "しばらくしてからもう一度お試しください。\n\n"
            "残高は元に戻っています。",
            title=f"{E.NG} ただいま注文できません",
        )
    )
    return False



@traced("注文")
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

    # 気になる動きがないか見ておく。
    # ⚠️ ここで利用者を止めることはしない。ふつうに使っている人を
    #    誤って止めるほうが痛いため。管理者に知らせるだけにする。
    try:
        report = await fraud.check_user(interaction.user.id)
        report.findings += (
            await fraud.check_order(interaction.user.id, quote.list_price)
        ).findings
        if report.any:
            await notify_admin_fraud(interaction, report)
    except Exception:
        log.debug("不正検知に失敗しました（注文には影響しません）", exc_info=True)

    # マクドナルド側が落ちている間は、送っても失敗するだけ。
    # 復帰を少し待ってから流すほうが、利用者にとっても相手にとってもよい。
    if not await wait_for_recovery(interaction):
        await saga.cancel_waiting(
            order_id, "マクドナルドへ接続できませんでした"
        )
        return

    # 同時に処理する注文の数に上限がある。混んでいれば順番を待つ。
    # 待っている間も残高は確保したまま（先に解放すると、順番が来たときに
    # 残高が足りなくなりうる）。
    if order_gate.gate.running >= order_gate.gate.limit:
        try:
            await interaction.edit_original_response(
                embed=embeds.info(
                    f"{E.LOADING} ただいま混み合っています。\n"
                    f"順番にお通ししますので、そのままお待ちください。\n\n"
                    f"{E.INFO} あなたの前に **{order_gate.gate.waiting}** 人います。"
                )
            )
        except discord.HTTPException:
            pass

    try:
        async with order_gate.gate.enter(interaction.user.id):
            result = await saga.execute(order_id, progress)
    except (order_gate.QueueFull, order_gate.QueueTimeout) as e:
        log.info("混雑のため注文を受け付けられませんでした: %s", e)
        await saga.cancel_waiting(order_id, str(e))
        await interaction.edit_original_response(
            embed=embeds.warn(
                f"{e}\n\n残高は元に戻っています。",
                title=f"{E.WARN} 混み合っています",
            )
        )
        return
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
        # 解析できていれば、原因に応じた説明を出す。
        # 生の応答をそのまま見せても利用者には分からないため。
        await interaction.edit_original_response(
            embed=embeds.error(result.user_message, title=f"{E.NG} 注文できませんでした")
        )
        # 管理者側にだけ、原因と対処を知らせる
        await notify_admin_failure(interaction, result)
        return

    # DMへ完了パネルを送る
    sent = await send_completion_dm(interaction, result)
    if sent:
        await interaction.edit_original_response(
            embed=embeds.ok("注文が完了しました。\n詳細と控えを DM にお送りしました。")
        )
    else:
        # DMが閉じていても控えは必要なので、この場に画像ごと出す
        await interaction.edit_original_response(
            embed=embeds.warn(
                f"注文は完了しましたが、DMを送信できませんでした。\n"
                "控えはこの下に表示します（あなたにだけ見えています）。\n\n"
                "DMの受信を許可すると、次回からDMにお送りできます。",
                title=f"{E.WARN} DMを送信できませんでした",
            )
        )
        await send_completion_here(interaction, result)

    await push_receipt_page(result)
    await saga.mark_notified(result.order_id)
    await post_achievement(interaction, result)
    await post_balance_change(interaction, result)
    await grant_invite_reward(interaction)


async def push_receipt_page(result: saga.OrderResult) -> None:
    """
    別置きの注文番号ページへ登録する。

    ⚠️ 送れなくても注文は成立している。例外を外へ出さない。
       控えのDMには注文番号が入っているので、ページが無くても困らない。
    """
    try:
        from services import web_push

        if not web_push.configured():
            return
        await web_push.send(
            token=result.view_token,
            receipt_number=result.receipt_number,
            store_name=result.store_name or "",
            store_id=result.store_id or "",
            pickup_label=result.pickup_label or "",
        )
    except Exception:
        log.exception("注文番号をページへ送れませんでした")


async def grant_invite_reward(interaction: discord.Interaction) -> None:
    """
    招待の特典は「招待された人の初回注文」で確定することが多いので、
    注文が成立したここで確かめる。

    ⚠️ ここで何があっても注文は成立済み。例外を外へ出さない。
    """
    try:
        from core import invite as inv
        from ui import invite_flows

        paid = await inv.grant_if_ready(interaction.user.id)
        if not paid:
            return
        async with session_scope() as s:
            row = await s.get(User, interaction.user.id)
        inviter_id = row.invited_by if row else None
        if inviter_id:
            await invite_flows.announce(
                interaction.client, interaction.user.id, int(inviter_id), paid
            )
    except Exception:
        log.exception("招待の特典を渡せませんでした")


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
    # 受け取り画面のリンク。自前のサイトを立てているならそちらを優先する。
    # どちらも無ければ空文字が返るのでボタンを出さない。
    from services import web as web_site
    from services import web_push

    link = (
        web_push.page_link(result.view_token)      # 別の場所に置いたページ
        or web_site.page_url(result.view_token)    # BOTが自分で配信するページ
        or receipt_svc.receipt_view_url(           # 外部サイト（飾り）
            result.store_id, result.receipt_number
        )
    )
    if link:
        view.add_item(
            discord.ui.Button(
                label="受け取り画面を開く", emoji=E.RECEIPT,
                style=discord.ButtonStyle.link, url=link,
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


async def send_completion_here(
    interaction: discord.Interaction, result: saga.OrderResult
) -> bool:
    """
    DMが閉じているときの代わりの届け先。

    控え画像と注文番号を、本人にだけ見える形でその場に出す。
    注文番号が分からないと店頭で受け取れないので、ここは落とせない。
    """
    from services import receipt as receipt_svc

    image = None
    try:
        image = await receipt_svc.render(result.receipt_number)
    except Exception:
        log.exception("レシート画像の生成に失敗しました")

    embed = embeds.receipt_fallback(
        receipt_number=result.receipt_number,
        store_name=result.store_name,
        store_id=result.store_id,
        pickup_label=result.pickup_label,
        with_image=image is not None,
    )

    kwargs: dict = {"embed": embed, "ephemeral": True}
    if image is not None:
        kwargs["file"] = discord.File(image, filename="receipt.png")
    link = receipt_svc.receipt_view_url(result.store_id, result.receipt_number)
    if link:
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="受け取り画面を開く", emoji=E.RECEIPT,
                style=discord.ButtonStyle.link, url=link,
            )
        )
        kwargs["view"] = view

    try:
        await interaction.followup.send(**kwargs)
        return True
    except discord.HTTPException:
        log.exception("控えの表示に失敗しました")
        return False


async def daily_order_count() -> int:
    """本日成立した注文の件数。実績パネルの「本日◯件目」に使う。"""
    from sqlalchemy import func

    # 日本時間の0時から数える（UTCの0時だと日本の朝9時で切り替わってしまう）。
    # ⚠️ DBはUTCで保存しているので、比較する前にUTCへ直す。
    today = config.jst_midnight_utc()
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


async def post_balance_change(
    interaction: discord.Interaction, result: saga.OrderResult
) -> None:
    """注文で残高が減ったことを、使ったチャンネルへ公開パネルで出す。"""
    store = result.store_name or result.store_id or ""
    reason = f"{balance_panel.REASON_ORDER}{f'（{store}）' if store else ''}"
    await balance_panel.post(
        interaction,
        amount=-abs(result.user_amount),
        balance=result.balance_after,
        reason=reason,
        total_orders=result.total_orders,
    )


async def notify_admin_fraud(
    interaction: discord.Interaction, report
) -> None:
    """
    気になる動きを管理者へ知らせる。

    止めるかどうかは人が決める。設定で自動停止もできるが、既定は通知だけ。
    """
    channel_id = settings.get("channel_admin")
    if not channel_id:
        return
    channel = interaction.client.get_channel(int(channel_id))
    if channel is None:
        return

    color = {
        fraud.HIGH: embeds.RED, fraud.WARN: embeds.ORANGE,
    }.get(report.worst, embeds.BLUE)
    e = discord.Embed(
        title=f"{E.WARN} 気になる動きがあります",
        description=fraud.format_report(report)[:4000],
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(
        name=f"{E.USER} 利用者",
        value=f"{interaction.user.mention}\n`{interaction.user.id}`",
        inline=True,
    )
    e.set_footer(text="/admin user で詳しく見られます")
    try:
        await channel.send(embed=e)
    except discord.HTTPException:
        log.exception("不正検知の通知を送れませんでした")


async def notify_admin_failure(
    interaction: discord.Interaction, result: saga.OrderResult
) -> None:
    """
    注文の失敗を管理者チャンネルへ知らせる。

    利用者に見せない詳細（どのアカウントで何が起きたか、対処方法）を
    ここに出す。カードが使えない場合など、放置すると全員の注文が
    失敗し続けるため、気付けるようにしておく。
    """
    info = result.error_info
    # 利用者の操作ミス（時間外の商品を選んだ等）は通知しない。
    # 管理者が何もできないうえ、件数が多いと本当の異常が埋もれる。
    from services.mcd import errors as mcd_errors

    quiet = {mcd_errors.PRODUCT_TIME, mcd_errors.PRODUCT_GONE, mcd_errors.STORE}
    if info is not None and getattr(info, "kind", "") in quiet:
        return

    channel_id = settings.get("channel_admin")
    if not channel_id:
        return
    channel = interaction.client.get_channel(int(channel_id))
    if channel is None:
        return

    kind = getattr(info, "kind", "UNKNOWN") if info else "UNKNOWN"
    urgent = bool(info is not None and getattr(info, "account_fault", False))
    e = discord.Embed(
        title=f"{E.NG if urgent else E.WARN} 注文が失敗しました",
        description=(
            getattr(info, "admin_text", "") if info
            else "原因を特定できませんでした。"
        ),
        color=embeds.RED if urgent else embeds.ORANGE,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name=f"{E.CHART} 種類", value=f"`{kind}`", inline=True)
    if info is not None and getattr(info, "code", ""):
        e.add_field(name="符号", value=f"`{info.code}`", inline=True)
    if result.store_name or result.store_id:
        e.add_field(
            name=f"{E.STORE} 店舗",
            value=f"{result.store_name or '—'}（`{result.store_id}`）",
            inline=True,
        )
    if info is not None and getattr(info, "message", ""):
        e.add_field(name=f"{E.INFO} 相手からの文言", value=info.message[:500], inline=False)
    e.add_field(
        name=f"{E.RECEIPT} 注文", value=f"`{result.order_id[:8]}`", inline=True
    )
    if info is not None and getattr(info, "raw", ""):
        e.add_field(
            name=f"{E.NOTE} 応答の中身",
            value=f"```{info.raw[:500]}```",
            inline=False,
        )
    if urgent:
        e.set_footer(text="このアカウントは候補から外しました。対処するまで他のアカウントで動きます")
    try:
        await channel.send(embed=e)
    except discord.HTTPException:
        log.exception("失敗の通知を送れませんでした")


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
