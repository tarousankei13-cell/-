"""
メニューからの注文組み立て

mcdon.asia を使わず、Discord 上だけで注文を作る。
商品・価格・カスタマイズ構造はマクドナルドのカタログから取得する（docs/08）。

purpose:
  "order" … そのまま注文する
  "hex"   … 注文コードを作るだけ（決済しない）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy import select

import config
import emoji as E
from core import ledger as L
from core import subsidy
from core import users as user_repo
from db.models import Cart, StoreCache, utcnow
from db.session import session_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import store_index
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError
from services.mcd.menu import ParsedMenu, Product, minutes_of
from services.mcd.protocol import (
    PICKUP_LABEL, DecodedOrder, OrderItem, build_hex,
)
from ui import embeds, flows

log = logging.getLogger("bot.menu_flows")

PAGE_SIZE = 25


def now_minutes() -> int:
    return minutes_of(datetime.now())


# ============================================================
#  カートの保存
# ============================================================

async def load_cart(discord_id: int) -> tuple[str, str | None, list[OrderItem]]:
    async with session_scope() as s:
        row = await s.get(Cart, discord_id)
        if row is None:
            return "", None, []
        items = [OrderItem.from_dict(d) for d in json.loads(row.items_json or "[]")]
        return row.store_id or "", row.pickup_method, items


async def save_cart(
    discord_id: int, *, purpose: str, store_id: str,
    pickup: str | None, items: list[OrderItem],
) -> None:
    async with session_scope() as s:
        row = await s.get(Cart, discord_id)
        if row is None:
            row = Cart(discord_id=discord_id, purpose=purpose, items_json="[]")
            s.add(row)
        row.purpose = purpose
        row.store_id = store_id
        row.pickup_method = pickup
        row.items_json = json.dumps([i.to_dict() for i in items], ensure_ascii=False)
        row.updated_at = utcnow()


async def clear_cart(discord_id: int) -> None:
    async with session_scope() as s:
        row = await s.get(Cart, discord_id)
        if row:
            await s.delete(row)


# ============================================================
#  店舗の選択
# ============================================================

class StoreSearchModal(discord.ui.Modal, title="お店をさがす"):
    query = discord.ui.TextInput(
        label="店名の一部（店舗IDでも可）",
        placeholder="例: 南砂　／　所沢　／　AKIBA　／　13934",
        required=True, max_length=40,
    )

    def __init__(self, purpose: str) -> None:
        super().__init__()
        self.purpose = purpose

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await show_search_results(interaction, str(self.query.value), self.purpose)


class StoreIdModal(discord.ui.Modal, title="店舗IDを入力"):
    store_id = discord.ui.TextInput(
        label="店舗ID（5桁の数字）",
        placeholder="例: 13934",
        required=True, max_length=8,
    )

    def __init__(self, purpose: str) -> None:
        super().__init__()
        self.purpose = purpose

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_menu(interaction, str(self.store_id.value).strip(), self.purpose)


async def show_search_results(
    interaction: discord.Interaction, query: str, purpose: str
) -> None:
    """検索結果を選択肢として出す。"""
    hits = store_index.search(query, limit=25)

    # インデックスに無くても、過去に使った店舗からは探せるようにする
    if not hits:
        norm = store_index.normalize(query)
        async with session_scope() as s:
            rows = (await s.execute(select(StoreCache))).scalars().all()
        hits = [
            store_index.StoreEntry(
                store_id=r.store_id, name=r.store_name or "", address=r.address or "",
                group=r.group_name,
            )
            for r in rows
            if norm and (
                norm in store_index.normalize(r.store_name or "")
                or norm in store_index.normalize(r.address or "")
                or norm in r.store_id
            )
        ][:25]

    if not hits:
        hint = (
            "店名の一部（例:「南砂」「所沢」）や、店舗IDでもお試しください。"
            if store_index.available()
            else "店舗IDを直接入力してください。"
        )
        await interaction.followup.send(
            embed=embeds.warn(
                f"「{query}」に一致するお店が見つかりませんでした。\n{hint}"
            ),
            view=StoreSelectView(interaction.user.id, purpose, []),
            ephemeral=True,
        )
        return

    if len(hits) == 1:
        await open_menu(interaction, hits[0].store_id, purpose)
        return

    view = SearchResultView(interaction.user.id, purpose, hits, query)
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


class SearchResultView(discord.ui.View):
    def __init__(self, owner_id: int, purpose: str, hits: list, query: str) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose
        self.query = query

        options = [
            discord.SelectOption(
                label=e.name[:100] or e.store_id,
                value=e.store_id,
                description=(e.address or f"店舗ID {e.store_id}")[:100],
            )
            for e in hits[:25]
        ]
        sel = discord.ui.Select(placeholder="お店を選んでください", options=options, row=0)
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

        again = discord.ui.Button(
            label="もう一度さがす", emoji="🔍", style=discord.ButtonStyle.secondary, row=1
        )
        again.callback = self._on_again
        self.add_item(again)

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{E.STORE} 「{self.query}」の検索結果",
            description=f"{len(self._sel.options)} 件見つかりました。お店を選んでください。",
            color=embeds.GREEN,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def _on_again(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoreSearchModal(self.purpose))

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_menu(interaction, self._sel.values[0], self.purpose)


class StoreSelectView(discord.ui.View):
    def __init__(self, owner_id: int, purpose: str, recent: list[tuple[str, str]]) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose

        search = discord.ui.Button(
            label="店名でさがす", emoji="🔍", style=discord.ButtonStyle.primary, row=0
        )
        search.callback = self._on_search
        self.add_item(search)

        by_id = discord.ui.Button(
            label="店舗IDで指定", emoji="🔢", style=discord.ButtonStyle.secondary, row=0
        )
        by_id.callback = self._on_input
        self.add_item(by_id)

        if recent:
            options = [
                discord.SelectOption(
                    label=(name or code)[:100], value=code, description=f"店舗ID {code}"
                )
                for code, name in recent[:25]
            ]
            sel = discord.ui.Select(
                placeholder="最近よく使われているお店から選ぶ", options=options, row=1
            )
            sel.callback = self._on_recent
            self.add_item(sel)
            self._recent_select = sel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def _on_search(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoreSearchModal(self.purpose))

    async def _on_input(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoreIdModal(self.purpose))

    async def _on_recent(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_menu(interaction, self._recent_select.values[0], self.purpose)


async def start_store_select(interaction: discord.Interaction, purpose: str) -> None:
    await user_repo.get_or_create(interaction.user.id)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(StoreCache).order_by(StoreCache.hit_count.desc()).limit(25)
            )
        ).scalars().all()
        recent = [(r.store_id, r.store_name or "") for r in rows]

    title = "注文するお店を選んでください" if purpose == "order" else "注文コードを作るお店を選んでください"
    if store_index.available():
        desc = (
            "**店名の一部**を入れるだけで探せます。\n"
            "例）`南砂`　`所沢`　`AKIBA`　`イオン`\n\n"
            f"{E.INFO} 店舗IDがわかっている場合は「店舗IDで指定」からどうぞ。"
        )
    else:
        desc = (
            "**店舗IDで指定**してください（5桁の数字）。\n"
            "マクドナルド公式アプリや店頭のレシートで確認できます。\n\n"
            f"{E.INFO} 一度使ったお店は、次回から店名でも探せます。"
        )
    e = discord.Embed(title=f"{E.STORE} {title}", description=desc, color=embeds.GREEN)

    view = StoreSelectView(interaction.user.id, purpose, recent)
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)


# ============================================================
#  メニューを開く
# ============================================================

async def open_menu(interaction: discord.Interaction, store_id: str, purpose: str) -> None:
    if not store_id.isdigit():
        await interaction.followup.send(
            embed=embeds.error("店舗IDは数字で入力してください。"), ephemeral=True
        )
        return

    handle = None
    try:
        handle = await mcd_accounts.pick_account()
        info = await mcd_stores.resolve_store(handle.client, store_id)
        await mcd_stores.ensure_menu_fresh(handle.client, store_id)
    except McdError as e:
        await interaction.followup.send(
            embed=embeds.error(f"店舗の情報を取得できませんでした。\n{e}"), ephemeral=True
        )
        return
    finally:
        if handle:
            await handle.aclose()

    menu = await mcd_stores.load_menu(store_id)
    if not menu.products:
        await interaction.followup.send(
            embed=embeds.error("この店舗のメニューを取得できませんでした。"), ephemeral=True
        )
        return

    await save_cart(interaction.user.id, purpose=purpose, store_id=store_id, pickup=None, items=[])
    view = CartView(interaction.user.id, purpose, store_id, info.name, info.delivery_methods, menu)
    await interaction.followup.send(embed=await view.build_embed(), view=view, ephemeral=True)


# ============================================================
#  カート
# ============================================================

class CartView(discord.ui.View):
    def __init__(
        self, owner_id: int, purpose: str, store_id: str, store_name: str,
        supported: dict[str, bool], menu: ParsedMenu,
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose
        self.store_id = store_id
        self.store_name = store_name
        self.supported = supported
        self.menu = menu
        self.items: list[OrderItem] = []
        self.pickup: str | None = None
        self._build()

    # -- 見た目 -------------------------------------------------

    def total(self) -> int:
        return sum(i.amount for i in self.items)

    async def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"{E.CART} カート",
            color=embeds.GREEN,
        )
        e.add_field(
            name=f"{E.STORE} 店舗",
            value=f"{self.store_name or '—'}\n`{self.store_id}`",
            inline=True,
        )
        e.add_field(
            name=f"{E.PIN} 受取方法",
            value=PICKUP_LABEL.get(self.pickup or "", f"{E.WARN} 未選択"),
            inline=True,
        )
        if self.items:
            lines = []
            for idx, item in enumerate(self.items, 1):
                p = self.menu.products.get(item.product_code)
                name = p.name if p else item.product_code
                lines.append(f"**{idx}.** {name}　{embeds.yen(item.amount)}")
                for comp in item.components:
                    for leaf in comp.walk():
                        if leaf is comp:
                            continue
                        cp = self.menu.products.get(leaf.product_code)
                        if cp:
                            lines.append(f"　└ {cp.name}")
            e.add_field(name="ご注文", value="\n".join(lines)[:1024], inline=False)
            e.add_field(name=f"{E.YEN} 合計", value=f"**{embeds.yen(self.total())}**", inline=False)
        else:
            e.add_field(
                name="ご注文",
                value="まだ商品が入っていません。「商品を追加」から選んでください。",
                inline=False,
            )
        return e

    def _build(self) -> None:
        self.clear_items()

        add = discord.ui.Button(label="商品を追加", emoji=E.PLUS, style=discord.ButtonStyle.primary, row=0)
        add.callback = self._on_add
        self.add_item(add)

        if self.items:
            rm = discord.ui.Button(label="最後の商品を削除", emoji=E.MINUS, style=discord.ButtonStyle.secondary, row=0)
            rm.callback = self._on_remove
            self.add_item(rm)

        options = []
        for method, cfg in config.PICKUP_METHODS.items():
            if not cfg["enabled"]:
                continue
            if self.supported and not self.supported.get(method, False):
                continue
            options.append(
                discord.SelectOption(
                    label=cfg["label"], value=method, default=(method == self.pickup)
                )
            )
        if not options:
            options = [discord.SelectOption(label="テイクアウト", value="takeOut")]
        sel = discord.ui.Select(placeholder="受取方法を選んでください", options=options, row=1)
        sel.callback = self._on_pickup
        self.add_item(sel)
        self._pickup_select = sel

        ready = bool(self.items) and self.pickup is not None
        label = "注文を確定する" if self.purpose == "order" else "注文コードを作る"
        emo = E.OK if self.purpose == "order" else E.RECEIPT
        go = discord.ui.Button(label=label, emoji=emo, style=discord.ButtonStyle.success, disabled=not ready, row=2)
        go.callback = self._on_go
        self.add_item(go)

        cancel = discord.ui.Button(label="やめる", emoji=E.NG, style=discord.ButtonStyle.secondary, row=2)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await save_cart(
            self.owner_id, purpose=self.purpose, store_id=self.store_id,
            pickup=self.pickup, items=self.items,
        )
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    # -- 操作 ---------------------------------------------------

    async def show(self, interaction: discord.Interaction, note: str | None = None) -> None:
        """カート画面に戻る。商品を足したあとは必ずここを通す。"""
        self._build()
        embed = await self.build_embed()
        if note:
            embed.description = note
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        # 同じメッセージを書き換えて進む。
        # 別メッセージを出すと、商品を足してもカートの表示が古いまま残ってしまう。
        view = CategoryView(self)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if self.items:
            self.items.pop()
        await self.refresh(interaction)

    async def _on_pickup(self, interaction: discord.Interaction) -> None:
        self.pickup = self._pickup_select.values[0]
        await self.refresh(interaction)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await clear_cart(self.owner_id)
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info("やめました。"), view=None
        )

    async def _on_go(self, interaction: discord.Interaction) -> None:
        pickup = self.pickup or "takeOut"
        # 販売時間を確定直前に再確認する（カート投入後に時間帯を跨ぐことがある）
        minutes = now_minutes()
        unavailable = [
            self.menu.products[i.product_code].name
            for i in self.items
            if i.product_code in self.menu.products
            and not self.menu.products[i.product_code].is_orderable_at(minutes)
        ]
        if unavailable:
            await interaction.response.edit_message(
                embed=embeds.warn(
                    "販売時間が終了した商品が含まれています。\n"
                    + "\n".join(f"・{n}" for n in unavailable)
                    + "\n\nカートから外してもう一度お試しください。"
                ),
                view=self,
            )
            return

        try:
            hex_str = build_hex(self.store_id, self.items, pickup)
        except Exception as e:
            await interaction.response.edit_message(
                embed=embeds.error(f"注文コードを作成できませんでした。\n{e}"), view=None
            )
            return

        self.stop()
        await clear_cart(self.owner_id)

        if self.purpose == "hex":
            await self._show_hex(interaction, hex_str, pickup)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await flows.open_preview(interaction, hex_str)

    async def _show_hex(self, interaction: discord.Interaction, hex_str: str, pickup: str) -> None:
        lines = []
        for item in self.items:
            p = self.menu.products.get(item.product_code)
            lines.append(f"**{p.name if p else item.product_code}**　{embeds.yen(item.amount)}")
            for comp in item.components:
                for leaf in comp.walk():
                    if leaf is comp:
                        continue
                    cp = self.menu.products.get(leaf.product_code)
                    if cp:
                        lines.append(f"　└ {cp.name}")

        e = discord.Embed(title=f"{E.RECEIPT} 注文コードを作成しました", color=embeds.BLUE)
        e.add_field(name=f"{E.STORE} 店舗", value=f"{self.store_name}（`{self.store_id}`）", inline=True)
        e.add_field(name=f"{E.PIN} 受取方法", value=PICKUP_LABEL.get(pickup, pickup), inline=True)
        e.add_field(name=f"{E.CART} ご注文", value="\n".join(lines)[:1024], inline=False)
        e.add_field(name=f"{E.YEN} 合計", value=f"**{embeds.yen(self.total())}**", inline=False)
        e.set_footer(text="このコードでは決済は行われていません")

        view = discord.ui.View(timeout=config.VIEW_TIMEOUT)
        order_btn = discord.ui.Button(label="このまま注文する", emoji=E.BURGER, style=discord.ButtonStyle.success)

        async def _order(i: discord.Interaction) -> None:
            if i.user.id != self.owner_id:
                await i.response.send_message(
                    embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
                )
                return
            await i.response.defer(ephemeral=True, thinking=True)
            await flows.open_preview(i, hex_str)

        order_btn.callback = _order
        view.add_item(order_btn)

        # 長いコードはファイルにして渡す（メッセージの上限対策）
        if len(hex_str) > 1800:
            import io

            await interaction.response.edit_message(embed=e, view=view)
            await interaction.followup.send(
                content=f"{E.RECEIPT} 注文コード（ファイル）",
                file=discord.File(io.BytesIO(hex_str.encode()), filename="order_code.txt"),
                ephemeral=True,
            )
        else:
            e.add_field(name="注文コード（HEX）", value=f"```\n{hex_str}\n```", inline=False)
            await interaction.response.edit_message(embed=e, view=view)


# ============================================================
#  カテゴリ → 商品 → オプション
# ============================================================

class CategoryView(discord.ui.View):
    def __init__(self, cart: CartView) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        minutes = now_minutes()
        options = []
        for c in cart.menu.collections[:25]:
            count = len(
                [
                    p for p in cart.menu.visible_products(c.id, minutes)
                    if p.product_class in ("PRODUCT", "VALUE_MEAL")
                ]
            )
            if count == 0:
                continue
            options.append(
                discord.SelectOption(label=c.name[:100], value=c.id, description=f"{count} 品")
            )
        if not options:
            options = [discord.SelectOption(label="（今は選べる商品がありません）", value="_none")]
        sel = discord.ui.Select(placeholder="カテゴリ", options=options, row=0)
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

        back = discord.ui.Button(label="カートに戻る", emoji=E.CART,
                                 style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"{E.BURGER} カテゴリを選んでください",
            description=f"{E.STORE} {self.cart.store_name or self.cart.store_id}",
            color=embeds.GREEN,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.cart.show(interaction)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        cid = self._sel.values[0]
        if cid == "_none":
            await self.cart.show(interaction, note="いま注文できる商品がありません。")
            return
        view = ProductView(self.cart, cid, page=0)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class ProductView(discord.ui.View):
    """商品一覧。Discordのセレクトは25件までなのでページ送りする。"""

    def __init__(self, cart: CartView, collection_id: str, page: int) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.collection_id = collection_id
        self.page = page
        minutes = now_minutes()
        self.products = [
            p for p in cart.menu.visible_products(collection_id, minutes)
            if p.product_class in ("PRODUCT", "VALUE_MEAL")
        ]
        self._build()

    @property
    def pages(self) -> int:
        return max(1, (len(self.products) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _page_items(self) -> list[Product]:
        start = self.page * PAGE_SIZE
        return self.products[start:start + PAGE_SIZE]

    def build_embed(self) -> discord.Embed:
        name = next(
            (c.name for c in self.cart.menu.collections if c.id == self.collection_id), ""
        )
        return discord.Embed(
            title=f"{E.BURGER} {name}",
            description=f"{len(self.products)} 品（{self.page + 1}/{self.pages} ページ）",
            color=embeds.GREEN,
        )

    def _build(self) -> None:
        self.clear_items()
        pickup = self.cart.pickup or "takeOut"
        options = []
        for p in self._page_items():
            price = p.price_for(pickup)
            options.append(
                discord.SelectOption(
                    label=p.name[:100], value=p.code,
                    description=f"{embeds.yen(price)}"
                    + ("　セット" if p.product_class == "VALUE_MEAL" else ""),
                )
            )
        if not options:
            options = [discord.SelectOption(label="（商品がありません）", value="_none")]
        sel = discord.ui.Select(placeholder="商品を選んでください", options=options, row=0)
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

        if self.pages > 1:
            prev = discord.ui.Button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary,
                                     disabled=self.page == 0, row=1)
            prev.callback = self._on_prev
            self.add_item(prev)
            nxt = discord.ui.Button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary,
                                    disabled=self.page >= self.pages - 1, row=1)
            nxt.callback = self._on_next
            self.add_item(nxt)

        back = discord.ui.Button(label="カテゴリに戻る", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        view = CategoryView(self.cart)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        code = self._sel.values[0]
        if code == "_none":
            return
        product = self.cart.menu.products.get(code)
        if product is None:
            await interaction.response.edit_message(
                embed=embeds.error("商品が見つかりませんでした。"), view=None
            )
            return

        choices = product.slots_of("choices")
        if choices:
            view = OptionView(self.cart, product, choices)
            await interaction.response.edit_message(embed=view.build_embed(), view=view)
        else:
            await self._add_and_close(interaction, product, {})

    async def _add_and_close(
        self, interaction: discord.Interaction, product: Product, picks: dict[str, str]
    ) -> None:
        await _add_to_cart(self.cart, product, picks)
        self.stop()
        await self.cart.show(interaction, note=f"{E.OK} **{product.name}** を追加しました。")


class OptionView(discord.ui.View):
    """セットのサイド・ドリンクなどを選ぶ。"""

    def __init__(self, cart: CartView, product: Product, choices: list) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.product = product
        self.choices = choices
        self.picks: dict[str, str] = {
            c.code: c.default_product for c in choices if c.default_product
        }
        self._build()

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"{E.BURGER} {self.product.name}",
            description="内容を選んでください。",
            color=embeds.GREEN,
        )
        for c in self.choices:
            chosen = self.picks.get(c.code)
            p = self.cart.menu.products.get(chosen or "")
            e.add_field(
                name=f"選択枠 {c.code}",
                value=(p.name if p else "未選択") + (f"（+{embeds.yen(c.extra_price)}）" if c.extra_price else ""),
                inline=True,
            )
        e.add_field(
            name=f"{E.YEN} 価格",
            value=f"**{embeds.yen(self.product.price_for(self.cart.pickup or 'takeOut'))}**",
            inline=False,
        )
        return e

    def _build(self) -> None:
        self.clear_items()
        for idx, c in enumerate(self.choices[:3]):
            candidates = self._candidates(c)
            if not candidates:
                continue
            options = [
                discord.SelectOption(
                    label=p.name[:100], value=p.code,
                    default=(self.picks.get(c.code) == p.code),
                )
                for p in candidates[:25]
            ]
            sel = discord.ui.Select(placeholder=f"選択枠 {idx + 1}", options=options, row=idx)
            sel.callback = self._make_cb(c.code, sel)
            self.add_item(sel)

        ok = discord.ui.Button(label="カートに追加", emoji=E.PLUS, style=discord.ButtonStyle.success, row=3)
        ok.callback = self._on_ok
        self.add_item(ok)
        back = discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self._on_back
        self.add_item(back)

    def _candidates(self, slot) -> list[Product]:
        """
        選択枠に入れられる商品。

        カタログは枠の中身を直接持っていないため、既定商品のサイズ違いを候補にする。
        """
        base = slot.default_product or slot.reference_product
        if not base:
            return []
        menu = self.cart.menu
        out = []
        # サイズ違いは「名前の末尾がサイズ表記」という規則で探す
        base_p = menu.products.get(base)
        if base_p is None:
            return []
        stem = base_p.name.rsplit(" ", 1)[0]
        for p in menu.products.values():
            if p.name == base_p.name or p.name.rsplit(" ", 1)[0] == stem:
                out.append(p)
        if base_p not in out:
            out.insert(0, base_p)
        return out or [base_p]

    def _make_cb(self, slot_code: str, sel: discord.ui.Select):
        async def cb(interaction: discord.Interaction) -> None:
            self.picks[slot_code] = sel.values[0]
            self._build()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        return cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    async def _on_back(self, interaction: discord.Interaction) -> None:
        view = CategoryView(self.cart)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_ok(self, interaction: discord.Interaction) -> None:
        await _add_to_cart(self.cart, self.product, self.picks)
        self.stop()
        await self.cart.show(
            interaction, note=f"{E.OK} **{self.product.name}** を追加しました。"
        )


async def _add_to_cart(cart: "CartView", product: Product, picks: dict[str, str]) -> None:
    """カートに1品足して保存する。追加の経路はここに一本化する。"""
    cart.items.append(build_order_item(cart, product, picks))
    await save_cart(
        cart.owner_id, purpose=cart.purpose, store_id=cart.store_id,
        pickup=cart.pickup, items=cart.items,
    )


def build_order_item(cart: CartView, product: Product, picks: dict[str, str]) -> OrderItem:
    """
    カタログの構造から注文の1品を組み立てる。

    構成品（composition）は固定、選択枠（choices）は選んだ商品を入れる。
    """
    pickup = cart.pickup or "takeOut"
    components: list[OrderItem] = []

    for slot in product.slots_of("composition"):
        if slot.code:
            components.append(OrderItem(product_code=slot.code, quantity=1))

    for slot in product.slots_of("choices"):
        chosen = picks.get(slot.code) or slot.default_product
        inner = [OrderItem(product_code=chosen, quantity=1)] if chosen else []
        components.append(
            OrderItem(product_code=slot.code, quantity=1, components=inner)
        )

    return OrderItem(
        product_code=product.code,
        quantity=1,
        amount=product.price_for(pickup),
        components=components,
    )
