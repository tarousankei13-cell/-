"""
店舗一覧の定期同期

マクドナルドには店舗一覧APIが無いため、公式の店舗検索サイトが
公開しているサイトマップから店舗IDの一覧を取り、
各店舗の詳細を配信元（data.cat）から取り込む。

    サイトマップ          → 存在する店舗IDの一覧（3,000件強）
    data.cat/<ID>.json → 店名・住所・グループ・モバイルオーダー可否

新店舗の開店・閉店・店名変更・モバイルオーダー対応の切り替えを
自動で反映するため、次の2段構えで回す。

  1. サイトマップの照合（既定60分ごと）
     IDの増減を調べる。増えた分だけ詳細を取りに行く。
  2. 詳細の巡回更新（毎回）
     最後に確認してから一番古い店舗から順に、既定400件ずつ取り直す。
     ETag を使うので中身が変わっていなければ 304（0バイト）で済む。
     全3,000店舗を約2時間で一周する。

書き出し先は data/stores.json。同梱の assets/stores.json より
優先して読まれる（store_index.default_path）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

import httpx

import config
from core.http import catalog_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
from services.mcd import store_index

log = logging.getLogger("bot.store_sync")

SITEMAP_URLS = [
    "https://map.mcdonalds.co.jp/sitemap.xml",
    "https://map.mcdonalds.co.jp/sitemap_fp.xml",
]
_ID_RE = re.compile(r"/map/(\d{4,6})")
_UA = "Mozilla/5.0 (compatible; McdOrderBot/1.0)"

# 同時接続数。相手に負荷をかけない範囲で。
CONCURRENCY = 20

# サイトマップには載っているが配信元に無いID（閉店直後など）を
# 毎回6グループ総当たりすると無駄なので、間隔をあけて再挑戦する。
UNRESOLVED_RETRY_HOURS = 24


@dataclass
class SyncReport:
    """同期の結果。管理者への通知に使う。"""
    added: dict[str, str] = field(default_factory=dict)      # ID → 店名
    removed: dict[str, str] = field(default_factory=dict)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)  # ID, 旧, 新
    mop_on: dict[str, str] = field(default_factory=dict)     # モバイルオーダー対応になった
    mop_off: dict[str, str] = field(default_factory=dict)    # 対応しなくなった
    checked: int = 0
    total: int = 0
    sitemap_checked: bool = False
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(
            self.added or self.removed or self.renamed or self.mop_on or self.mop_off
        )


def _now() -> int:
    return int(time.time())


def _load_current() -> tuple[dict, dict]:
    """
    いま使っている一覧を読む。

    同期済みの一覧が無ければ同梱の一覧から始める。
    同梱の一覧には ETag も確認時刻も入っていないので、
    すべて「未確認」として扱い、巡回更新で順に埋めていく。
    """
    path = store_index.SYNCED_PATH
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw.get("stores"), dict):
                return raw["stores"], raw.get("meta") or {}
            return raw, {}
        except (OSError, ValueError) as e:
            log.warning("同期済みの一覧を読めませんでした（同梱の一覧から作り直します）: %s", e)

    try:
        raw = json.loads(store_index.BUNDLED_PATH.read_text(encoding="utf-8"))
        stores = raw.get("stores") if isinstance(raw.get("stores"), dict) else raw
        return dict(stores or {}), {}
    except (OSError, ValueError) as e:
        log.warning("同梱の一覧も読めませんでした: %s", e)
        return {}, {}


def _save(stores: dict, meta: dict) -> None:
    """一時ファイルに書いてから差し替える（途中で壊れた一覧が残らないように）。"""
    path = store_index.SYNCED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"meta": meta, "stores": stores}
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp, path)


async def fetch_sitemap_ids(client: httpx.AsyncClient) -> tuple[set[str], str]:
    """
    サイトマップから店舗IDを集める。

    サイトマップは ETag を返さないため、中身のハッシュで変化を見る。
    取得できなかった場合は空を返す（呼び出し側で既存の一覧を保つ）。
    """
    ids: set[str] = set()
    digest = hashlib.sha256()
    for url in SITEMAP_URLS:
        r = await client.get(url, headers={"User-Agent": _UA}, timeout=60)
        r.raise_for_status()
        body = r.text
        digest.update(body.encode("utf-8", "replace"))
        ids |= set(_ID_RE.findall(body))
    return ids, digest.hexdigest()


async def fetch_store(
    client: httpx.AsyncClient, store_id: str, *, group: str = "", etag: str = ""
) -> tuple[dict | None, str, str, bool]:
    """
    店舗の詳細を取る。

    戻り値は (詳細, グループ, ETag, 変化なしか)。
    グループが分かっている場合はそこだけを見る（1回の通信で済む）。
    分からない場合だけ総当たりする。店舗はどれか1つのグループにしかない。

    戻り値の「変化なし」が True のときは詳細が None になる（304）。
    """
    groups = [group] if group else config.MCD_GROUPS
    for g in groups:
        url = f"https://data.cat.{g}.prod.mop.mcd.qorcommerce.com/{store_id}.json"
        headers = {"If-None-Match": etag} if (etag and g == group) else {}
        try:
            r = await client.get(url, headers=headers, timeout=20)
        except httpx.HTTPError:
            continue
        if r.status_code == 304:
            return None, g, etag, True
        if r.status_code != 200:
            continue
        try:
            data = (r.json() or {}).get("store") or {}
        except ValueError:
            continue
        if data.get("name"):
            return data, g, r.headers.get("etag", ""), False
    return None, "", "", False


def _entry(data: dict, group: str, etag: str) -> dict:
    return {
        "n": data.get("name", ""),
        "a": data.get("address", ""),
        "g": group,
        "mop": bool(data.get("mopEnabled")),
        "e": etag,
        "c": _now(),
    }


async def sync(
    *,
    full: bool = False,
    batch: int | None = None,
    progress: "Callable[[int, int, int], Awaitable[None]] | None" = None,
) -> SyncReport:
    """
    一覧を同期する。

    full=True なら全店舗を取り直す（管理者の手動再構築用）。
    それ以外は、サイトマップの照合（間隔が来ていれば）と
    巡回更新をまとめて行う。

    progress は (確認した件数, 対象の件数, 現在の店舗数) で呼ばれる。
    """
    report = SyncReport()
    stores, meta = _load_current()
    report.total = len(stores)

    batch = batch or int(
        config.STORE_INDEX_REFRESH_BATCH if not full else 10**9
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    # 共用の接続を借りる。毎回作り直すとそのたびにTLSの handshake が起きる。
    async with catalog_session() as client:
        # ---- ① サイトマップの照合 ----------------------------
        interval = int(config.STORE_SITEMAP_CHECK_MINUTES) * 60
        due = full or (_now() - int(meta.get("sitemap_at") or 0) >= interval)
        sitemap_ids: set[str] = set()
        if due:
            try:
                sitemap_ids, digest = await fetch_sitemap_ids(client)
            except Exception as e:
                # ここで失敗しても、既にある店舗の巡回更新は続ける。
                # 店舗IDの照合ができないだけで、一覧が使えなくなるわけではない。
                report.error = f"サイトマップを取得できませんでした: {e}"
                log.warning(report.error)
            else:
                report.sitemap_checked = True
                meta["sitemap_at"] = _now()
                if sitemap_ids and digest != meta.get("sitemap_hash"):
                    meta["sitemap_hash"] = digest
                    # 閉店した店舗を外す
                    for sid in [s for s in stores if s not in sitemap_ids]:
                        report.removed[sid] = stores.pop(sid).get("n", "")
                    log.info(
                        "サイトマップが更新されました（%d件 / 新規候補 %d件）",
                        len(sitemap_ids), len(sitemap_ids - set(stores)),
                    )

        # ---- ② 取りに行く店舗を決める ------------------------
        # 新しいIDが先。次に、最後に確認してから一番古いもの。
        unresolved: dict = dict(meta.get("unresolved") or {})
        cutoff = _now() - UNRESOLVED_RETRY_HOURS * 3600
        new_ids = [
            s for s in sorted(sitemap_ids - set(stores))
            if full or int(unresolved.get(s) or 0) < cutoff
        ] if sitemap_ids else []
        known = sorted(stores, key=lambda s: int(stores[s].get("c") or 0))
        targets = new_ids + [s for s in known if s not in new_ids]
        targets = targets[:batch]

        async def work(sid: str) -> None:
            async with sem:
                before = stores.get(sid)
                data, group, etag, unchanged = await fetch_store(
                    client, sid,
                    group="" if full or not before else (before.get("g") or ""),
                    etag="" if full else (before.get("e") or "" if before else ""),
                )
                report.checked += 1

                if unchanged:
                    # 中身は同じ。確認時刻だけ進めて、次の巡回では後回しにする。
                    before["c"] = _now()
                    return

                if data is None:
                    # 取れなかった。既にある店舗はそのまま残す
                    # （一時的な通信failureで一覧から消さないため）。
                    if before is not None:
                        before["c"] = _now()
                    else:
                        # 未登録のIDが取れない＝配信元に無い。
                        # しばらく再挑戦しないよう控えておく。
                        unresolved[sid] = _now()
                    return

                fresh = _entry(data, group, etag)
                if before is None:
                    stores[sid] = fresh
                    report.added[sid] = fresh["n"]
                    return

                if before.get("n") and fresh["n"] != before["n"]:
                    report.renamed.append((sid, before["n"], fresh["n"]))
                if fresh["mop"] and not before.get("mop"):
                    report.mop_on[sid] = fresh["n"]
                elif not fresh["mop"] and before.get("mop"):
                    report.mop_off[sid] = fresh["n"]
                stores[sid] = fresh

        async def work_and_report(sid: str) -> None:
            await work(sid)
            if progress and report.checked % 50 == 0:
                try:
                    await progress(report.checked, len(targets), len(stores))
                except Exception:
                    log.debug("進捗の通知に失敗しました", exc_info=True)

        await asyncio.gather(*[work_and_report(s) for s in targets])

    # 取れるようになったIDは控えから外す
    for sid in list(unresolved):
        if sid in stores:
            unresolved.pop(sid, None)
    meta["unresolved"] = unresolved
    meta["updated_at"] = _now()
    meta["count"] = len(stores)
    report.total = len(stores)
    _save(stores, meta)
    store_index.load_index(store_index.SYNCED_PATH)
    log.info(
        "店舗一覧を同期しました: %d店舗（確認 %d件 / 追加 %d / 削除 %d / 改名 %d）",
        len(stores), report.checked, len(report.added), len(report.removed),
        len(report.renamed),
    )
    return report


def format_report(r: SyncReport) -> str:
    """管理者への通知文。変化が無ければ空文字を返す。"""
    if not r.changed:
        return ""
    lines = []
    if r.added:
        lines.append("**新しく開店した店舗**")
        lines += [f"　+ {n}（`{i}`）" for i, n in list(r.added.items())[:15]]
        if len(r.added) > 15:
            lines.append(f"　…ほか {len(r.added) - 15} 店舗")
    if r.removed:
        lines.append("**一覧から外れた店舗**")
        lines += [f"　− {n}（`{i}`）" for i, n in list(r.removed.items())[:15]]
        if len(r.removed) > 15:
            lines.append(f"　…ほか {len(r.removed) - 15} 店舗")
    if r.renamed:
        lines.append("**店名が変わった店舗**")
        lines += [f"　{o} → {n}（`{i}`）" for i, o, n in r.renamed[:15]]
    if r.mop_on:
        lines.append("**モバイルオーダーに対応した店舗**")
        lines += [f"　{n}（`{i}`）" for i, n in list(r.mop_on.items())[:10]]
    if r.mop_off:
        lines.append("**モバイルオーダーを停止した店舗**")
        lines += [f"　{n}（`{i}`）" for i, n in list(r.mop_off.items())[:10]]
    lines.append(f"\n現在 **{r.total}** 店舗")
    return "\n".join(lines)
