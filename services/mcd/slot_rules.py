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


# 実機（公式アプリ）で見て、その枠には**出てこない**と分かった商品。
#   枠コード → 出てこない商品コード
#
# ⚠️ カタログには「どれが選べるか」が書かれていない（docs/09 V-22）。
#    こちらの候補は「参照商品と同じカテゴリ全部」から出すので、
#    実機では選べないものが混ざる。選ばせれば必ず断られる。
#
# ⚠️ **実機の画面で見たものだけ入れること。** 推測で足さない。
#    下はチーズチーズ倍月見 セットのサイド枠を実機で見た結果
#    （2026-10・1店舗）。出ていたのは4つだけだった。
#        マックフライポテト / サイドサラダ /
#        チキンマックナゲット5ピース / えだまめコーン
#    エビプリオと月見パイは「ご一緒にいかがですか？」の**追加商品**
#    として別枠に出ており、セットの中身ではない。
#
# ⚠️ **1店舗で見ただけ**なので、店舗や時期で違う可能性がある。
#    だから `_confirmed`（実際に通った記録）のほうを優先する。
#    もしどこかで本当に選べたなら、その店では自動的に復活する。
KNOWN_BAD: dict[str, set[str]] = {
    # セットのサイド枠（通常セット）
    "9987009": {
        "2080",   # シャカチキ
        "1670",   # チキンマックナゲット® 15ピース
        "2081",   # プリプリエビプリオ 5ピース（追加商品として別枠に出る）
        "2255",   # 北海道産バターとあんことおもちの月見パイ（同上）
    },
}


def _key(store_id: str, slot_code: str, product_code: str) -> str:
    return f"{store_id}/{slot_code}/{product_code}"


def reload() -> None:
    """ファイルから読み直す（復元の直後に呼ぶ）。

    ⚠️ 復元で slot_rules.json を戻しても、プロセス内のキャッシュは
       古いまま。これを呼ばないと再起動するまで反映されない。
    """
    global _loaded
    with _lock:
        _loaded = False
    load()


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
        # ⚠️ 実際に通った記録がいちばん強い。実機で見えなかった物でも、
        #    その店で本当に通ったなら出してよい（店舗差・時期差がある）。
        if k in _confirmed:
            return True
        if k in _rejected:
            return False
    # 実機で「その枠には出てこない」と分かっているもの
    return str(product_code) not in KNOWN_BAD.get(str(slot_code), ())


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


def known_bad(store_id: str, slot_code: str, product_code: str) -> bool:
    """すでに「入れられない」と覚えている組み合わせか。"""
    if not _loaded:
        load()
    k = _key(str(store_id), str(slot_code), str(product_code))
    with _lock:
        return k in _rejected


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


def _leaves(node) -> list[str]:
    """その節の下にある一番奥の商品コード（複数ありうる）。"""
    kids = getattr(node, "components", None) or []
    if not kids:
        code = str(getattr(node, "product_code", ""))
        return [code] if code else []
    out: list[str] = []
    for k in kids:
        out.extend(_leaves(k))
    return out


def choices_of(item) -> list[tuple[str, str]]:
    """
    注文1品から (枠コード, 選んだ商品コード) を取り出す。

    形は3通りある。

        枠 → 商品                     サイド枠 9987009 → 2020
        枠 → 中間 → 商品              ドリンク枠 9997918 → 9997914 → 3120
        構成品 → 枠 → 商品            ポテナゲの中のナゲット → ソース枠 7251 → 6048

    ⚠️ 3つめ（入れ子）を **構成品のコードを枠として** 報告していたため、
       ポテナゲのソースの可否を学習しても引けず、カートの未選択検査も
       すり抜けていた。中間ノードと構成品を見分けて、本当の枠を返す。
    """
    out: list[tuple[str, str]] = []

    bridges = _bridges()

    def is_slot(node) -> bool:
        """
        その節が「枠」か。構成品と枠は、下にあるものの形で見分ける。

            枠      … 下は商品だけ、または中間ノードだけ
            構成品  … 下に **枠** がある（ポテナゲの中のナゲット）
        """
        for k in getattr(node, "components", None) or []:
            if (getattr(k, "components", None) or []) and str(
                getattr(k, "product_code", "")
            ) not in bridges:
                return False
        return True

    def walk(node, depth: int) -> None:
        kids = getattr(node, "components", None) or []
        code = str(getattr(node, "product_code", ""))
        if not kids:
            return
        # depth 0 は商品そのもの。中間ノードは枠ではない。
        if depth > 0 and code and code not in bridges and is_slot(node):
            for leaf in _leaves(node):
                out.append((code, leaf))
        for k in kids:
            walk(k, depth + 1)

    walk(item, 0)
    # 同じ組み合わせは1回だけ
    return list(dict.fromkeys(out))


def _bridges() -> set[str]:
    """枠と商品の間に入る中間ノードのコード（枠ではない）。"""
    try:
        from services.mcd import slot_bridge
        return set(slot_bridge.all_known().values())
    except Exception:          # 読めなくても判定は続ける
        return set()
