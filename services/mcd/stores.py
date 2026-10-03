"""
店舗情報とメニューの取得・保存

店舗JSONの store.api.ordRootUrl から group が直接わかるため、
group-e〜j を総当たりする必要はない（docs/08 §4）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

import config
from db.models import as_utc, MenuCollection, MenuProduct, StoreCache, StoreDaypart, utcnow
from db.session import session_scope
from services.mcd.client import McdClient, McdError
from services.mcd import availability
from services.mcd.menu import (
    Collection, Display, Extra, MenuDiff, ParsedMenu, Product, Slot,
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

    cached: StoreInfo | None = None
    etag = None
    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
        if row and row.cat_root_url:
            row.hit_count += 1
            etag = row.store_etag
            cached = StoreInfo(
                store_id=store_id, group=row.group_name, name=row.store_name or "",
                address=row.address or "",
                latitude=float(row.latitude or 0), longitude=float(row.longitude or 0),
                cat_root_url=row.cat_root_url, ord_root_url=row.ord_root_url or "",
                delivery_methods=json.loads(row.delivery_methods or "{}"),
            )
            age = _age_minutes(row.resolved_at)
            # 営業時間や提供時間帯は日ごとに変わるので、一定時間で取り直す。
            # ETag のおかげで、変更が無ければ通信量はゼロで済む。
            from core import settings as _settings
            ttl = int(_settings.get("store_refresh_minutes", config.STORE_REFRESH_MINUTES))
            if not force and age is not None and age < ttl:
                return cached

    raw, group, new_etag = await client.fetch_store(store_id, etag=etag if not force else None)
    if raw is None and cached is not None:
        # 変更なし。確認した時刻だけ更新しておく。
        async with session_scope() as s:
            row = await s.get(StoreCache, store_id)
            if row:
                row.resolved_at = utcnow()
        return cached

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
        row.store_etag = new_etag or None

        # 注文できるかの判定に使う情報（店舗を選んだ時点で理由を出すため）
        row.mop_enabled = bool(store.get("mopEnabled", True))
        row.foe_status = str(store.get("foeStatus") or "NORMAL")
        row.method_hours = json.dumps(
            availability.method_hours(raw), ensure_ascii=False
        )

        # 時間帯も保存する
        await s.execute(delete(StoreDaypart).where(StoreDaypart.store_id == store_id))
        # ⚠️ 日付キーは**日本の日付**。UTCで作ると日本時間の0〜9時に
        #    前日のキーを見てしまい、時間帯が一件も取れなくなる。
        date_key = config.today_jst()
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

def _age_minutes(when: datetime | None) -> float | None:
    """その時刻から何分経ったか。"""
    when = as_utc(when)
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 60.0


async def menu_age_minutes(store_id: str) -> float | None:
    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
        if row is not None and row.menu_synced_at is not None:
            return _age_minutes(row.menu_synced_at)
        first = (
            await s.execute(
                select(MenuProduct.synced_at).where(MenuProduct.store_id == store_id).limit(1)
            )
        ).scalar_one_or_none()
    return _age_minutes(first)


async def sync_menu(
    client: McdClient, store_id: str, *, store: StoreInfo | None = None,
    force: bool = False,
) -> MenuDiff:
    """
    メニューを取得してDBへ保存し、前回との差分を返す。

    新商品・終売・値上げ・提供時間帯の変更はここで取り込まれる。

    前回のETagを送るので、内容が変わっていなければサーバーは 304 を返し、
    約1MBのダウンロードが発生しない。そのため高頻度で呼んでも負荷が小さい。
    """
    info = store or await resolve_store(client, store_id)
    if not info.cat_root_url:
        raise McdError(f"店舗 {store_id} のカタログURLが不明です")

    async with session_scope() as s:
        row = await s.get(StoreCache, store_id)
        etag = None if force else (row.menu_etag if row else None)
        if etag:
            # ETagはあるのに商品が1件も無い場合（DBを消した後など）は、
            # 304で「変更なし」と判断してしまい、いつまでも空のままになる。
            has_products = await s.scalar(
                select(func.count()).select_from(MenuProduct)
                .where(MenuProduct.store_id == store_id)
            )
            if not has_products:
                log.info("商品が未登録のため、ETagを無視して取得します: %s", store_id)
                etag = None

    raw, new_etag = await client.fetch_menu(store_id, info.cat_root_url, etag=etag)
    if raw is None:
        # 変更なし。確認した時刻だけ更新する。
        async with session_scope() as s:
            row = await s.get(StoreCache, store_id)
            if row:
                row.menu_synced_at = utcnow()
        log.debug("メニューに変更はありませんでした: %s", store_id)
        return MenuDiff()

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
                    # None（登録なし）と []（扱っていない）を区別して保存する。
                    # 混同すると、注文できない商品を表示してしまう。
                    time_windows=(
                        None if p.time_windows is None else json.dumps(p.time_windows)
                    ),
                    size_group=p.size_group or None,
                    display=(
                        json.dumps(p.display.to_dict(), ensure_ascii=False)
                        if p.display.to_dict() else None
                    ),
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
        row = await s.get(StoreCache, store_id)
        if row:
            row.menu_etag = new_etag or None
            row.menu_synced_at = now
            row.menu_extras = json.dumps(
                [e.to_dict() for e in parsed.extras.values()], ensure_ascii=False
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
            time_windows=(
                None if r.time_windows is None else json.loads(r.time_windows)
            ),
            size_group=r.size_group or "",
            display=Display.from_dict(json.loads(r.display) if r.display else None),
        )
    collections = [
        Collection(
            id=r.collection_id, name=r.name_ja,
            product_codes=json.loads(r.product_codes or "[]"), sort_order=r.sort_order,
        )
        for r in crows
    ]
    # 選択肢専用の商品（ソース・ドレッシングなど）
    extras: dict[str, Extra] = {}
    async with session_scope() as s:
        cache = await s.get(StoreCache, store_id)
        raw_extras = cache.menu_extras if cache else None
    if raw_extras:
        try:
            for d in json.loads(raw_extras):
                e = Extra.from_dict(d)
                if e.code:
                    extras[e.code] = e
        except (ValueError, TypeError):
            log.warning("選択肢専用の商品を読めませんでした: %s", store_id)

    # サイズ違いの対応表は、保存してある代表コードから組み直す
    size_groups = {c: p.size_group for c, p in products.items() if p.size_group}
    return ParsedMenu(
        store_id=store_id, products=products, collections=collections,
        extras=extras,
        size_groups=size_groups,
    )


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
