"""
店舗情報とメニューの取得・保存

店舗JSONの store.api.ordRootUrl から group が直接わかるため、
group-e〜h を総当たりする必要はない（docs/08 §4）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import MenuCollection, MenuProduct, StoreCache, StoreDaypart, utcnow
from db.session import session_scope
from services.mcd.client import McdClient, McdError
from services.mcd.menu import (
    Collection, MenuDiff, ParsedMenu, Product, Slot,
    diff_menus, parse_dayparts, parse_menu, supported_pickup_methods,
)

log = logging.getLogger("bot.mcd.stores")

GROUP_RE = re.compile(r"ord\.(group-[a-z])\.")


@dataclass
class StoreInfo:
    store_id: str
    group: str
    name: str
    address: str
    latitude: float
    longitude: float
    cat_root_url: str
    ord_root_url: str
    delivery_methods: dict[str, bool]

    def supports(self, pickup_method: str) -> bool:
        return bool(self.delivery_methods.get(pickup_method))


def _extract_group(store: dict, fallback: str) -> str:
    ord_url = ((store.get("store") or {}).get("api") or {}).get("ordRootUrl") or ""
    m = GROUP_RE.search(ord_url)
    return m.group(1) if m else fallback


async def resolve_store(
    client: McdClient, store_id: str, *, force: bool = False
) -> StoreInfo:
    """
    店舗情報を取得する。キャッシュがあれば使う。

    店舗IDは5桁。数字以外が混ざっていたら弾く。
    """
    store_id = store_id.strip()
    if not store_id.isdigit():
        raise McdError("店舗IDは数字で指定してください")

    if not force:
        async with session_scope() as s:
            row = await s.get(StoreCache, store_id)
            if row and row.cat_root_url:
                row.hit_count += 1
                return StoreInfo(
                    store_id=store_id, group=row.group_name, name=row.store_name or "",
                    address=row.address or "",
                    latitude=float(row.latitude or 0), longitude=float(row.longitude or 0),
                    cat_root_url=row.cat_root_url, ord_root_url=row.ord_root_url or "",
                    delivery_methods=json.loads(row.delivery_methods or "{}"),
                )

    raw, group = await client.fetch_store(store_id)
    store = raw.get("store") or {}
    api = store.get("api") or {}
    info = StoreInfo(
        store_id=store_id,
        group=_extract_group(raw, group),
        name=store.get("name") or "",
        address=store.get("address") or "",
        latitude=float(store.get("latitude") or 0),
        longitude=float(store.get("longitude") or 0),
        cat_root_url=api.get("catRootUrl") or "",
        ord_root_url=api.get("ordRootUrl") or "",
        delivery_methods=supported_pickup_methods(raw),
    )

    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
        if row is None:
            row = StoreCache(store_id=store_id, group_name=info.group)
            s.add(row)
        row.group_name = info.group
        row.store_name = info.name
        row.address = info.address
        row.latitude = info.latitude
        row.longitude = info.longitude
        row.cat_root_url = info.cat_root_url
        row.ord_root_url = info.ord_root_url
        row.delivery_methods = json.dumps(info.delivery_methods, ensure_ascii=False)
        row.resolved_at = utcnow()

        # 時間帯も保存する
        await s.execute(delete(StoreDaypart).where(StoreDaypart.store_id == store_id))
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for dp in parse_dayparts(raw, date_key):
            s.add(
                StoreDaypart(
                    store_id=store_id, daypart=dp.name, date=date_key,
                    visible=json.dumps(dp.visible),
                    checkoutable=json.dumps(dp.checkoutable),
                )
            )

    log.info("店舗を解決しました: %s %s (%s)", store_id, info.name, info.group)
    return info


# ============================================================
#  メニュー
# ============================================================

async def menu_age_minutes(store_id: str) -> float | None:
    async with session_scope() as s:
        row = (
            await s.execute(
                select(MenuProduct.synced_at).where(MenuProduct.store_id == store_id).limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    if row.tzinfo is None:
        row = row.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - row).total_seconds() / 60.0


async def sync_menu(
    client: McdClient, store_id: str, *, store: StoreInfo | None = None
) -> MenuDiff:
    """
    メニューを取得してDBへ保存し、前回との差分を返す。

    新商品・終売・値上げはここで検出される。
    """
    info = store or await resolve_store(client, store_id)
    if not info.cat_root_url:
        raise McdError(f"店舗 {store_id} のカタログURLが不明です")

    raw = await client.fetch_menu(store_id, info.cat_root_url)
    parsed = parse_menu(store_id, raw)

    async with session_scope() as s:
        old = [
            (r.product_code, r.name_ja, r.price_takeout or 0)
            for r in (
                await s.execute(
                    select(MenuProduct).where(MenuProduct.store_id == store_id)
                )
            ).scalars().all()
        ]
        diff = diff_menus(old, parsed)

        await s.execute(delete(MenuProduct).where(MenuProduct.store_id == store_id))
        await s.execute(delete(MenuCollection).where(MenuCollection.store_id == store_id))
        now = utcnow()
        for p in parsed.products.values():
            s.add(
                MenuProduct(
                    store_id=store_id, product_code=p.code, name_ja=p.name,
                    product_class=p.product_class, day_part=p.day_part,
                    price_eatin=p.price_eatin, price_takeout=p.price_takeout,
                    price_other=p.price_other, pre_price=p.pre_price,
                    structure=p.structure_json(),
                    time_windows=json.dumps(p.time_windows),
                    synced_at=now,
                )
            )
        for c in parsed.collections:
            s.add(
                MenuCollection(
                    store_id=store_id, collection_id=c.id, name_ja=c.name,
                    product_codes=json.dumps(c.product_codes, ensure_ascii=False),
                    sort_order=c.sort_order,
                )
            )

    log.info(
        "メニューを同期しました: %s 商品%d件（新規%d / 終売%d / 価格変更%d）",
        store_id, len(parsed.products),
        len(diff.added), len(diff.removed), len(diff.price_changed),
    )
    return diff


async def ensure_menu_fresh(client: McdClient, store_id: str) -> None:
    """
    注文の直前に呼ぶ。キャッシュが古ければ取り直す。

    価格がずれたまま注文すると、負担率の計算が狂う。
    """
    age = await menu_age_minutes(store_id)
    if age is None or age > config.MENU_STALE_MINUTES:
        await sync_menu(client, store_id)


async def load_menu(store_id: str) -> ParsedMenu:
    """DBからメニューを読み出す。"""
    async with session_scope() as s:
        prows = (
            await s.execute(select(MenuProduct).where(MenuProduct.store_id == store_id))
        ).scalars().all()
        crows = (
            await s.execute(
                select(MenuCollection)
                .where(MenuCollection.store_id == store_id)
                .order_by(MenuCollection.sort_order)
            )
        ).scalars().all()

    products: dict[str, Product] = {}
    for r in prows:
        products[r.product_code] = Product(
            code=r.product_code, name=r.name_ja,
            product_class=r.product_class or "PRODUCT", day_part=r.day_part or "",
            price_eatin=r.price_eatin or 0, price_takeout=r.price_takeout or 0,
            price_other=r.price_other or 0, pre_price=r.pre_price or 0,
            slots=[Slot.from_dict(d) for d in json.loads(r.structure or "[]")],
            time_windows=json.loads(r.time_windows or "[]"),
        )
    collections = [
        Collection(
            id=r.collection_id, name=r.name_ja,
            product_codes=json.loads(r.product_codes or "[]"), sort_order=r.sort_order,
        )
        for r in crows
    ]
    return ParsedMenu(store_id=store_id, products=products, collections=collections)


async def active_store_ids(days: int | None = None) -> list[str]:
    """直近で使われた店舗。定期同期の対象を絞るのに使う。"""
    days = days or config.MENU_ACTIVE_STORE_DAYS
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(StoreCache.store_id).where(StoreCache.resolved_at >= since)
            )
        ).scalars().all()
    return list(rows)
