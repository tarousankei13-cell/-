"""
セットの選択枠に入れられる商品の、学習した一覧

⚠️ **カタログには「どれが選べるか」が書かれていない。**
   menu.json の choices には referenceProduct と defaultProduct しか無く、
   grills / relatedSets / collections / sizeVariants / canAdds /
   limitedAbility / groupMenu のどれにも、枠ごとの候補一覧は無い。
   （2026-10 時点で全項目を確認済み）

   そのため候補は「参照商品が属するカテゴリ全部」から出しているが、
   その中にはその枠に入れられないものが混ざる。
   例: サイドメニューには ¥50 のソース類が入っているが、
       セットのサイドとしては選べない。

   どれがダメかを決め打ちで書くと、こちらの思い込みで
   選べるはずのものまで消してしまう。そこで、
   **実際に断られた組み合わせを覚えて、次から出さない**ようにする。

   ⚠️ 時間帯の問題と取り違えないこと。こちらの時間判定が通っていたのに
      マクドナルドが「時間外」と言ってきた場合**だけ**、
      組み合わせの問題として記録する。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("bot.slot_rules")

STORE_PATH = Path("data/slot_rules.json")

# 断られた組み合わせ: "店舗/枠/商品" の集合
_rejected: set[str] = set()
# 通った組み合わせ。断られた記録より優先する（店側の一時的な都合もあるため）
_confirmed: set[str] = set()
_lock = threading.Lock()
_loaded = False


def _key(store_id: str, slot_code: str, product_code: str) -> str:
    return f"{store_id}/{slot_code}/{product_code}"


def load() -> None:
    """保存してある学習結果を読む。壊れていても起動を止めない。"""
    global _loaded
    with _lock:
        _loaded = True
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        _rejected.clear()
        _confirmed.clear()
        _rejected.update(str(x) for x in data.get("rejected", []))
        _confirmed.update(str(x) for x in data.get("confirmed", []))
    log.info(
        "選択枠の学習結果を読み込みました（断られた %d 件 / 通った %d 件）",
        len(_rejected), len(_confirmed),
    )


def _save() -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(
            json.dumps(
                {"rejected": sorted(_rejected), "confirmed": sorted(_confirmed)},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        log.warning("選択枠の学習結果を保存できませんでした")


def allowed(store_id: str, slot_code: str, product_code: str) -> bool:
    """その枠にその商品を出してよいか。"""
    if not _loaded:
        load()
    k = _key(str(store_id), str(slot_code), str(product_code))
    with _lock:
        if k in _confirmed:
            return True
        return k not in _rejected


def reject(store_id: str, slot_code: str, product_code: str) -> bool:
    """
    断られた組み合わせとして覚える。新しく覚えたら True。

    ⚠️ 一度通ったことがある組み合わせは覚えない。
       売り切れなど、その時だけの事情で断られることがある。
    """
    if not _loaded:
        load()
    k = _key(str(store_id), str(slot_code), str(product_code))
    with _lock:
        if k in _confirmed or k in _rejected:
            return False
        _rejected.add(k)
        _save()
    log.info("選択枠に入れられない組み合わせを覚えました: %s", k)
    return True


def confirm(store_id: str, slot_code: str, product_code: str) -> None:
    """通った組み合わせとして覚える。以後は断られた記録より優先する。"""
    if not _loaded:
        load()
    k = _key(str(store_id), str(slot_code), str(product_code))
    with _lock:
        if k in _confirmed:
            return
        _confirmed.add(k)
        _rejected.discard(k)
        _save()


def forget(store_id: str, slot_code: str = "", product_code: str = "") -> int:
    """学習結果を消す。管理コマンドから使う。消した件数を返す。"""
    if not _loaded:
        load()
    prefix = _key(str(store_id), str(slot_code), str(product_code)).rstrip("/")
    with _lock:
        gone = [k for k in (_rejected | _confirmed) if k.startswith(prefix)]
        for k in gone:
            _rejected.discard(k)
            _confirmed.discard(k)
        if gone:
            _save()
    return len(gone)


def summary() -> dict[str, int]:
    if not _loaded:
        load()
    with _lock:
        return {"rejected": len(_rejected), "confirmed": len(_confirmed)}


def choices_of(item) -> list[tuple[str, str]]:
    """
    注文1品から (枠コード, 選んだ商品コード) を取り出す。

    枠 →（中間ノード）→ 商品 と入れ子になっているので、
    一番奥の商品まで辿る。
    """
    out: list[tuple[str, str]] = []
    for comp in getattr(item, "components", None) or []:
        slot = str(getattr(comp, "product_code", ""))
        inner = getattr(comp, "components", None) or []
        if not inner:
            continue        # 具材の調整（選択枠ではない）
        leaf = inner[0]
        while getattr(leaf, "components", None):
            leaf = leaf.components[0]
        chosen = str(getattr(leaf, "product_code", ""))
        if slot and chosen:
            out.append((slot, chosen))
    return out
