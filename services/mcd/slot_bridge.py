"""
選択枠の中間ノード

実物の注文コードを調べると、セットの選択枠は2通りの形がある。

    サイド枠    9987009 → 2020              （枠の直下に商品）
    ドリンク枠  9997918 → 9997914 → 3120    （間にもう1段ある）

この中間の `9997914` は、**メニューカタログのどこにも載っていない**。
全店舗ぶんのカタログを調べたが一度も出てこない。
注文用のAPIが別に持っている情報らしく、こちらからは導き出せない。

そこで、
  ・分かっているものは最初から持っておく（下の KNOWN）
  ・利用者が貼った注文コードから**自動で学ぶ**
    （実際に通ったコードなので、確かな情報源になる）

学んだ内容は data/slot_bridge.json に保存し、次回以降も使う。

⚠️ 中間が分からない枠は、枠の直下に商品を置く形にする。
   分からないからといって注文を止めるより、通る見込みのある形で
   送ってみるほうがよい（サイド枠はこの形で実際に通っている）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("bot.slot_bridge")

_ROOT = Path(__file__).parent.parent.parent
STORE_PATH = _ROOT / "data" / "slot_bridge.json"

# 実物の注文コードから確認できているもの
#   枠のコード → 中間のコード
KNOWN: dict[str, str] = {
    "9997918": "9997914",   # 通常セットのドリンク枠（実データ）
    "9997925": "9997922",   # 朝マックのセットのドリンク枠（実データ・2026-10）
}

# 実物の注文コードで「中間ノードが**無い**」と確認できた枠。
#
# ⚠️ これを記録しないと、「分からない枠」と「不要と分かっている枠」を
#    区別できない。区別できないと、後から誰かが（私を含めて）
#    「この枠の中間が不明だ」と思い込んで埋めようとする。
#    実際に一度、不要と確認済みの枠について利用者に実データを
#    求めてしまった（2026-10-06）。
#
# ⚠️ ここに入れるのは **枠の直下に商品が来ている実物を見たもの限定**。
#    「同じ種類の枠だから」で足してはいけない。一度それで
#    9997925 に 9997914 を当てて外した（実物は 9997922）。
NO_BRIDGE: dict[str, str] = {
    "7251": "6048",      # ナゲットのソース枠 → バーベキューソース（実データ）
    "9987009": "2020",   # セットのサイド枠 → ポテトM（実データ）
    "9987010": "5010",   # セットのサイド枠 → ハッシュポテト（実データ）
    "9987017": "3315",   # ハッピーセットのドリンク枠 → オレンジ(S)（実データ）
}

# ⚠️ 中間ノードの値に **規則は無い**。
#      9997918 → 9997914（差4）
#      9997925 → 9997922（差3）
#    一時は「同じカテゴリの枠は同じ中間ノードを使う」とみなして
#    9997925 にも 9997914 を当てていたが、実物は 9997922 だった。
#    **推測で埋めてはいけない。** 分からない枠は実物の注文コードから
#    learn_from_order() で覚えること。

_lock = threading.Lock()
_learned: dict[str, str] = {}
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
                _learned.update({str(k): str(v) for k, v in data.items() if v})
                log.info("選択枠の中間ノードを %d 件読み込みました", len(_learned))
    except (OSError, ValueError) as e:
        log.warning("選択枠の控えを読めませんでした: %s", e)


def _save() -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_learned, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(STORE_PATH)
    except OSError as e:
        log.warning("選択枠の控えを保存できませんでした: %s", e)


def bridge_for(slot_code: str) -> str:
    """
    その枠に必要な中間ノード。要らなければ空文字。
    """
    _load()
    code = str(slot_code)
    with _lock:
        return _learned.get(code) or KNOWN.get(code, "")


def learn(slot_code: str, bridge_code: str) -> bool:
    """
    実際に通った注文コードから1件おぼえる。

    すでに同じ内容を知っていれば False を返す。
    """
    _load()
    slot_code, bridge_code = str(slot_code), str(bridge_code)
    if not slot_code or not bridge_code or slot_code == bridge_code:
        return False
    with _lock:
        if _learned.get(slot_code) == bridge_code or KNOWN.get(slot_code) == bridge_code:
            return False
        _learned[slot_code] = bridge_code
        _save()
    log.info("選択枠の中間ノードをおぼえました: %s → %s", slot_code, bridge_code)
    return True


def learn_from_order(items) -> int:
    """
    解析した注文から、枠と中間の対応をまとめて学ぶ。

    「孫がいる中間ノード」＝枠の直下にあって、さらに子を持つもの、
    という形で見つける。
    """
    # 注文の形は決まっている。
    #   深さ0  商品（セット）
    #   深さ1  選択枠
    #   深さ2  中間ノード、または実際の商品
    #   深さ3  実際の商品
    #
    # 学びたいのは「深さ1の枠」→「深さ2の中間」だけ。
    # 深さ0と深さ1の関係（商品→枠）はカタログに載っているので学ばない。
    found = 0
    for slot_code, bridge_code in pairs_in_order(items):
        if learn(slot_code, bridge_code):
            found += 1
    return found


def all_known() -> dict[str, str]:
    _load()
    with _lock:
        return {**KNOWN, **_learned}


def forget(slot_code: str) -> bool:
    """おぼえた1件を消す。

    ⚠️ KNOWN（実物で確認済みのもの）は消せない。消したいのは
       「間違って覚えてしまったもの」だけで、確認済みのものを
       消す手段を用意すると、事故で動かなくなる。
    """
    _load()
    code = str(slot_code)
    with _lock:
        if code not in _learned:
            return False
        del _learned[code]
        _save()
    log.info("おぼえた中間ノードを消しました: %s", code)
    return True


def needs_no_bridge(slot_code: str) -> bool:
    """「中間ノードは不要」と実物で確認できている枠か。

    ⚠️ 「不明」とは違う。不明な枠も結果として直下に商品を置くので
       送るコードは同じだが、**不明なら調べる余地があり、確認済みなら
       調べる必要が無い**。一覧でこの2つを混ぜると、直す必要の無い
       ものを追いかけることになる。
    """
    return str(slot_code) in NO_BRIDGE


def status_of(slot_code: str) -> str:
    """枠の状況。"bridge"（中間あり）/ "direct"（中間不要）/ "unknown"。"""
    code = str(slot_code)
    if bridge_for(code):
        return "bridge"
    if code in NO_BRIDGE:
        return "direct"
    return "unknown"


def is_learned(slot_code: str) -> bool:
    """おぼえたもの（＝消せるもの）か。KNOWN なら False。"""
    _load()
    with _lock:
        return str(slot_code) in _learned


def pairs_in_order(items) -> list[tuple[str, str]]:
    """注文コードの中にある（枠, 中間）の対応を、学ばずに取り出す。

    中身を見せるだけの場面で使う。learn_from_order() と同じ見つけ方を
    するが、覚えない。

    ⚠️ 見つけ方を2か所に書くと必ずずれる。learn_from_order() は
       これを呼ぶ形にしてある。
    """
    out: list[tuple[str, str]] = []
    for top in items or []:
        for slot in getattr(top, "components", None) or []:
            for child in getattr(slot, "components", None) or []:
                # child が子を持っている＝中間ノード
                if getattr(child, "components", None):
                    pair = (str(slot.product_code), str(child.product_code))
                    if pair not in out:
                        out.append(pair)
    return out


def forget_all() -> None:
    """検証用。おぼえた内容を消す。"""
    with _lock:
        _learned.clear()
    _save()
