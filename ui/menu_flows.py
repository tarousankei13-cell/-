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
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
from sqlalchemy import select

import config
import emoji as E
from core import users as user_repo
from db.models import User, Cart, StoreCache, as_utc, utcnow
from db.session import session_scope
from services.mcd import accounts as mcd_accounts
from services.mcd import store_index
from services.mcd import stores as mcd_stores
from services.mcd.client import McdError
from services.mcd import availability, slot_rules
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

@dataclass
class SavedCart:
    """組み立て途中のカート。"続きから" で拾い直すために使う。"""
    purpose: str
    store_id: str
    pickup: str | None
    items: list[OrderItem]
    age_minutes: float

    @property
    def resumable(self) -> bool:
        """拾い直せるか。中身・お店・新しさの3つがそろって初めて拾える。"""
        return (
            bool(self.items)
            and bool(self.store_id)
            and self.age_minutes <= config.CART_RESUME_MINUTES
        )


async def load_cart(discord_id: int) -> SavedCart | None:
    """
    保存してあるカートを読み出す。無ければ None。

    ⚠️ 保存だけして読み出していなかった時期がある。ビューが
       タイムアウト（VIEW_TIMEOUT秒）するとカートに手が届かなくなり、
       次に注文を始めた時点で空で上書きされて消えていた。
    """
    async with session_scope() as s:
        row = await s.get(Cart, discord_id)
        if row is None:
            return None
        try:
            items = [OrderItem.from_dict(d) for d in json.loads(row.items_json or "[]")]
        except (ValueError, KeyError, TypeError):
            log.warning("カートを読めませんでした（discord_id=%s）", discord_id)
            return None

        # ⚠️ SQLite から読んだ日時は naive なので as_utc を通す。
        #    そのまま引き算すると offset-naive と offset-aware で落ちる。
        updated = as_utc(row.updated_at)
        age = (
            (datetime.now(timezone.utc) - updated).total_seconds() / 60
            if updated else float("inf")
        )
        return SavedCart(
            purpose=row.purpose or "order",
            store_id=row.store_id or "",
            pickup=row.pickup_method,
            items=items,
            age_minutes=max(0.0, age),
        )


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
    """
    お店の選び方。

    前回と同じお店で頼む人が多いので、覚えてある場合は
    **1回押すだけ**で済むようにしてある。
    """
    def __init__(
        self, owner_id: int, purpose: str,
        last_store: tuple[str, str] | None = None,
    ) -> None:
        """
        お店の選び方を出す。

        以前は「最近よく使われているお店」のプルダウンも出していたが、
        他の利用者がどこで注文したかが伝わってしまうため取りやめた。
        （プライバシー重視の方針・docs/05 §0）
        代わりに、**その人自身の**前回のお店だけを出す。
        """
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose
        self.last_store = last_store

        # 前回と同じお店なら、探さずに1回押すだけで進める
        if last_store:
            store_id, store_name = last_store
            again = discord.ui.Button(
                label=f"前回のお店（{store_name[:24]}）", emoji=E.REPEAT,
                style=discord.ButtonStyle.success, row=0,
            )
            again.callback = self._on_last
            self.add_item(again)

        search = discord.ui.Button(
            label="店名でさがす", emoji="🔍",
            style=discord.ButtonStyle.primary if not last_store
            else discord.ButtonStyle.secondary,
            row=0 if not last_store else 1,
        )
        search.callback = self._on_search
        self.add_item(search)

        by_id = discord.ui.Button(
            label="店舗IDで指定", emoji="🔢", style=discord.ButtonStyle.secondary,
            row=0 if not last_store else 1,
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

    async def _on_last(self, interaction: discord.Interaction) -> None:
        """前回のお店へそのまま進む。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_menu(interaction, self.last_store[0], self.purpose)

    async def _on_search(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoreSearchModal(self.purpose))

    async def _on_input(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoreIdModal(self.purpose))

class ResumeCartView(discord.ui.View):
    """
    途中まで作ったカートを拾い直すか聞く。

    少し目を離しただけで最初からやり直しになるのは不親切なので、
    `config.CART_RESUME_MINUTES` 分以内なら続きから始められるようにする。
    """

    def __init__(self, owner_id: int, purpose: str, saved: SavedCart) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose
        self.saved = saved
        self._build()

    async def build_embed(self) -> discord.Embed:
        store_name = await _store_name_of(self.saved.store_id)
        left = max(0, int(config.CART_RESUME_MINUTES - self.saved.age_minutes))
        e = discord.Embed(
            title=f"{E.CART} 途中のご注文があります",
            description=(
                f"**{store_name}** で組み立てた内容が残っています。\n"
                "続きから進められます。"
            ),
            color=embeds.GREEN,
        )
        e.add_field(name=f"{E.CART} 商品数", value=f"{len(self.saved.items)} 点", inline=True)
        e.add_field(
            name=f"{E.LOADING} 残り時間",
            value=f"あと約 {left} 分" if left else "まもなく消えます",
            inline=True,
        )
        e.set_footer(text="値段や販売時間は、続きを開いたときに取り直します")
        return e

    def _build(self) -> None:
        self.clear_items()
        go = discord.ui.Button(
            label="続きから", emoji=E.REPEAT, style=discord.ButtonStyle.success, row=0
        )
        go.callback = self._on_resume
        self.add_item(go)

        fresh = discord.ui.Button(
            label="最初から選びなおす", style=discord.ButtonStyle.secondary, row=0
        )
        fresh.callback = self._on_fresh
        self.add_item(fresh)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=embeds.error("この操作は開いた本人のみ行えます。"), ephemeral=True
            )
            return False
        return True

    async def _on_resume(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_menu(
            interaction, self.saved.store_id, self.purpose, resume=self.saved
        )

    async def _on_fresh(self, interaction: discord.Interaction) -> None:
        self.stop()
        await clear_cart(self.owner_id)
        await interaction.response.defer(ephemeral=True)
        await start_store_select(interaction, self.purpose)


async def _store_name_of(store_id: str) -> str:
    """キャッシュしてある店名。無ければ店舗IDをそのまま返す。"""
    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
    return (row.store_name if row and row.store_name else store_id) or store_id


async def start_store_select(interaction: discord.Interaction, purpose: str) -> None:
    await user_repo.get_or_create(interaction.user.id)

    # 途中まで組み立てたカートが残っていれば、作り直さずに済ませる。
    # ビューは VIEW_TIMEOUT 秒で反応しなくなるが、中身は残っている。
    saved = await load_cart(interaction.user.id)
    if saved is not None and saved.resumable and saved.purpose == purpose:
        view = ResumeCartView(interaction.user.id, purpose, saved)
        e = await view.build_embed()
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, view=view, ephemeral=True)
        return

    # その人自身の前回のお店を覚えていれば、1回押すだけで進めるようにする
    last_store = None
    async with session_scope() as s:
        row = await s.get(User, interaction.user.id)
        if row and row.last_store_id:
            last_store = (row.last_store_id, row.last_store_name or row.last_store_id)

    title = "注文するお店を選んでください" if purpose == "order" else "注文コードを作るお店を選んでください"
    if last_store:
        desc = (
            f"{E.REPEAT} **前回のお店**（{last_store[1]}）なら、"
            "一番上のボタンを押すだけです。\n\n"
            "別のお店にする場合は「店名でさがす」から**店名の一部**を入れてください。"
        )
    elif store_index.available():
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

    view = StoreSelectView(interaction.user.id, purpose, last_store)
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=view, ephemeral=True)


# ============================================================
#  メニューを開く
# ============================================================

async def open_menu(
    interaction: discord.Interaction, store_id: str, purpose: str,
    resume: "SavedCart | None" = None, *, reorder: bool = False,
) -> None:
    """
    お店のメニューを開く。

    resume を渡すと、保存してあったカートの中身を引き継ぐ。
    渡さなければ空のカートから始める（既存のカートは捨てる）。

    reorder=True は「過去の注文と同じ内容を組み直す」場合。
    やることは resume と同じ（取り直したメニューで作り直す）ので、
    **文面だけ**変える。処理を分けると、終売や時間帯の確認を
    片方だけ直し忘れる。
    """
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
        # ⚠️ 行き止まりにしない。近くの注文できる店舗まで出す。
        #    「別の店舗をお選びください」とだけ言われても、
        #    利用者はどこを選べばよいか分からない。
        e = embeds.store_unavailable(info.name, store_id, av)
        near = nearby_lines(store_id)
        if near:
            e.add_field(
                name=f"{E.PIN} 近くでご注文いただける店舗",
                value="\n".join(near),
                inline=False,
            )
        await interaction.followup.send(
            embed=e,
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

    # ---- 続きから始める場合は、残っていた中身を引き継ぐ ----
    # ⚠️ 15分の間に終売・時間帯外・メニュー改定が起きることがある。
    #    いま取り直したメニューに無い商品は、黙って持ち越さない。
    items: list[OrderItem] = []
    dropped = 0
    if resume is not None:
        for item in resume.items:
            if str(item.product_code) in menu.products:
                items.append(item)
            else:
                dropped += 1
        # 値段は取り直したメニューで入れ直す（15分の間に変わりうる）
        reprice(menu, items, "takeOut")

    await save_cart(
        interaction.user.id, purpose=purpose, store_id=store_id,
        pickup=None, items=items,
    )
    # いまの時間に使える受取方法だけを選択肢に出す
    usable = {m: (m in av.methods) for m in info.delivery_methods}
    # 前回と同じ受取方法を最初から選んでおく。
    # 毎回選び直すのは手間なので、違うときだけ変えてもらえばよい。
    last_pickup = None
    async with session_scope() as s:
        row = await s.get(User, interaction.user.id)
        if row and row.last_pickup and (usable or info.delivery_methods).get(
            row.last_pickup
        ):
            last_pickup = row.last_pickup

    view = CartView(
        interaction.user.id, purpose, store_id, info.name,
        usable or info.delivery_methods, menu,
        active_dayparts=await availability.active_dayparts_for(store_id),
        pickup=last_pickup,
        items=items,
    )
    embed = await view.build_embed()
    if resume is not None:
        if reorder:
            note = f"{E.REPEAT} 前回と同じ内容をご用意しました。"
            if not items:
                # ⚠️ 全部外れたときに「ご用意しました」と出してはいけない。
                #    カートは空なので、そう伝える。
                note = (
                    f"{E.WARN} 前回の商品は、いまお取り扱いがありませんでした。\n"
                    "お手数ですが、あらためてお選びください。"
                )
            elif dropped:
                note += (
                    f"\n{E.WARN} いまお取り扱いの無い商品を "
                    f"{dropped} 点だけ外しました。"
                )
            else:
                note += "\n内容をご確認のうえ、お進みください。"
        else:
            note = f"{E.REPEAT} 続きから始めます。"
            if dropped:
                note += (
                    f"\n{E.WARN} お取り扱いが終わった商品を {dropped} 点だけ外しました。"
                )
        embed.description = note
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ============================================================
#  カート
# ============================================================

class CartView(discord.ui.View):
    def __init__(
        self, owner_id: int, purpose: str, store_id: str, store_name: str,
        supported: dict[str, bool], menu: ParsedMenu,
        active_dayparts: set[str] | None = None,
        pickup: str | None = None,
        items: list[OrderItem] | None = None,
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.purpose = purpose
        self.store_id = store_id
        self.store_name = store_name
        self.supported = supported
        self.menu = menu
        # いま注文を受け付けている時間帯（朝マック・夜マックなど）。
        # 終わったカテゴリを出さないために使う。
        self.active_dayparts: set[str] = set(active_dayparts or ())
        # 「続きから」で開いたときは、残っていた中身を引き継ぐ
        self.items: list[OrderItem] = list(items or [])
        # カートの中で選んでいる行（group_items の添字）。未選択なら None
        self.selected: int | None = None
        # 前回と同じ受取方法を最初から選んでおく（手数を1つ減らす）
        self.pickup: str | None = pickup
        self._build()

    # -- 見た目 -------------------------------------------------

    def total(self, pickup: str | None = None) -> int:
        """合計。受取方法で税率が変わるので、どちらの合計かを指定する。"""
        method = pickup or self.pickup or "takeOut"
        return sum(price_of(self.menu, i, method) for i in self.items)

    def _total_text(self) -> str:
        """合計。受取方法で金額が違うときだけ、両方見せる。"""
        eat, take = self.total("eatIn"), self.total("takeOut")
        if eat == take:
            return f"**{embeds.yen(eat)}**"
        return (
            f"店内　　　**{embeds.yen(eat)}**\n"
            f"お持ち帰り**{embeds.yen(take)}**"
        )

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
        # いまだけ頼めるもの（朝マック・夜マックなど）を伝える。
        #   ⚠️ 「レギュラー」は一日の大半なので出さない。
        #      いつでも頼めるものを知らせても、選ぶ助けにならない。
        notable = availability.notable_labels(self.active_dayparts)
        if notable:
            e.add_field(
                name=f"{E.LOADING} いまの時間帯",
                value="／".join(f"**{n}**" for n in notable) + "\n"
                      "この時間だけのメニューがございます",
                inline=True,
            )
        e.add_field(
            name=f"{E.CART} 商品数",
            value=f"{len(self.items)} 点",
            inline=True,
        )
        if self.items:
            lines = cart_lines(self.menu, self.items)
            e.add_field(name="ご注文", value="\n".join(lines)[:1024], inline=False)
            e.add_field(name=f"{E.YEN} 小計", value=self._total_text(), inline=False)
            e.set_footer(text="お受け取り方法は、このあとお選びいただきます")
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
            groups = group_items(self.items)
            # 選んだ商品が消えていたら選択を外す（削除の直後など）
            if self.selected is not None and self.selected >= len(groups):
                self.selected = None

            options = []
            for i, g in enumerate(groups[:25]):
                prod = self.menu.products.get(g.item.product_code)
                name = prod.name if prod else g.item.product_code
                note = customization_note(self.menu, g.item)
                options.append(
                    discord.SelectOption(
                        label=f"{i + 1}. {name}{note}"[:100],
                        value=str(i),
                        description=(
                            f"{g.count} 個　"
                            f"{embeds.yen(price_of(self.menu, g.item, self.pickup or 'takeOut') * g.count)}"
                        )[:100],
                        default=(self.selected == i),
                    )
                )
            sel = discord.ui.Select(
                placeholder="変更する商品を選んでください（削除・個数）",
                options=options, row=1,
            )
            sel.callback = self._on_select
            self.add_item(sel)
            self._item_select = sel

            picked = self.selected is not None and self.selected < len(groups)
            g = groups[self.selected] if picked else None

            minus = discord.ui.Button(
                label="1個 減らす", emoji=E.MINUS,
                style=discord.ButtonStyle.secondary, row=2,
                disabled=not picked,
            )
            minus.callback = self._on_minus
            self.add_item(minus)

            plus = discord.ui.Button(
                label="1個 増やす", emoji=E.PLUS,
                style=discord.ButtonStyle.secondary, row=2,
                disabled=not picked,
            )
            plus.callback = self._on_plus
            self.add_item(plus)

            drop = discord.ui.Button(
                label="この商品を削除", emoji=E.NG,
                style=discord.ButtonStyle.danger, row=2,
                disabled=not picked,
            )
            drop.callback = self._on_drop
            self.add_item(drop)

            # 選んだ商品の具材を変える。選んでいないときは出さない。
            prod = self.menu.products.get(g.item.product_code) if g else None
            if prod is not None and prod.customizations():
                cz = discord.ui.Button(
                    label=f"{prod.name[:16]}の具材を変える", emoji=E.NOTE,
                    style=discord.ButtonStyle.secondary, row=3,
                )
                cz.callback = self._on_customize_selected
                self.add_item(cz)

        # ⚠️ 受取方法はここでは聞かない。
        #    中身が決まる前に受取方法を選ばせると、
        #    商品を足すたびに関係のない選択肢が目に入って迷う。
        #    実物のアプリと同じく、最後にまとめて聞く（PickupView）。
        label = "注文へ進む" if self.purpose == "order" else "注文コードを作る"
        emo = E.OK if self.purpose == "order" else E.RECEIPT
        go = discord.ui.Button(
            label=label, emoji=emo, style=discord.ButtonStyle.success,
            disabled=not self.items, row=2,
        )
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

    def popular_products(self, limit: int = 20) -> list[Product]:
        """
        すぐ選べるように出す商品。

        「おすすめ」と「セット」を先に、残りを人気の順で。
        カテゴリを選ばずに商品へ進めるようにするため。
        """
        minutes = now_minutes()
        seen: set[str] = set()
        out: list[Product] = []
        priority = ("おすすめ", "セット", "バーガー")
        ordered = sorted(
            self.menu.collections,
            key=lambda c: (
                priority.index(c.name) if c.name in priority else len(priority),
                c.sort_order,
            ),
        )
        for col in ordered:
            if not availability.collection_available(col.name, self.active_dayparts):
                continue
            for p in self.menu.visible_products(col.id, minutes):
                if p.code in seen or p.product_class not in ("PRODUCT", "VALUE_MEAL"):
                    continue
                seen.add(p.code)
                out.append(p)
                if len(out) >= limit:
                    return out
        return out

    async def _on_add(self, interaction: discord.Interaction) -> None:
        # 同じメッセージを書き換えて進む。
        # 別メッセージを出すと、商品を足してもカートの表示が古いまま残ってしまう。
        view = CategoryView(self)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    # -- カートの中身を編集する ---------------------------------

    def _selected_group(self) -> "CartGroup | None":
        groups = group_items(self.items)
        if self.selected is None or self.selected >= len(groups):
            return None
        return groups[self.selected]

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected = int(self._item_select.values[0])
        await self.refresh(interaction)

    async def _on_minus(self, interaction: discord.Interaction) -> None:
        """選んだ商品を1個だけ減らす。最後の1個なら、その行ごと消える。"""
        g = self._selected_group()
        if g is None:
            await interaction.response.defer()
            return
        del self.items[g.indexes[-1]]
        if g.count == 1:
            self.selected = None     # 行ごと消えたので選択を外す
        await self.refresh(interaction)

    async def _on_plus(self, interaction: discord.Interaction) -> None:
        """選んだ商品を1個増やす。具材の調整もそのまま複製する。"""
        g = self._selected_group()
        if g is None:
            await interaction.response.defer()
            return
        if len(self.items) >= config.CART_MAX_ITEMS:
            await interaction.response.send_message(
                embed=embeds.warn(
                    f"カートに入れられるのは {config.CART_MAX_ITEMS} 点までです。"
                ),
                ephemeral=True,
            )
            return
        # 同じ内容を作り直す。参照を共有すると、片方の調整が
        # もう片方にも効いてしまう。
        copy = OrderItem.from_dict(g.item.to_dict())
        self.items.insert(g.indexes[-1] + 1, copy)
        await self.refresh(interaction)

    async def _on_drop(self, interaction: discord.Interaction) -> None:
        """選んだ商品を、同じ内容のぶんまとめて削除する。"""
        g = self._selected_group()
        if g is None:
            await interaction.response.defer()
            return
        for i in sorted(g.indexes, reverse=True):
            del self.items[i]
        self.selected = None
        await self.refresh(interaction)

    async def _on_customize_selected(self, interaction: discord.Interaction) -> None:
        """
        選んだ商品の具材を変える。

        いったん取り出して、調整してから入れ直す。
        ⚠️ 同じ内容が複数あっても、取り出すのは**1個だけ**。
           まとめて消すと、調整していない残りまで巻き添えになる。
        """
        g = self._selected_group()
        if g is None:
            await interaction.response.defer()
            return
        item = g.item
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

        del self.items[g.indexes[-1]]
        self.selected = None
        await save_cart(
            self.owner_id, purpose=self.purpose, store_id=self.store_id,
            pickup=self.pickup, items=self.items,
        )
        view = CustomizeView(self, product, picks)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

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

    def _incomplete_items(self, minutes: int) -> list[str]:
        """
        必須の枠が埋まっていない商品の名前。

        空のまま送るとマクドナルドに断られ、利用者には理由が分からない。
        """
        out: list[str] = []
        for item in self.items:
            product = self.menu.products.get(str(item.product_code))
            if product is None:
                continue
            chosen = {slot for slot, _ in slot_rules.choices_of(item)}
            # ⚠️ 入れ子の枠（ポテナゲの中のナゲットのソース）も見ること。
            #    見落とすと、必須の枠が空のまま注文してしまう。
            slots = list(product.slots_of("choices")) + [
                c for _comp, c in self.menu.nested_choices(product)
            ]
            for c in slots:
                if c.min_quantity < 1:
                    continue
                if c.code in chosen or c.default_product:
                    continue
                out.append(product.name)
                break
        return out

    async def _on_go(self, interaction: discord.Interaction) -> None:
        """中身が決まったので、最後に受取方法を聞く。"""
        # 販売時間を確定直前に再確認する（カート投入後に時間帯を跨ぐことがある）
        #
        # ⚠️ セットの中身まで見ること。セット自体は売っていても、
        #    中のサイドやドリンクが時間外ということがある。
        #    （朝の時間にマックフライポテトを入れた場合など）
        #    見落とすとマクドナルド側から
        #    「ただいまのお時間は選択した商品のお取り扱いがありません」
        #    で弾かれ、利用者には原因が分からない。
        minutes = now_minutes()
        # ⚠️ カートを「続きから」で拾うと、15分前の内容がそのまま入っている。
        #    その間にメニューが変わって、埋まっていない枠が生まれることがある。
        broken = self._incomplete_items(minutes)
        if broken:
            await interaction.response.edit_message(
                embed=embeds.warn(
                    "お選びいただく内容がそろっていない商品があります。\n"
                    + "\n".join(f"・{n}" for n in broken)
                    + "\n\nカートから外して、選び直してください。"
                ),
                view=self,
            )
            return

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

        view = PickupView(self)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def finish(self, interaction: discord.Interaction, pickup: str) -> None:
        """受取方法が決まった。ここで注文コードを作って先へ進む。"""
        # 受取方法で税率が変わるので、金額を入れ直してから組み立てる
        reprice(self.menu, self.items, pickup)
        self.pickup = pickup

        try:
            hex_str = build_hex(self.store_id, self.items, pickup)
        except Exception as e:
            await interaction.response.edit_message(
                embed=embeds.error(f"注文コードを作成できませんでした。\n{e}"), view=None
            )
            return

        # 次も同じ受取方法を既定にできるよう覚えておく
        try:
            async with session_scope() as s:
                row = await s.get(User, self.owner_id)
                if row is not None:
                    row.last_pickup = pickup
        except Exception:
            log.exception("受取方法を覚えられませんでした")

        self.stop()
        await clear_cart(self.owner_id)

        if self.purpose == "hex":
            await self._show_hex(interaction, hex_str, pickup)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await flows.open_preview(interaction, hex_str)

    async def _show_hex(self, interaction: discord.Interaction, hex_str: str, pickup: str) -> None:
        lines = cart_lines(self.menu, self.items, pickup)

        e = discord.Embed(title=f"{E.RECEIPT} 注文コードを作成しました", color=embeds.BLUE)
        e.add_field(name=f"{E.STORE} 店舗", value=f"{self.store_name}（`{self.store_id}`）", inline=True)
        e.add_field(name=f"{E.PIN} 受取方法", value=PICKUP_LABEL.get(pickup, pickup), inline=True)
        e.add_field(name=f"{E.CART} ご注文", value="\n".join(lines)[:1024], inline=False)
        e.add_field(name=f"{E.YEN} 合計", value=f"**{embeds.yen(self.total(pickup))}**", inline=False)
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

class PickupView(discord.ui.View):
    """
    お受け取り方法を選ぶ。注文の最後の一歩。

    中身が決まってから聞くので、金額もここで確定できる。
    受取方法で金額が変わる場合は**ボタンに金額を出す**ので、
    押す前に差額が分かる。（実データでは店内とお持ち帰りは同額だった）
    """

    def __init__(self, cart: "CartView") -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self._build()

    def build_embed(self) -> discord.Embed:
        c = self.cart
        e = discord.Embed(
            title=f"{E.PIN} お受け取り方法をお選びください",
            description=f"{E.STORE} {c.store_name or '—'}（`{c.store_id}`）",
            color=embeds.GREEN,
        )
        lines = cart_lines(c.menu, c.items, "takeOut")
        e.add_field(name=f"{E.CART} ご注文", value="\n".join(lines)[:1024], inline=False)
        if c.total("eatIn") != c.total("takeOut"):
            e.set_footer(text="お受け取り方法によってお支払い額が変わります")
        return e

    def _methods(self) -> list[tuple[str, str]]:
        """いまこの店で使える受取方法（コード, 表示名）。"""
        out = []
        for method, cfg in config.PICKUP_METHODS.items():
            if not cfg["enabled"]:
                continue
            if self.cart.supported and not self.cart.supported.get(method, False):
                continue
            out.append((method, cfg["label"]))
        return out or [("takeOut", config.PICKUP_METHODS["takeOut"]["label"])]

    def _build(self) -> None:
        self.clear_items()
        methods = self._methods()
        both_differ = self.cart.total("eatIn") != self.cart.total("takeOut")

        for i, (method, label) in enumerate(methods[:4]):
            total = self.cart.total(method)
            text = f"{label}　{embeds.yen(total)}" if both_differ else label
            # 前回と同じ受取方法を目立たせる。毎回同じ人がほとんどなので、
            # どれを押せばいいか一目で分かるようにする。
            usual = self.cart.pickup or "takeOut"
            b = discord.ui.Button(
                label=text[:80],
                style=(
                    discord.ButtonStyle.success if method == usual
                    else discord.ButtonStyle.primary
                ),
                row=i // 2,
            )
            b.callback = self._make_pick(method)
            self.add_item(b)

        back = discord.ui.Button(
            label="カートに戻る", emoji=E.CART,
            style=discord.ButtonStyle.secondary, row=3,
        )
        back.callback = self._on_back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    def _make_pick(self, method: str):
        async def cb(interaction: discord.Interaction) -> None:
            self.stop()
            await self.cart.finish(interaction, method)
        return cb

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.cart.show(interaction)


def search_products(
    menu: ParsedMenu, query: str, minutes: int,
    active_dayparts: set[str] | None = None, limit: int = 25,
) -> list[Product]:
    """
    商品名で探す。

    ・ひらがな・カタカナ・全角半角・大文字小文字のどれでも当たるようにする
    ・いま注文できないものは出さない（出しても選べないので)
    ・前方一致を先に、部分一致をあとに並べる
    """
    q = store_index.normalize(query)
    if not q:
        return []

    starts: list[Product] = []
    contains: list[Product] = []
    for p in menu.products.values():
        if p.product_class not in ("PRODUCT", "VALUE_MEAL"):
            continue
        if not p.is_orderable_at(minutes):
            continue
        # 日本語名と英語名の両方で当てる。
        # カタログに "Big Mac" が入っているので「Big」でも引ける。
        names = [store_index.normalize(p.name)]
        if p.display.name_en:
            names.append(store_index.normalize(p.display.name_en))
        if any(n.startswith(q) for n in names):
            starts.append(p)
        elif any(q in n for n in names):
            contains.append(p)

    both = starts + contains
    return both[:limit]


class ProductSearchModal(discord.ui.Modal, title="商品名でさがす"):
    query = discord.ui.TextInput(
        label="商品名の一部",
        placeholder="例: ポテト　／　てりやき　／　コーヒー",
        required=True, max_length=40,
    )

    def __init__(self, cart: "CartView") -> None:
        super().__init__()
        self.cart = cart

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.query.value).strip()
        hits = search_products(
            self.cart.menu, text, now_minutes(), self.cart.active_dayparts
        )
        if not hits:
            await interaction.response.send_message(
                embed=embeds.warn(
                    f"「{text}」に当てはまる商品は見つかりませんでした。\n"
                    "いまの時間に注文できない商品は出てきません。\n"
                    "カテゴリからも探せます。",
                    title=f"{E.INFO} 見つかりませんでした",
                ),
                ephemeral=True,
            )
            return

        view = ProductSearchView(self.cart, text, hits)
        await interaction.response.edit_message(
            embed=view.build_embed(), view=view
        )


class ProductSearchView(discord.ui.View):
    """商品名の検索結果。選ぶと商品の詳細へ進む。"""

    def __init__(self, cart: "CartView", query: str, hits: list[Product]) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.query = query
        self.hits = hits
        self._build()

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"🔍 「{self.query}」の検索結果",
            description=f"{len(self.hits)} 品見つかりました。",
            color=embeds.GREEN,
        )

    def _build(self) -> None:
        self.clear_items()
        pickup = self.cart.pickup or "takeOut"
        sel = discord.ui.Select(
            placeholder="商品を選んでください",
            options=[
                discord.SelectOption(
                    label=p.name[:100], value=p.code,
                    description=(
                        embeds.yen(p.price_for(pickup))
                        + ("　セット" if p.product_class == "VALUE_MEAL" else "")
                    ),
                )
                for p in self.hits[:25]
            ],
            row=0,
        )
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

        again = discord.ui.Button(label="もう一度さがす", emoji="🔍",
                                  style=discord.ButtonStyle.secondary, row=1)
        again.callback = self._on_again
        self.add_item(again)

        back = discord.ui.Button(label="カテゴリに戻る",
                                 style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        product = self.cart.menu.products.get(self._sel.values[0])
        if product is None:
            await interaction.response.defer()
            return
        view = ProductDetailView(self.cart, product)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_again(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProductSearchModal(self.cart))

    async def _on_back(self, interaction: discord.Interaction) -> None:
        view = CategoryView(self.cart)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class CategoryView(discord.ui.View):
    def __init__(self, cart: CartView) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        minutes = now_minutes()
        # 時間帯が終わったカテゴリは出さない。
        # 夜に「朝マック」を出しても、中身はほとんど注文できない。
        active = cart.active_dayparts
        # ⚠️ 「いまだけのメニュー」を先頭に出す。
        #    朝マックの時間に朝マックが一覧の下の方にあると、
        #    その時間しか頼めないものに気づかないまま終わる。
        notable_now = {
            name for name, part in availability.COLLECTION_DAYPART.items()
            if part in availability.NOTABLE_DAYPARTS and part in (active or set())
        }
        options, featured = [], []
        for c in cart.menu.collections[:25]:
            if not availability.collection_available(c.name, active):
                continue
            count = len(
                [
                    p for p in cart.menu.visible_products(c.id, minutes)
                    if p.product_class in ("PRODUCT", "VALUE_MEAL")
                ]
            )
            if count == 0:
                continue
            now_only = (c.name or "").strip() in notable_now
            opt = discord.SelectOption(
                label=c.name[:100], value=c.id,
                description=(f"いまの時間だけ・{count} 品" if now_only
                             else f"{count} 品")[:100],
                emoji="⏰" if now_only else None,
            )
            (featured if now_only else options).append(opt)
        options = featured + options
        if not options:
            options = [discord.SelectOption(label="（今は選べる商品がありません）", value="_none")]
        # よく頼まれる商品は、カテゴリを選ばずにここから直接選べるようにする。
        # 「カテゴリ → 商品」の2段を踏まずに済み、操作が1つ減る。
        popular = cart.popular_products(limit=25)
        if popular:
            psel = discord.ui.Select(
                placeholder="人気の商品からすぐ選ぶ",
                options=[
                    discord.SelectOption(
                        label=p.name[:100], value=p.code,
                        description=f"{embeds.yen(p.price_for(cart.pickup or 'takeOut'))}",
                    )
                    for p in popular
                ],
                row=0,
            )
            psel.callback = self._on_quick
            self.add_item(psel)
            self._quick = psel

        sel = discord.ui.Select(
            placeholder="カテゴリから探す", options=options,
            row=1 if popular else 0,
        )
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel

        # 店名が検索できるのに商品名が検索できないのは不便。
        # 247商品あるので、名前が分かっているなら辿るより速い。
        find = discord.ui.Button(label="商品名でさがす", emoji="🔍",
                                 style=discord.ButtonStyle.primary, row=2)
        find.callback = self._on_search
        self.add_item(find)

        back = discord.ui.Button(label="カートに戻る", emoji=E.CART,
                                 style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._on_back
        self.add_item(back)

    async def _on_search(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProductSearchModal(self.cart))

    async def _on_quick(self, interaction: discord.Interaction) -> None:
        """人気の商品から直接選ぶ。カテゴリを飛ばす。"""
        code = self._quick.values[0]
        product = self.cart.menu.products.get(code)
        if product is None:
            await interaction.response.edit_message(
                embed=embeds.error("商品が見つかりませんでした。"), view=None
            )
            return
        # ここも商品の詳細を見せてから。入口によって挙動が違うと迷う。
        view = ProductDetailView(self.cart, product)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

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

        # 単品もセットも、まず詳細を見せる。
        # 以前は単品をいきなりカートへ入れていたため、
        # 具材を変える画面にたどり着けなかった。
        view = ProductDetailView(
            self.cart, product, back_to=(self.collection_id, self.page)
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class ProductDetailView(discord.ui.View):
    """
    商品の詳細。商品を選ぶと必ずここに来る。

    実物のアプリと同じだけの情報を出す（説明・画像・両方の価格・注意書き）。
    ここから「そのまま追加」も「具材を変えてから追加」もできる。

    ⚠️ 以前は単品をいきなりカートへ入れていた。そのせいで、
       選択枠を持たない商品（ハンバーガー類）は具材を変える画面に
       たどり着けなかった。入口はここに一本化する。
    """

    MAX_QUANTITY = 3   # 1画面で足せる数。これ以上は繰り返し押してもらう

    def __init__(
        self, cart: "CartView", product: Product,
        back_to: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.product = product
        # 「商品一覧へ」で戻る先（カテゴリID, ページ）
        self.back_to = back_to
        self._build()

    # -- 見た目 --

    def build_embed(self) -> discord.Embed:
        p = self.product
        d = p.display

        title = p.name
        if d.limited:
            title = f"{title}　🍁期間限定"

        e = discord.Embed(
            title=f"{E.BURGER} {title}",
            description=d.description or d.subtitle or None,
            color=embeds.GREEN,
        )
        if d.image_url:
            e.set_thumbnail(url=d.image_url)

        # ---- 価格 ----
        # 実データ（247商品）では店内とお持ち帰りが同額だった。
        # ただし店舗や時期で変わりうるので、違えば両方出す。
        e.add_field(name=f"{E.YEN} 価格", value=self._price_text(), inline=False)

        # ---- セットの選択枠 ----
        choices = p.slots_of("choices")
        unfillable = self.cart.menu.unfillable_slots(p, now_minutes())
        if unfillable:
            e.add_field(
                name=f"{E.WARN} ただいま承れません",
                value=(
                    "この商品は、お選びいただく内容を"
                    "こちらで用意できないため承れません。\n"
                    "恐れ入りますが、別の商品をお選びください。"
                ),
                inline=False,
            )
        if choices:
            names = [c.name or "お好きなもの" for c in choices]
            e.add_field(
                name=f"{E.CART} セットの内容",
                value="・" + "\n・".join(names[:6]),
                inline=True,
            )

        # ---- 調整できる具材 ----
        cz = p.customizations()
        if cz:
            names = [c.name for c in cz[:8]]
            more = f" ほか{len(cz) - 8}件" if len(cz) > 8 else ""
            e.add_field(
                name=f"{E.NOTE} 変更できるもの",
                value="/".join(names) + more + "\n→ 下の「カスタマイズ」から変えられます。",
                inline=False,
            )

        # ---- 提供時間 ----
        hours = self._hours_text()
        if hours:
            e.add_field(name=f"{E.LOADING} 提供時間", value=hours, inline=True)

        if d.limited and d.limited_from:
            e.add_field(name="🍁 販売期間", value=self._limited_text(), inline=True)

        # ---- 注意書き ----
        notes = "\n".join(x for x in (d.precautions, d.notes) if x).strip()
        if notes:
            e.add_field(name=f"{E.INFO} ご注意", value=notes[:1024], inline=False)

        marks = []
        if d.msc:
            marks.append("MSC認証（持続可能な漁業）")
        if d.rainforest:
            marks.append("レインフォレスト・アライアンス認証")
        if marks:
            e.set_footer(text=" / ".join(marks))

        return e

    def _price_text(self) -> str:
        """店内・お持ち帰りの金額。同額なら1行にまとめる。"""
        p = self.product
        eat, take = p.price_for("eatIn"), p.price_for("takeOut")
        if eat == take:
            return f"**{embeds.yen(eat)}**（店内・お持ち帰り）"
        return (
            f"店内　　　**{embeds.yen(eat)}**\n"
            f"お持ち帰り**{embeds.yen(take)}**"
        )

    def _hours_text(self) -> str:
        """この商品を注文できる時間帯。終日なら何も言わない。"""
        windows = self.product.time_windows
        if not windows:
            return ""
        def hhmm(m: int) -> str:
            return f"{m // 60:02d}:{m % 60:02d}"
        return "\n".join(
            f"{hhmm(w['start'])}〜{hhmm(w['end'])}" for w in windows[:3]
        )

    def _limited_text(self) -> str:
        d = self.product.display
        def day(v: str) -> str:
            return v[:10].replace("-", "/") if len(v) >= 10 else v
        start = day(d.limited_from)
        end = day(d.limited_to) if d.limited_to else ""
        return f"{start}〜{end}" if end else f"{start}〜"

    # -- 組み立て --

    def _build(self) -> None:
        self.clear_items()
        choices = self.product.slots_of("choices")

        if choices:
            # ⚠️ 埋めようがない枠を持つセットは、そもそも追加させない。
            #    選び終えてからマクドナルドに断られるのが一番つらい。
            blocked = bool(
                self.cart.menu.unfillable_slots(self.product, now_minutes())
            )
            go = discord.ui.Button(
                label="中身を選べません" if blocked else "中身を選ぶ",
                emoji=E.NG if blocked else E.CART,
                style=(
                    discord.ButtonStyle.secondary if blocked
                    else discord.ButtonStyle.success
                ),
                disabled=blocked, row=0,
            )
            go.callback = self._on_choose
            self.add_item(go)
        else:
            for n in range(1, self.MAX_QUANTITY + 1):
                b = discord.ui.Button(
                    label=f"{n}個 追加", emoji=E.PLUS if n == 1 else None,
                    style=discord.ButtonStyle.success, row=0,
                )
                b.callback = self._make_add(n)
                self.add_item(b)

        if self.product.customizations():
            cz = discord.ui.Button(
                label="カスタマイズ", emoji=E.NOTE,
                style=discord.ButtonStyle.secondary, row=1,
            )
            cz.callback = self._on_customize
            self.add_item(cz)

        back = discord.ui.Button(
            label="商品一覧へ", emoji="◀️", style=discord.ButtonStyle.secondary, row=2
        )
        back.callback = self._on_back
        self.add_item(back)

        cart = discord.ui.Button(
            label="カートを確認する", emoji=E.CART,
            style=discord.ButtonStyle.primary, row=2,
        )
        cart.callback = self._on_cart
        self.add_item(cart)

        url = self.product.display.detail_url
        if url.startswith("https://"):
            self.add_item(
                discord.ui.Button(
                    label="アレルギー・栄養情報", emoji=E.INFO,
                    style=discord.ButtonStyle.link, url=url, row=2,
                )
            )

    # -- 操作 --

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    def _make_add(self, quantity: int):
        async def cb(interaction: discord.Interaction) -> None:
            # 上限を超える分は足さない。黙って減らさず、足せた数を伝える。
            room = config.CART_MAX_ITEMS - len(self.cart.items)
            added = max(0, min(quantity, room))
            if added == 0:
                await interaction.response.send_message(
                    embed=embeds.warn(
                        f"カートに入れられるのは {config.CART_MAX_ITEMS} 点までです。"
                    ),
                    ephemeral=True,
                )
                return

            # まとめて足してから1回だけ保存する（押すたびに書かない）
            for _ in range(added):
                self.cart.items.append(
                    build_order_item(self.cart, self.product, {})
                )
            await save_cart(
                self.cart.owner_id, purpose=self.cart.purpose,
                store_id=self.cart.store_id, pickup=self.cart.pickup,
                items=self.cart.items,
            )
            self.stop()
            suffix = f" ×{added}" if added > 1 else ""
            note = f"{E.OK} **{self.product.name}**{suffix} を追加しました。"
            if added < quantity:
                note += f"\n{E.WARN} 上限のため {quantity - added} 点は追加できませんでした。"
            await self.cart.show(interaction, note=note)
        return cb

    async def _on_choose(self, interaction: discord.Interaction) -> None:
        view = OptionView(
            self.cart, self.product, self.product.slots_of("choices"),
            back_to=self.back_to,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_customize(self, interaction: discord.Interaction) -> None:
        view = CustomizeView(self.cart, self.product, {}, back_to=self.back_to)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if self.back_to:
            collection_id, page = self.back_to
            view = ProductView(self.cart, collection_id, page)
        else:
            view = CategoryView(self.cart)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_cart(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.cart.show(interaction)


class OptionView(discord.ui.View):
    """セットのサイド・ドリンクなどを選ぶ。"""

    def __init__(
        self, cart: CartView, product: Product, choices: list,
        back_to: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.product = product
        self.back_to = back_to
        # ⚠️ 構成品が持つ枠（ナゲットのソースなど）も一緒に聞く。
        #    聞かずに送ると必須の枠が空になり、断られる。
        #    鍵は「構成品コード/枠コード」。上位の枠と混ざらないように。
        self.nested = cart.menu.nested_choices(product)
        self.choices = list(choices)
        self._keys: dict[int, str] = {}      # 枠の並び順 → picks の鍵
        for i, c in enumerate(self.choices):
            self._keys[i] = c.code
        for comp_code, slot in self.nested:
            self._keys[len(self.choices)] = f"{comp_code}/{slot.code}"
            self.choices.append(slot)

        self.picks: dict[str, str] = {}
        for i, c in enumerate(self.choices):
            if c.default_product:
                self.picks[self._keys[i]] = c.default_product
        self._build()

    def _key_of(self, slot) -> str:
        """その枠を picks に記録するときの鍵。"""
        for i, c in enumerate(self.choices):
            if c is slot:
                return self._keys[i]
        return slot.code

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title=f"{E.BURGER} {self.product.name}",
            description="内容を選んでください。",
            color=embeds.GREEN,
        )
        for c in self.choices:
            codes = picked_codes(self.picks.get(self._key_of(c)))
            shown = [
                f"{pp.name}×{n}" if n > 1 else pp.name
                for code, n in spread_quantity(codes, c.need)
                if (pp := self.cart.menu.products.get(code))
            ]
            # ⚠️ 見出しに枠コード（9987009）をそのまま出さない。
            #    利用者には何の枠か分からない。
            name = self._slot_label(c)
            if c.multi:
                name += f"（{c.need}個）"
            e.add_field(
                name=name,
                value=("\n".join(shown) if shown else f"{E.WARN} 未選択"),
                inline=True,
            )
        pickup = self.cart.pickup or "takeOut"
        base = self.product.price_for(pickup)
        # ⚠️ 選ぶものによってはお値段が上がる。**いくら上がるかは
        #    こちらには分からない**（docs/09 V-23）。分かったふりを
        #    せず「変わることがある」とだけ伝え、確定した金額は
        #    注文の直前にマクドナルドの数字で見せる。
        e.add_field(
            name=f"{E.YEN} 価格",
            value=(
                f"**{embeds.yen(base)}**"
                + ("\n（お選びいただく内容によって変わることがあります）"
                   if self.choices else "")
            ),
            inline=False,
        )
        return e

    def _slot_label(self, slot, candidates: list[Product] | None = None) -> str:
        """
        選択枠の見出し。

        枠そのものに名前が無いので、中に入る商品から言い当てる。
        カテゴリ名（ドリンク・サイドメニュー）が引ければそれを使い、
        引けなければ候補の名前から推し量る。
        """
        if slot.name:
            return slot.name
        menu = self.cart.menu
        base = menu.slot_reference(slot)
        col = menu.collection_of(base) if base else None
        if col:
            return col.name
        if base and base in menu.products:
            name = menu.products[base].name
            # 「バーベキューソース」→「ソース」
            for word in ("ソース", "ドリンク", "サイド"):
                if word in name:
                    return word
            return name
        return "お選びください"

    def _build(self) -> None:
        """
        選択枠の数だけ Select を並べ、最後にボタンを置く。

        ⚠️ **Discord の行の決まり**
              ・1画面は5行（row 0〜4）
              ・Select は1つで**1行を丸ごと**使う（幅5）
              ・Button は幅1。1行に5個まで並べられる

           つまり「Select がある行には Button を置けない」。
           置こうとすると ValueError（6 > 5 width）で**ビューを作る処理ごと
           落ちる**。落ちると応答を返せないので、利用者には
           「BOTは時間内に応答しませんでした」としか見えない。

        ⚠️ ボタンの行を決め打ちしないこと。
           かつて row=3 固定にしていたため、枠が4つある商品
           （ハッピーセット＝サイド・おもちゃ・ドリンク＋ソース）や、
           サイズ選択が増えて4行目まで埋まったセットで必ず落ちていた。
           実データで42商品が該当した。**空いている次の行に置く。**
        """
        self.clear_items()
        row = 0
        # 最後の1行はボタン用に空けておく（Select は 0〜3 の4行まで）
        for c in self.choices:
            if row >= 4:
                break
            candidates = self._candidates(c)
            if not candidates:
                continue
            key = self._key_of(c)
            label = self._slot_label(c, candidates)
            now = picked_codes(self.picks.get(key))
            # ⚠️ ドリンクのように種類が多い枠は、絵文字を付けて
            #    一目で仲間が分かるようにする。
            #    並び順は choice_candidates が種類ごとにまとめている。
            from services.mcd import drinks

            grouped = drinks.is_drink_slot(candidates)
            options = []
            # ⚠️ ここに「+¥50」のような上乗せ額を出さない。
            #    こちらには正しい額が分からない（docs/09 V-23）。
            #    間違った額を自信ありげに出すくらいなら、出さないほうがよい。
            for p in candidates[:25]:
                opt = discord.SelectOption(
                    label=p.name[:100], value=p.code, default=(p.code in now),
                )
                if grouped:
                    order, emoji, group_name = drinks.group_of(p.name)
                    opt.emoji = emoji
                    opt.description = group_name
                options.append(opt)
            # ⚠️ ナゲット15ピースのソースのように3個必須の枠は、
            #    実際のアプリと同じく **種類を分けて選べる** ようにする。
            #    足りない分は選んだものを増やして埋めるので、
            #    1種類だけ選んでも注文できる。
            if c.multi:
                sel = discord.ui.Select(
                    placeholder=f"{label}（{c.need}個までお選びいただけます）"[:150],
                    options=options, row=row,
                    min_values=1, max_values=min(c.need, len(options)),
                )
            else:
                sel = discord.ui.Select(placeholder=label, options=options, row=row)
            sel.callback = self._make_cb(key, sel)
            self.add_item(sel)
            row += 1

            # サイズを選べる商品なら、その下にサイズも出す
            sizes = self._sizes(c)
            if len(sizes) > 1 and row < 4:
                size_options = [
                    discord.SelectOption(
                        label=p.name[:100], value=p.code,
                        default=(p.code in picked_codes(self.picks.get(key))),
                    )
                    for p in sizes[:25]
                ]
                ssel = discord.ui.Select(
                    placeholder=f"{label}のサイズ", options=size_options, row=row
                )
                ssel.callback = self._make_cb(key, ssel)
                self.add_item(ssel)
                row += 1

        # ⚠️ ここが肝心。Select を置いた**次の行**にボタンを並べる。
        #    row を使い切っていれば4行目（最後の行）に置く。
        btn_row = min(row, 4)
        ok = discord.ui.Button(
            label="カートに追加", emoji=E.PLUS,
            style=discord.ButtonStyle.success, row=btn_row,
        )
        ok.callback = self._on_ok
        self.add_item(ok)
        if self.product.customizations():
            cz = discord.ui.Button(
                label="具材を変える", emoji=E.NOTE,
                style=discord.ButtonStyle.secondary, row=btn_row,
            )
            cz.callback = self._on_customize
            self.add_item(cz)
        back = discord.ui.Button(
            label="戻る", style=discord.ButtonStyle.secondary, row=btn_row,
        )
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
        # ⚠️ 入れ子の枠（ナゲットのソース）は、親が構成品のほうになる。
        #    親を間違えると候補を引けない。
        owner = self.product
        for comp_code, sl in self.nested:
            if sl is slot:
                owner = self.cart.menu.products.get(comp_code) or self.product
                break
        return self.cart.menu.choice_candidates(slot, now_minutes(), parent=owner)

    def _sizes(self, slot) -> list[Product]:
        """いま選んでいる商品のサイズ違い。無ければ空。"""
        codes = picked_codes(self.picks.get(self._key_of(slot)))
        # 複数選べる枠はサイズ違いを出さない（どれのサイズか決められない）
        if len(codes) != 1:
            return []
        chosen = codes[0]
        minutes = now_minutes()
        return [
            p for p in self.cart.menu.size_variants(chosen)
            if p.is_orderable_at(minutes)
        ]

    def _make_cb(self, key: str, sel: discord.ui.Select):
        async def cb(interaction: discord.Interaction) -> None:
            self.picks[key] = ",".join(sel.values)
            self._build()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        return cb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cart.owner_id

    async def _on_back(self, interaction: discord.Interaction) -> None:
        view = ProductDetailView(self.cart, self.product, back_to=self.back_to)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_ok(self, interaction: discord.Interaction) -> None:
        # ⚠️ 必須の枠が埋まっていない注文は送らない。
        #    空のまま送るとマクドナルドに断られ、理由が分からない。
        empty = [
            c for c in self.choices
            if c.min_quantity >= 1
            and not (self.picks.get(self._key_of(c)) or c.default_product)
        ]
        if empty:
            names = "・".join(c.name or "お選びいただく内容" for c in empty)
            await interaction.response.send_message(
                embed=embeds.warn(
                    f"{names} が選ばれていません。\n"
                    "お選びいただいてから、カートに追加してください。"
                ),
                ephemeral=True,
            )
            return
        await _add_to_cart(self.cart, self.product, self.picks)
        self.stop()
        await self.cart.show(
            interaction, note=f"{E.OK} **{self.product.name}** を追加しました。"
        )

    async def _on_customize(self, interaction: discord.Interaction) -> None:
        """具材を調整してから入れる。"""
        view = CustomizeView(
            self.cart, self.product, self.picks, back_to=self.back_to
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class CustomizeView(discord.ui.View):
    """
    具材の増減を選ぶ（ピクルス抜き・氷抜きなど）。

    外せる具材は最初から全部入れた状態で出し、**外したいものだけを
    選択から外してもらう**。ふだんの注文は何もせず「この内容で追加」を
    押すだけで済む。
    """

    def __init__(
        self, cart: "CartView", product: Product, picks: dict[str, str],
        back_to: tuple[str, int] | None = None,
    ) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.cart = cart
        self.product = product
        self.picks = picks
        self.back_to = back_to
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
        # 商品の画面へ返す。カテゴリまで飛ばされると選び直しになってしまう。
        view = ProductDetailView(self.cart, self.product, back_to=self.back_to)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_ok(self, interaction: discord.Interaction) -> None:
        await _add_to_cart(self.cart, self.product, self.picks, self.amounts)
        self.stop()
        note = f"{E.OK} **{self.product.name}** を追加しました。"
        removed = [s.name for s in self.slots if self.amounts.get(s.code, 1) == 0]
        if removed:
            note += f"（{'・'.join(removed)}抜き）"
        await self.cart.show(interaction, note=note)


def price_of(menu: ParsedMenu, item: OrderItem, pickup: str) -> int:
    """
    カタログの定価。受取方法ごとに別の値が入っている（EATIN / TAKEOUT / OTHER）。

    カートに入れた時点では受取方法が決まっていないので、
    保存してある amount ではなくカタログから引き直す。
    """
    p = menu.products.get(str(item.product_code))
    if p is None:
        return int(item.amount or 0)
    # ⚠️ **選んだ中身による上乗せ額を、ここで当てにいかない。**
    #    実機で確かめたところ、カフェラテ（単品¥240・参照のコーラM
    #    ¥310 より安い）は +¥50、野菜生活100（¥330・参照より高い）は
    #    +¥0 だった。単品価格の差では説明がつかず、カタログの
    #    どこにも書かれていない（docs/09 V-23）。
    #    一度「差額＝単品価格の差」で計算したが、8件中5件外れた。
    #
    #    **当て推量の金額を出すくらいなら、定価だけを出す。**
    #    本当の金額はマクドナルドが注文の登録時に返してくるので、
    #    決済の前にそれを見せて確かめる（core/saga.py）。
    return p.price_for(pickup)


def _unit_price(product, pickup: str) -> int:
    """単品価格。値段を持たない選択肢なら0。

    ⚠️ 選択枠の候補には `Extra`（ナゲットのソース・ハッピーセットの
       おもちゃ・ドレッシング）が混ざる。これは `products` に載らない
       ので価格を持たない。`price_for` を呼ぶと AttributeError で
       **画面を作る処理ごと落ち**、利用者には
       「BOTは時間内に応答しませんでした」としか見えない。
       もともと無料の選択肢なので、0として扱う。
    """
    getter = getattr(product, "price_for", None)
    if not callable(getter):
        return 0
    try:
        return int(getter(pickup) or 0)
    except Exception:
        return 0


def reprice(menu: ParsedMenu, items: list[OrderItem], pickup: str) -> None:
    """
    受取方法が決まったところで、金額を入れ直す。

    ⚠️ ここを忘れると、受取方法と違う金額で注文コードを作ってしまう。
       負担率の計算も狂う。実データでは店内とお持ち帰りが同額だったが、
       デリバリーは202/247商品で別の値段だった。店舗や時期でも変わる。
    """
    for item in items:
        item.amount = price_of(menu, item, pickup)


@dataclass
class CartGroup:
    """カートの中で同じ内容がいくつ並んでいるか。"""
    item: OrderItem
    count: int
    indexes: list[int]      # self.items の中での位置


def group_items(items: list[OrderItem]) -> list[CartGroup]:
    """
    まったく同じ内容のものをまとめる。

    ⚠️ 表示と削除・増減で**必ず同じまとめ方**を使うこと。
       別々に数えると、画面の「3.」と消える商品がずれる。
    """
    groups: list[CartGroup] = []
    keys: list[str] = []
    for i, item in enumerate(items):
        key = json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False)
        if keys and keys[-1] == key:
            groups[-1].count += 1
            groups[-1].indexes.append(i)
            continue
        groups.append(CartGroup(item=item, count=1, indexes=[i]))
        keys.append(key)
    return groups


def _slot_labels(menu: ParsedMenu, product: Product | None) -> dict[str, str]:
    """
    選択枠のコード → 「サイドメニュー」「ドリンク」などの名前。

    枠そのものに名前が付いていないことが多い（9987009 のような符号だけ）。
    その場合は、その枠に入る商品が属するカテゴリ名で代える。
    「ハッシュポテト」とだけ出るより「サイドメニュー：ハッシュポテト」の
    ほうが、何の枠なのか分かる。
    """
    if product is None:
        return {}
    out: dict[str, str] = {}
    for slot in product.slots_of("choices"):
        code = str(slot.code)
        if slot.name:
            out[code] = slot.name
            continue
        ref = str(slot.reference_product or slot.default_product or "")
        col = menu.collection_of(ref) if ref else None
        if col and col.name:
            out[code] = col.name
    return out


def cart_item_lines(
    menu: ParsedMenu, item: OrderItem, pickup: str = "takeOut"
) -> list[str]:
    """
    セットの中身を1品ぶん書き出す。

    枠の名前が分かるときは「サイド：マックフライポテト(M)」と出す。
    ただ商品名が並ぶだけだと、何がどの枠なのか分からないため。
    """
    parent = menu.products.get(str(item.product_code))
    labels = _slot_labels(menu, parent)
    lines: list[str] = []
    for comp in item.components:
        label = labels.get(str(comp.product_code), "")
        # 枠 → （中間ノード）→ 商品 と辿って、一番奥の商品名を出す
        leaves = [
            menu.products[str(x.product_code)]
            for x in comp.walk()
            if x is not comp and str(x.product_code) in menu.products
        ]
        if not leaves:
            continue
        name = leaves[-1].name
        lines.append(f"　└ {label}：{name}" if label else f"　└ {name}")
    return lines


def cart_lines(
    menu: ParsedMenu, items: list[OrderItem], pickup: str = "takeOut"
) -> list[str]:
    """
    カートの中身を人が読める行にする。

    まったく同じ内容のものは「×3」とまとめる。
    「3個 追加」で3行並ぶと読みにくいため。
    """
    lines: list[str] = []
    for idx, g in enumerate(group_items(items), 1):
        p = menu.products.get(g.item.product_code)
        name = p.name if p else g.item.product_code
        note = customization_note(menu, g.item)
        qty = f" ×{g.count}" if g.count > 1 else ""
        money = embeds.yen(price_of(menu, g.item, pickup) * g.count)
        lines.append(f"**{idx}.** {name}{note}{qty}　{money}")
        lines += cart_item_lines(menu, g.item, pickup)
    return lines


def nearby_lines(store_id: str, limit: int = 3) -> list[str]:
    """
    近くの注文できる店舗を、画面に出せる形で返す。

    ⚠️ 索引に位置が入っていなければ空を返す（古い索引のとき）。
       間違った距離を出すより、何も出さないほうがよい。
    """
    from services.mcd import store_index

    try:
        found = store_index.nearby(store_id, limit=limit)
    except Exception:
        log.exception("近くの店舗を探せませんでした")
        return []
    out = []
    for entry, km in found:
        far = f"{km:.1f}km" if km >= 1 else f"{int(km * 1000)}m"
        out.append(f"・**{entry.name}**（`{entry.store_id}`）　約 {far}")
    return out


async def _add_to_cart(
    cart: "CartView",
    product: Product,
    picks: dict[str, str],
    amounts: dict[str, int] | None = None,
) -> bool:
    """
    カートに1品足して保存する。追加の経路はここに一本化する。

    上限に達していたら足さずに False を返す。
    """
    if len(cart.items) >= config.CART_MAX_ITEMS:
        return False
    cart.items.append(build_order_item(cart, product, picks, amounts))
    await save_cart(
        cart.owner_id, purpose=cart.purpose, store_id=cart.store_id,
        pickup=cart.pickup, items=cart.items,
    )
    return True


def picked_codes(value: str | None) -> list[str]:
    """
    picks に入っている「選んだもの」を取り出す。

    ⚠️ ナゲット15ピースのソースのように **複数選べる枠** があるため、
       picks の値はコンマ区切りで複数入ることがある。
    """
    if not value:
        return []
    return [c for c in str(value).split(",") if c]


def spread_quantity(codes: list[str], need: int) -> list[tuple[str, int]]:
    """
    選んだものに個数を割り振る。

    3個必須の枠で1種類だけ選ばれたら、その1種類を3個にする。
    3種類選ばれたら1個ずつ。2種類なら 2個+1個。
    """
    codes = list(dict.fromkeys(codes))      # 重複は除く（順序は保つ）
    if not codes:
        return []
    need = max(need, len(codes))
    base, rest = divmod(need, len(codes))
    return [(c, base + (1 if i < rest else 0)) for i, c in enumerate(codes)]


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
    menu = cart.menu
    # 構成品のうち、さらに選択枠を持つもの（ナゲットのソースなど）
    nested = {}
    for comp_code, slot in menu.nested_choices(product):
        chosen = picked_codes(
            picks.get(f"{comp_code}/{slot.code}")
        ) or picked_codes(slot.default_product)
        if chosen:
            nested.setdefault(comp_code, []).append((slot, chosen))

    for slot in product.slots_of("composition"):
        if not slot.code:
            continue
        qty = amounts.get(slot.code, slot.default_quantity)
        # カタログが許す範囲に収める（不正な数量を送らない）
        qty = max(slot.min_quantity, min(qty, slot.max_quantity))

        # ⚠️ 構成品が選択枠を持っているときは、既定どおりでも**送る**。
        #    中に「選んだもの」を入れて渡す必要があるため。
        #    ポテナゲのナゲットのソースがこれ。送らないと
        #    必須の枠が空のまま注文することになり、断られる。
        inner = nested.get(slot.code)
        if inner:
            picked = [
                OrderItem(
                    product_code=sl.code, quantity=sl.need, has_flag=True,
                    components=[
                        OrderItem(product_code=code, quantity=n)
                        for code, n in spread_quantity(chosen, sl.need)
                    ],
                )
                for sl, chosen in inner
            ]
            components.append(
                # ⚠️ 構成品（ナゲットなど）は **商品** なのでフラグを付けない。
                #    中にソースの枠を抱えていても付けない（実物で確認）。
                OrderItem(product_code=slot.code, quantity=qty, components=picked)
            )
            continue

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
        chosen = picked_codes(picks.get(slot.code)) or picked_codes(
            slot.default_product
        )
        if not chosen:
            continue
        # ⚠️ 枠が求める個数をそのまま送る。1個固定で送っていたため、
        #    ソース3個必須のナゲット15ピースが注文できなかった。
        leaves = [
            OrderItem(product_code=code, quantity=n)
            for code, n in spread_quantity(chosen, slot.need)
        ]
        # ⚠️ 枠コードだけで引かない。朝マックのドリンク枠（9997925）は
        #    通常セット（9997918）と別コードだが、同じ中間ノードが要る。
        bridge = menu.bridge_for(slot)
        if bridge:
            leaves = [
                OrderItem(product_code=bridge, quantity=leaf.quantity,
                          has_flag=True, components=[leaf])
                for leaf in leaves
            ]
        components.append(
            OrderItem(
                product_code=slot.code, quantity=slot.need, has_flag=True,
                components=leaves,
            )
        )

    return OrderItem(
        product_code=product.code,
        quantity=1,
        amount=product.price_for(pickup),
        components=components,
    )
