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
import asyncio
import logging
from datetime import datetime

import discord
from sqlalchemy import select

import config
import emoji as E
from core import users as user_repo
from db.models import Cart, StoreCache, utcnow
from db.session import session_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import store_index
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError
from services.mcd import availability, slot_bridge
from services.mcd.menu import (
    ParsedMenu, Product, customization_note, minutes_of,
)
from services.mcd.protocol import PICKUP_LABEL, OrderItem, build_hex
from ui import embeds, flows

log = logging.getLogger("bot.menu_flows")

PAGE_SIZE = 25


def now_minutes() -> int:
    """
    いまが0時から何分か（日本時間）。

    提供時間帯は日本時間で定義されているため、サーバーのタイムゾーン
    設定に頼らず、必ず日本時間で数える（config.now_jst）。
    """
    return minutes_of(config.now_jst())


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
            # ひらがなのままだと当たらない（店名は漢字で登録されている）ため、
            # そこを先に案内する。実際に一番多い外れ方。
            "次のようにお試しください。\n"
            "・**漢字**で入力する（例:「まえばし」→「前橋」）\n"
            "・店名の一部だけ入れる（例:「南砂」「所沢」「イオン」）\n"
            "・市区町村や都道府県で探す（例:「江東区」「群馬県」）\n"
            "・店舗IDが分かる場合はIDで指定する"
            if store_index.available()
            else "店舗IDを直接入力してください。"
        )
        await interaction.followup.send(
            embed=embeds.warn(
                f"「{query}」に一致するお店が見つかりませんでした。\n{hint}"
            ),
            view=StoreSelectView(interaction.user.id, purpose),
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
    def __init__(self, owner_id: int, purpose: str) -> None:
        """
        お店の選び方を出す。

        以前は「最近よく使われているお店」のプルダウンも出していたが、
        他の利用者がどこで注文したかが伝わってしまうため取りやめた。
        （プライバシー重視の方針・docs/05 §0）
        """
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

async def start_store_select(interaction: discord.Interaction, purpose: str) -> None:
    await user_repo.get_or_create(interaction.user.id)
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

    view = StoreSelectView(interaction.user.id, purpose)
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

    # 注文できない店舗は、商品を選ばせる前にここで止める。
    # 選び終えてから断られるのが一番つらいので、理由まで説明する。
    av = await availability.check_store(store_id)
    if not av.orderable:
        await interaction.followup.send(
            embed=embeds.store_unavailable(info.name, store_id, av),
            view=StoreSelectView(interaction.user.id, purpose),
            ephemeral=True,
        )
        return

    menu = await mcd_stores.load_menu(store_id)
    if not menu.products:
        await interaction.followup.send(
            embed=embeds.error("この店舗のメニューを取得できませんでした。"), ephemeral=True
        )
        return

    # 利用者が商品を選んでいる間に、裏で注文の下ごしらえをしておく。
    # 確定ボタンを押したときの待ちが短くなる。失敗しても影響しない。
    if purpose == "order":
        asyncio.create_task(mcd_accounts.warm_up(store_id, info.group))

    await save_cart(interaction.user.id, purpose=purpose, store_id=store_id, pickup=None, items=[])
    # いまの時間に使える受取方法だけを選択肢に出す
    usable = {m: (m in av.methods) for m in info.delivery_methods}
    view = CartView(
        interaction.user.id, purpose, store_id, info.name,
        usable or info.delivery_methods, menu,
    )
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
                note = customization_note(self.menu, item)
                lines.append(f"**{idx}.** {name}{note}　{embeds.yen(item.amount)}")
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

            # 直前に入れた商品が具材を変えられるなら、ここから入れるようにする。
            # わざわざ画面を挟まず、必要な人だけが押せばよい。
            last = self.menu.products.get(self.items[-1].product_code)
            if last is not None and last.customizations():
                cz = discord.ui.Button(
                    label=f"{last.name[:20]}の具材を変える", emoji=E.NOTE,
                    style=discord.ButtonStyle.secondary, row=0,
                )
                cz.callback = self._on_customize_last
                self.add_item(cz)

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

    async def _on_customize_last(self, interaction: discord.Interaction) -> None:
        """
        直前に入れた商品の具材を変える。

        いったん取り出して、調整してから入れ直す。
        """
        if not self.items:
            await interaction.response.defer()
            return
        item = self.items[-1]
        product = self.menu.products.get(item.product_code)
        if product is None:
            await interaction.response.defer()
            return

        # 選択枠の内容を引き継ぐ（サイドやドリンクを選び直さなくて済むように）
        picks: dict[str, str] = {}
        for comp in item.components:
            inner = comp.components
            while inner and inner[0].components:
                inner = inner[0].components
            if inner:
                picks[comp.product_code] = inner[0].product_code

        self.items.pop()
        await save_cart(
            self.owner_id, purpose=self.purpose, store_id=self.store_id,
            pickup=self.pickup, items=self.items,
        )
        view = CustomizeView(self, product, picks)
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

    def _unavailable_items(self, minutes: int) -> list[Product]:
        """
        いま取り扱いの無い商品を、セットの中身まで辿って集める。

        同じ商品が複数の枠に入っていても一度だけ返す。
        """
        seen: set[str] = set()
        out: list[Product] = []

        def walk(item) -> None:
            code = str(getattr(item, "product_code", "") or "")
            if code and code not in seen:
                seen.add(code)
                p = self.menu.products.get(code)
                if p is not None and not p.is_orderable_at(minutes):
                    out.append(p)
            for child in getattr(item, "components", None) or []:
                walk(child)

        for item in self.items:
            walk(item)
        return out

    async def _on_go(self, interaction: discord.Interaction) -> None:
        pickup = self.pickup or "takeOut"
        # 販売時間を確定直前に再確認する（カート投入後に時間帯を跨ぐことがある）
        #
        # ⚠️ セットの中身まで見ること。セット自体は売っていても、
        #    中のサイドやドリンクが時間外ということがある。
        #    （朝の時間にマックフライポテトを入れた場合など）
        #    見落とすとマクドナルド側から
        #    「ただいまのお時間は選択した商品のお取り扱いがありません」
        #    で弾かれ、利用者には原因が分からない。
        minutes = now_minutes()
        unavailable = [p.name for p in self._unavailable_items(minutes)]
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
            note = customization_note(self.menu, item)
            lines.append(
                f"**{p.name if p else item.product_code}**{note}　{embeds.yen(item.amount)}"
            )
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
        """
        カートに入れる。

        ⚠️ 具材を調整できる商品でも、その画面を**勝手に挟まない**。
           ほとんどの人はそのまま注文するので、全員に1画面増やすのは
           かえって不親切。調整したい人だけが別のボタンから入る。
        """
        await _add_to_cart(self.cart, product, picks)
        self.stop()
        note = f"{E.OK} **{product.name}** を追加しました。"
        if product.customizations():
            names = "・".join(s.name for s in product.customizations()[:3])
            note += f"\n{E.INFO} {names}などを抜くこともできます（「具材を変える」から）"
        await self.cart.show(interaction, note=note)

    async def _customize_and_close(
        self, interaction: discord.Interaction, product: Product, picks: dict[str, str]
    ) -> None:
        """具材を調整してから入れたい人だけが通る道。"""
        view = CustomizeView(self.cart, product, picks)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


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

    def _slot_label(self, slot, candidates: list[Product]) -> str:
        """選択枠の見出し。中身から「ドリンク」「サイド」を言い当てる。"""
        base = slot.default_product or slot.reference_product
        col = self.cart.menu.collection_of(base) if base else None
        return col.name if col else "お選びください"

    def _build(self) -> None:
        self.clear_items()
        row = 0
        for c in self.choices[:2]:
            candidates = self._candidates(c)
            if not candidates:
                continue
            label = self._slot_label(c, candidates)
            options = [
                discord.SelectOption(
                    label=p.name[:100], value=p.code,
                    default=(self.picks.get(c.code) == p.code),
                )
                for p in candidates[:25]
            ]
            sel = discord.ui.Select(placeholder=label, options=options, row=row)
            sel.callback = self._make_cb(c.code, sel)
            self.add_item(sel)
            row += 1

            # サイズを選べる商品なら、その下にサイズも出す
            sizes = self._sizes(c)
            if len(sizes) > 1 and row < 3:
                size_options = [
                    discord.SelectOption(
                        label=p.name[:100], value=p.code,
                        default=(self.picks.get(c.code) == p.code),
                    )
                    for p in sizes[:25]
                ]
                ssel = discord.ui.Select(
                    placeholder=f"{label}のサイズ", options=size_options, row=row
                )
                ssel.callback = self._make_cb(c.code, ssel)
                self.add_item(ssel)
                row += 1

        ok = discord.ui.Button(label="カートに追加", emoji=E.PLUS, style=discord.ButtonStyle.success, row=3)
        ok.callback = self._on_ok
        self.add_item(ok)
        if self.product.customizations():
            cz = discord.ui.Button(
                label="具材を変える", emoji=E.NOTE,
                style=discord.ButtonStyle.secondary, row=3,
            )
            cz.callback = self._on_customize
            self.add_item(cz)
        back = discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, row=3)
        back.callback = self._on_back
        self.add_item(back)

    def _candidates(self, slot) -> list[Product]:
        """
        選択枠に入れられる商品。

        以前は「既定商品のサイズ違い」だけを候補にしていたため、
        ドリンクがコカ・コーラしか選べなかった。
        いまは参照商品の属するカテゴリ全体から、
        **その時刻に取り扱いのあるものだけ**を出す。
        """
        return self.cart.menu.choice_candidates(slot, now_minutes())

    def _sizes(self, slot) -> list[Product]:
        """いま選んでいる商品のサイズ違い。無ければ空。"""
        chosen = self.picks.get(slot.code)
        if not chosen:
            return []
        minutes = now_minutes()
        return [
            p for p in self.cart.menu.size_variants(chosen)
            if p.is_orderable_at(minutes)
        ]

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

    async def _on_customize(self, interaction: discord.Interaction) -> None:
        """具材を調整してから入れる。"""
        view = CustomizeView(self.cart, self.product, self.picks)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class CustomizeView(discord.ui.View):
    """
    具材の増減を選ぶ（ピクルス抜き・氷抜きなど）。

    外せる具材は最初から全部入れた状態で出し、**外したいものだけを
    選択から外してもらう**。ふだんの注文は何もせず「この内容で追加」を
    押すだけで済む。
    """

    def __init__(
        self, cart: "CartView", product: Product, picks: dict[str, str]
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.product = product
        self.picks = picks
        self.slots = product.customizations()
        # 最初は全部、既定の数量のまま
        self.amounts: dict[str, int] = {
            slot.code: slot.default_quantity for slot in self.slots
        }
        self._build()

    # -- 表示 --

    def build_embed(self) -> discord.Embed:
        removed = [s.name for s in self.slots if self.amounts.get(s.code, 1) == 0]
        added = [
            f"{s.name}×{self.amounts[s.code]}"
            for s in self.slots
            if self.amounts.get(s.code, 0) > s.default_quantity
        ]
        e = discord.Embed(
            title=f"{E.BURGER} {self.product.name}",
            description=(
                "具材を調整できます。**そのままでよければ**下の"
                "「この内容で追加」を押してください。"
            ),
            color=embeds.GREEN,
        )
        e.add_field(
            name=f"{E.MINUS} 抜くもの",
            value=("・" + "\n・".join(removed)) if removed else "なし",
            inline=True,
        )
        if any(s.increasable for s in self.slots):
            e.add_field(
                name=f"{E.PLUS} 増やすもの",
                value=("・" + "\n・".join(added)) if added else "なし",
                inline=True,
            )
        e.set_footer(text="お店の都合で、ご希望に添えない場合があります")
        return e

    # -- 組み立て --

    def _build(self) -> None:
        self.clear_items()
        row = 0

        removable = [s for s in self.slots if s.removable]
        if removable:
            options = [
                discord.SelectOption(
                    label=s.name[:100], value=s.code,
                    description="外すと「抜き」になります",
                    default=self.amounts.get(s.code, 1) > 0,
                )
                for s in removable[:25]
            ]
            sel = discord.ui.Select(
                placeholder="入れるものを選んでください（外すと「抜き」）",
                options=options, row=row,
                min_values=0, max_values=len(options),
            )
            sel.callback = self._on_keep
            self.add_item(sel)
            self._keep_select = sel
            row += 1

        increasable = [s for s in self.slots if s.increasable]
        if increasable and row < 4:
            options = []
            for s in increasable[:8]:
                for q in range(s.default_quantity + 1, s.max_quantity + 1):
                    options.append(
                        discord.SelectOption(
                            label=f"{s.name} ×{q}"[:100],
                            value=f"{s.code}:{q}",
                            default=self.amounts.get(s.code) == q,
                        )
                    )
            if options:
                sel = discord.ui.Select(
                    placeholder="増やすものを選んでください（任意）",
                    options=options[:25], row=row,
                    min_values=0, max_values=min(len(options[:25]), len(increasable)),
                )
                sel.callback = self._on_increase
                self.add_item(sel)
                self._inc_select = sel
                row += 1

        ok = discord.ui.Button(
            label="この内容で追加", emoji=E.CART,
            style=discord.ButtonStyle.success, row=4,
        )
        ok.callback = self._on_ok
        self.add_item(ok)
        back = discord.ui.Button(label="戻る", style=discord.ButtonStyle.secondary, row=4)
        back.callback = self._on_back
        self.add_item(back)

    # -- 操作 --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.cart.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def _on_keep(self, interaction: discord.Interaction) -> None:
        keep = set(self._keep_select.values)
        for slot in self.slots:
            if slot.removable:
                self.amounts[slot.code] = (
                    slot.default_quantity if slot.code in keep else slot.min_quantity
                )
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_increase(self, interaction: discord.Interaction) -> None:
        chosen = {}
        for v in self._inc_select.values:
            code, _, q = v.partition(":")
            chosen[code] = int(q)
        for slot in self.slots:
            if slot.increasable:
                # 抜いているものは増やさない（選び直しの取り消しを防ぐ）
                if self.amounts.get(slot.code, 1) == 0:
                    continue
                self.amounts[slot.code] = chosen.get(slot.code, slot.default_quantity)
        self._build()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        view = CategoryView(self.cart)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_ok(self, interaction: discord.Interaction) -> None:
        await _add_to_cart(self.cart, self.product, self.picks, self.amounts)
        self.stop()
        note = f"{E.OK} **{self.product.name}** を追加しました。"
        removed = [s.name for s in self.slots if self.amounts.get(s.code, 1) == 0]
        if removed:
            note += f"（{'・'.join(removed)}抜き）"
        await self.cart.show(interaction, note=note)


async def _add_to_cart(
    cart: "CartView",
    product: Product,
    picks: dict[str, str],
    amounts: dict[str, int] | None = None,
) -> None:
    """カートに1品足して保存する。追加の経路はここに一本化する。"""
    cart.items.append(build_order_item(cart, product, picks, amounts))
    await save_cart(
        cart.owner_id, purpose=cart.purpose, store_id=cart.store_id,
        pickup=cart.pickup, items=cart.items,
    )


def build_order_item(
    cart: CartView,
    product: Product,
    picks: dict[str, str],
    amounts: dict[str, int] | None = None,
) -> OrderItem:
    """
    カタログの構造から注文の1品を組み立てる。

    構成品（composition）は固定、選択枠（choices）は選んだ商品を入れる。

    amounts は具材の増減（{"99901032": 0} で「ピクルス抜き」）。
    指定が無い具材は既定の数量のままにする。
    """
    pickup = cart.pickup or "takeOut"
    amounts = amounts or {}
    components: list[OrderItem] = []

    # ---- 具材（composition）----
    # ⚠️ **既定のままの具材は送らない。**
    #    実物の注文コードを調べたところ、セットの中のバーガーも、
    #    ドリンクの氷も、何も指定していない具材は一切入っていなかった。
    #    既定の内容は相手が分かっているので、送るのは
    #    「抜いた」「増やした」ものだけでよい。
    #    全部送ると相手が受け付けないことがある。
    for slot in product.slots_of("composition"):
        if not slot.code:
            continue
        qty = amounts.get(slot.code, slot.default_quantity)
        # カタログが許す範囲に収める（不正な数量を送らない）
        qty = max(slot.min_quantity, min(qty, slot.max_quantity))
        if qty == slot.default_quantity:
            continue        # 既定のまま＝送らない
        components.append(OrderItem(product_code=slot.code, quantity=qty))

    # ---- 選択枠（choices）----
    # 枠によっては、枠と商品の間にもう1段ある。
    #   サイド枠    9987009 → 2020
    #   ドリンク枠  9997918 → 9997914 → 3120
    # この中間はカタログに載っていないため、分かっているものを
    # services/mcd/slot_bridge.py に持たせてある（docs/09 §2）。
    for slot in product.slots_of("choices"):
        chosen = picks.get(slot.code) or slot.default_product
        if not chosen:
            continue
        leaf = OrderItem(product_code=chosen, quantity=1)
        bridge = slot_bridge.bridge_for(slot.code)
        if bridge:
            leaf = OrderItem(product_code=bridge, quantity=1, components=[leaf])
        components.append(
            OrderItem(product_code=slot.code, quantity=1, components=[leaf])
        )

    return OrderItem(
        product_code=product.code,
        quantity=1,
        amount=product.price_for(pickup),
        components=components,
    )
