"""
店舗インデックス

店舗名の一部から店舗を探せるようにする。
マクドナルドには店舗一覧のAPIが無いため、あらかじめ作っておいた
一覧（assets/stores.json）を同梱して検索に使う。

一覧が無い場合や、一覧に載っていない新店舗の場合でも、
店舗IDを直接入れれば注文できる（そちらは常に動く）。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bot.store_index")

_ROOT = Path(__file__).parent.parent.parent
# 同梱の一覧。更新されない代わりに必ず存在する。
BUNDLED_PATH = _ROOT / "assets" / "stores.json"
# 定期同期で書き出す一覧。こちらがあれば優先する。
SYNCED_PATH = _ROOT / "data" / "stores.json"


def default_path() -> Path:
    """同期済みの一覧があればそれを、無ければ同梱の一覧を使う。"""
    return SYNCED_PATH if SYNCED_PATH.exists() else BUNDLED_PATH


# 後方互換（従来の名前で参照している箇所のため）
INDEX_PATH = BUNDLED_PATH

# 検索時に無視する文字（中黒・記号・空白など）
_NOISE = re.compile(r"[\s　・･\-ー－―‐_,.。、（）()\[\]【】]+")


@dataclass
class StoreEntry:
    store_id: str
    name: str
    address: str
    group: str = ""
    mop_enabled: bool = True

    @property
    def label(self) -> str:
        return f"{self.name}（{self.store_id}）"


def normalize(text: str) -> str:
    """
    検索用に文字をそろえる。

    ・全角/半角、カタカナの濁点などを統一（NFKC）
    ・ひらがな → カタカナ（「まつもと」で「マツモト」も当たるように）
    ・空白や中黒を除去（「ＡＫＩＢＡ 店」でも当たるように）
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _NOISE.sub("", t)
    # ひらがなをカタカナへ寄せる
    return "".join(
        chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in t
    )


class StoreIndex:
    def __init__(self) -> None:
        self._entries: list[StoreEntry] = []
        self._by_id: dict[str, StoreEntry] = {}
        self._norm_name: dict[str, str] = {}
        self._norm_addr: dict[str, str] = {}
        self._meta: dict = {}

    @property
    def meta(self) -> dict:
        """最後に同期した時刻などの付帯情報。"""
        return self._meta

    @property
    def available(self) -> bool:
        return bool(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    def _clear(self) -> None:
        self._entries.clear()
        self._by_id.clear()
        self._norm_name.clear()
        self._norm_addr.clear()
        self._meta = {}

    def load(self, path: Path | None = None) -> int:
        """
        一覧を読み込む。

        読み込めなかった場合は中身を空にする。
        古い内容が残っていると「検索できるのに結果がおかしい」という
        分かりにくい状態になるため。
        """
        path = path or default_path()
        if not path.exists():
            self._clear()
            log.warning(
                "店舗インデックスがありません（%s）。"
                "店名での検索は、過去に使った店舗のみになります", path
            )
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._clear()
            log.error("店舗インデックスを読み込めませんでした: %s", e)
            return 0

        # 先に新しい内容を組み立ててから一気に差し替える。
        # 読み込み中に検索されても、古い内容のまま正しく動く。
        entries: list[StoreEntry] = []
        by_id: dict[str, StoreEntry] = {}
        norm_name: dict[str, str] = {}
        norm_addr: dict[str, str] = {}
        # 同期で書き出した一覧は {"meta": {...}, "stores": {...}} の形。
        # 同梱の一覧は店舗IDをそのまま並べた形。どちらも読めるようにする。
        if isinstance(raw, dict) and isinstance(raw.get("stores"), dict):
            self._meta = raw.get("meta") or {}
            raw = raw["stores"]
        else:
            self._meta = {}

        for store_id, d in raw.items():
            entry = StoreEntry(
                store_id=str(store_id),
                name=d.get("n") or d.get("name") or "",
                address=d.get("a") or d.get("address") or "",
                group=d.get("g") or d.get("group") or "",
                mop_enabled=bool(d.get("mop", True)),
            )
            if not entry.name:
                continue
            entries.append(entry)
            by_id[entry.store_id] = entry
            norm_name[entry.store_id] = normalize(entry.name)
            norm_addr[entry.store_id] = normalize(entry.address)

        self._entries, self._by_id = entries, by_id
        self._norm_name, self._norm_addr = norm_name, norm_addr
        log.info("店舗インデックスを読み込みました: %d 店舗", len(self._entries))
        return len(self._entries)

    def get(self, store_id: str) -> StoreEntry | None:
        """
        店舗IDで引く。

        店舗IDは5桁のゼロ埋め（北海道なら 01003）。
        利用者が先頭の 0 を省いて「1003」と入れることがあるので、
        そのままで見つからなければゼロ埋めしても探す。
        """
        sid = str(store_id).strip()
        hit = self._by_id.get(sid)
        if hit is None and sid.isdigit() and len(sid) < 5:
            hit = self._by_id.get(sid.zfill(5))
        return hit

    def search(self, query: str, limit: int = 25) -> list[StoreEntry]:
        """
        店名・住所の一部から探す。

        当たりやすい順に並べる:
          1. 店舗IDそのもの
          2. 店名が完全一致
          3. 店名が前方一致
          4. 店名に含まれる
          5. 住所に含まれる
        """
        q = normalize(query)
        if not q:
            return []

        # 数字だけならIDとして扱う
        digits = query.strip()
        if digits.isdigit():
            hit = self.get(digits)
            if hit:
                return [hit]

        scored: list[tuple[int, int, StoreEntry]] = []
        for e in self._entries:
            name = self._norm_name.get(e.store_id, "")
            addr = self._norm_addr.get(e.store_id, "")
            if name == q:
                score = 0
            elif name.startswith(q):
                score = 1
            elif q in name:
                score = 2
            elif q in addr:
                score = 3
            elif q in e.store_id:
                score = 4
            else:
                continue
            # 同じ点数なら、名前が短い＝より的確なものを上に
            scored.append((score, len(name), e))

        scored.sort(key=lambda x: (x[0], x[1], x[2].name))
        return [e for _, _, e in scored[:limit]]


_index = StoreIndex()


def load_index(path: Path | None = None) -> int:
    return _index.load(path)


def get_index() -> StoreIndex:
    return _index


def search(query: str, limit: int = 25) -> list[StoreEntry]:
    return _index.search(query, limit)


def get(store_id: str) -> StoreEntry | None:
    return _index.get(store_id)


def available() -> bool:
    return _index.available


def meta() -> dict:
    return _index.meta


def count() -> int:
    return _index.count
