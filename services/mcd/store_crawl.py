"""
店舗一覧の作り直し

マクドナルドには店舗一覧のAPIが無いため、店舗IDを順に当たって一覧を作る。
BOTには作成済みの一覧を同梱しているので、通常は実行する必要はない。

新店舗が増えたときや、一覧が古くなったときだけ使う。
相手のサーバーに負担をかけないよう、同時接続数は控えめにしている。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("bot.store_crawl")

GROUPS = ["group-f", "group-h", "group-g", "group-e"]
ID_START, ID_END = 10000, 48000
CONCURRENCY = 10          # 控えめにする（相手のサーバーに配慮）
INDEX_PATH = Path(__file__).parent.parent.parent / "assets" / "stores.json"

ProgressCallback = Callable[[int, int, int], Awaitable[None]]  # (処理済, 全体, 発見数)


async def crawl(
    *,
    progress: ProgressCallback | None = None,
    id_start: int = ID_START,
    id_end: int = ID_END,
    concurrency: int = CONCURRENCY,
) -> dict[str, dict]:
    """店舗IDを走査して一覧を作る。"""
    hit_counter: Counter[str] = Counter()
    found: dict[str, dict] = {}
    total = id_end - id_start
    done = 0

    def group_order() -> list[str]:
        """よく当たるグループから試す（無駄なリクエストを減らす）"""
        ranked = [g for g, _ in hit_counter.most_common()]
        return ranked + [g for g in GROUPS if g not in ranked]

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(client: httpx.AsyncClient, sid: int) -> None:
        nonlocal done
        async with sem:
            for g in group_order():
                try:
                    r = await client.get(
                        f"https://data.cat.{g}.prod.mop.mcd.qorcommerce.com/{sid}.json",
                        timeout=12,
                    )
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    d = (r.json() or {}).get("store") or {}
                except ValueError:
                    break
                if d.get("name"):
                    hit_counter[g] += 1
                    found[str(sid)] = {
                        "n": d.get("name", ""),
                        "a": d.get("address", ""),
                        "g": g,
                        "mop": bool(d.get("mopEnabled")),
                    }
                break
            done += 1
            if progress and done % 1000 == 0:
                await progress(done, total, len(found))

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=concurrency + 10)
    ) as client:
        await asyncio.gather(*[fetch_one(client, i) for i in range(id_start, id_end)])

    log.info("店舗一覧を作成しました: %d 店舗", len(found))
    return found


def save_index(stores: dict[str, dict], path: Path | None = None) -> int:
    """一覧をファイルへ保存する。返り値はバイト数。"""
    path = path or INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(stores, ensure_ascii=False, separators=(",", ":"))
    path.write_text(payload, encoding="utf-8")
    return len(payload.encode("utf-8"))
