"""
参照商品が載っていない選択枠の手がかり

カタログの選択枠はふつう `referenceProduct`（代表商品）を持っていて、
そこから候補を引ける。ところが **一部の枠は全商品で空** になっている。

    9987016  ハッピーセットのドリンク枠（通常）
    9987017  ハッピーセットのドリンク枠（朝マック）

この2つは、ハッピーセット8商品すべてで参照・既定がどちらも空。
さらに構成品に入っている `2981` は、カタログのどこにも定義が無い
（名前もグループもサイズ違いも無し）。つまり **カタログだけでは
何の枠か導き出せない**。

公式アプリでは、この枠でドリンクを選ぶ。そこで

  ・分かっているものは代表商品を手で持っておく（下の HINTS）
  ・確実に選べると分かっているものを先に並べる（prefer）
  ・それでも断られた組み合わせは slot_rules が覚えて、次から出さない

という形にした。推測を含むので、**断られても注文が壊れない**
作りにしてある（必須の枠が空のまま送るより確実に良い）。

管理者が手で直したいときは data/slot_hints.json を置けば上書きできる。

    {"9987016": {"reference": "3480", "prefer": ["3480", "3315"]}}
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("bot.slot_hints")

_ROOT = Path(__file__).parent.parent.parent
STORE_PATH = _ROOT / "data" / "slot_hints.json"

# ハッピーセットのドリンク枠。
#   reference … 候補の母集団を引くための代表商品（ドリンクのコレクション）
#   prefer    … 実際のハッピーセットで選べるもの。先頭に並べる。
_HAPPY_SET_DRINK = {
    "reference": "3480",            # ミルク（ハッピーセットで実際に選べる）
    "prefer": [
        "3480",                     # ミルク
        "3315",                     # ミニッツメイド® オレンジ(S)
        "3922",                     # ミニッツメイド アップル100
        "3918",                     # 野菜生活100 M
    ],
    "note": "ハッピーセットのドリンク",
}

HINTS: dict[str, dict] = {
    "9987016": dict(_HAPPY_SET_DRINK),      # 通常のハッピーセット
    "9987017": dict(_HAPPY_SET_DRINK),      # 朝マックのハッピーセット
}

_lock = threading.Lock()
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if STORE_PATH.exists():
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for code, v in data.items():
                    if isinstance(v, dict):
                        HINTS[str(code)] = v
                log.info("選択枠の手がかりを %d 件読み込みました", len(data))
    except (OSError, ValueError) as e:
        log.warning("選択枠の手がかりを読めませんでした: %s", e)


def reference_for(slot_code: str) -> str:
    """その枠の代表商品。分からなければ空文字。"""
    with _lock:
        _load()
        return str(HINTS.get(str(slot_code), {}).get("reference") or "")


def preferred(slot_code: str) -> list[str]:
    """先に並べる商品。確実に選べると分かっているもの。"""
    with _lock:
        _load()
        v = HINTS.get(str(slot_code), {}).get("prefer") or []
        return [str(x) for x in v if x]


def note_for(slot_code: str) -> str:
    """枠の呼び名（画面に出す）。"""
    with _lock:
        _load()
        return str(HINTS.get(str(slot_code), {}).get("note") or "")


def all_known() -> dict[str, dict]:
    with _lock:
        _load()
        return {k: dict(v) for k, v in HINTS.items()}


def reset() -> None:
    """テスト用。読み込み済みの印を外す。"""
    global _loaded
    with _lock:
        _loaded = False
