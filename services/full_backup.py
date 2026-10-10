"""まるごとバックアップと、項目を選んでの復元

既存の services/backup.py は**DBファイルをそのまま**保存するだけだった。
それだと次の2つが抜ける。

  ・学習した知識（data/slot_bridge.json / slot_rules.json）
  ・店舗の一覧（data/stores.json）

抜けたまま復元すると、覚えた中間ノードや「選べない組み合わせ」が
ゼロに戻る。ここでは**DBの中身＋ファイル＋（任意で）暗号化キー**を
1つの書庫（zip）にまとめ、復元時に**区分を選んで**戻せるようにする。

⚠️ 暗号化キー（data/encryption_key.txt）は、含めると1ファイルで
   丸ごと引っ越せるが、**その書庫が漏れると登録アカウントの認証情報が
   全部読める**。既定では入れない。入れるときは呼び出し側で warn を出す。

⚠️ 区分は db/models の全テーブルを**漏れなく**割り当てる。
   1つでも抜けると、全体復元のつもりで消えるテーブルが出る。
   テストで「全テーブルがどこかの区分に入る」ことを必ず確かめる。
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from db.models import Base
from db.session import session_scope

log = logging.getLogger("bot.full_backup")

_ROOT = Path(__file__).parent.parent
_DATA = _ROOT / "data"

# この書庫の形式。読み込み側が食い違いに気づけるように番号を持つ。
FORMAT = 2

# ─────────────────────────────────────────────────────────
#  区分：どのテーブル／ファイルが、どの区分に属するか
# ─────────────────────────────────────────────────────────
#
# ⚠️ **全テーブルを漏れなく割り当てること。** 抜けると、全体復元の
#    つもりで消えるテーブルが出る。tests/test_full_backup.py が
#    「db/models の全テーブルがどこかの区分に入る」ことを確かめる。

CATEGORY_LABEL = {
    "settings": "設定（補助率・上限・チャンネルなど）",
    "users":    "利用者と残高",
    "orders":   "注文履歴",
    "mcd":      "マクドナルドのアカウント",
    "charge":   "チャージ口座（Kyash・PayPay）",
    "menu":     "店舗一覧・メニュー",
    "server":   "サーバー管理（チケット・認証・処分・声かけ）",
}
CATEGORY_ORDER = ("settings", "users", "orders", "mcd", "charge", "menu", "server")

CATEGORY_TABLES: dict[str, set[str]] = {
    "settings": {"bot_config", "subsidy_rules"},
    "users":    {"users", "ledger", "carts"},
    "orders":   {"orders", "order_events", "refunds"},
    "mcd":      {"mcd_accounts", "mcd_tokens", "mcd_account_events"},
    "charge":   {"kyash_accounts", "kyash_receipts",
                 "paypay_accounts", "paypay_receipts"},
    "menu":     {"store_cache", "store_dayparts",
                 "menu_products", "menu_collections"},
    "server":   {"tickets", "verifications", "guard_hits", "mod_warnings",
                 "panels", "nudges", "invites", "invite_links",
                 "invite_payouts", "audit_log"},
}

# 学習ファイル。どの区分と一緒に入れるか。
#   （覚えた中間ノードと選べない組み合わせはメニューの知識なので "menu"）
CATEGORY_FILES: dict[str, list[str]] = {
    "menu": ["slot_bridge.json", "slot_rules.json", "stores.json"],
}


def _table_to_category() -> dict[str, str]:
    out = {}
    for cat, tabs in CATEGORY_TABLES.items():
        for t in tabs:
            out[t] = cat
    return out


def check_coverage() -> list[str]:
    """区分に入っていないテーブルの一覧。空ならOK。

    ⚠️ 起動時やテストで呼ぶ。新しいテーブルを足したのに区分へ
       入れ忘れると、ここに出る。出たまま配らない。
    """
    all_tables = set(Base.metadata.tables)
    covered = set(_table_to_category())
    return sorted(all_tables - covered)


@dataclass
class BackupResult:
    name: str
    data: bytes
    tables: int
    rows: int
    files: list[str]
    included_key: bool

    @property
    def size(self) -> int:
        return len(self.data)


def _json_default(o):
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, bytes):
        # 暗号化済みの列など。失わないよう16進で持つ
        return {"__bytes__": o.hex()}
    return str(o)


async def _dump_table(name: str) -> list[dict]:
    """1テーブルを辞書の配列にする。"""
    async with session_scope() as s:
        res = await s.execute(text(f"SELECT * FROM {name}"))
        cols = list(res.keys())
        return [dict(zip(cols, row)) for row in res.fetchall()]


async def make_backup(*, include_key: bool = False) -> BackupResult:
    """まるごとバックアップを作る。

    ⚠️ include_key=True のときだけ暗号化キーを同梱する。既定は入れない。
       呼び出し側は True のとき必ず警告を出すこと。
    """
    missing = check_coverage()
    if missing:
        # ⚠️ 区分漏れのまま作ると、その区分は復元で消える。作らせない。
        raise RuntimeError(
            "区分に割り当てられていないテーブルがあります: "
            + ", ".join(missing)
            + "。services/full_backup.py の CATEGORY_TABLES を直してください。"
        )

    t2c = _table_to_category()
    tables_data: dict[str, list[dict]] = {}
    total_rows = 0
    for table in sorted(Base.metadata.tables):
        rows = await _dump_table(table)
        tables_data[table] = rows
        total_rows += len(rows)

    now = datetime.now(timezone.utc)
    manifest = {
        "format": FORMAT,
        "created_at": now.isoformat(),
        "categories": {
            cat: {
                "label": CATEGORY_LABEL[cat],
                "tables": sorted(CATEGORY_TABLES[cat]),
                "files": CATEGORY_FILES.get(cat, []),
                "rows": sum(len(tables_data.get(t, [])) for t in CATEGORY_TABLES[cat]),
            }
            for cat in CATEGORY_ORDER
        },
        "table_category": t2c,
        "includes_key": False,
    }

    files_in: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # テーブルごとに1ファイル（区分ごとに取り出せるように）
        for table, rows in tables_data.items():
            z.writestr(
                f"tables/{table}.json",
                json.dumps(rows, ensure_ascii=False, default=_json_default),
            )
        # 学習ファイルなど
        for cat, names in CATEGORY_FILES.items():
            for fn in names:
                src = _DATA / fn
                if src.exists():
                    z.writestr(f"files/{fn}", src.read_bytes())
                    files_in.append(fn)
        # 暗号化キー（任意）
        if include_key:
            key = _DATA / "encryption_key.txt"
            if key.exists():
                z.writestr("secret/encryption_key.txt", key.read_bytes())
                manifest["includes_key"] = True
                files_in.append("encryption_key.txt(鍵)")
        z.writestr("manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=1))

    stamp = now.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"mcdbot-full-{stamp}.zip"
    log.info("まるごとバックアップを作成: %s（%d行 / 鍵%s）",
             name, total_rows, "あり" if manifest["includes_key"] else "なし")
    return BackupResult(
        name=name, data=buf.getvalue(),
        tables=len(tables_data), rows=total_rows,
        files=files_in, included_key=manifest["includes_key"],
    )


# ─────────────────────────────────────────────────────────
#  復元
# ─────────────────────────────────────────────────────────


@dataclass
class Manifest:
    """書庫の中身の目録（復元前に見せる用）。"""
    format: int
    created_at: str
    includes_key: bool
    categories: dict[str, dict] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def category_choices(self) -> list[tuple[str, str, int]]:
        """(区分キー, ラベル, 行数) を表示順で。中身のある区分だけ。"""
        out = []
        for cat in CATEGORY_ORDER:
            info = self.categories.get(cat)
            if info is None:
                continue
            out.append((cat, CATEGORY_LABEL.get(cat, cat), int(info.get("rows", 0))))
        return out


def read_manifest(data: bytes) -> Manifest:
    """書庫を開いて目録だけ読む。壊れていてもここで気づく。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "manifest.json" not in z.namelist():
                return Manifest(0, "", False,
                                error="これは、まるごとバックアップではありません。")
            m = json.loads(z.read("manifest.json").decode("utf-8"))
    except (zipfile.BadZipFile, ValueError, KeyError) as e:
        return Manifest(0, "", False, error=f"書庫として読めません（{e}）")

    fmt = int(m.get("format", 0))
    if fmt > FORMAT:
        return Manifest(fmt, m.get("created_at", ""), bool(m.get("includes_key")),
                        error=(
                            "このBOTより新しい形式のバックアップです。"
                            "BOTを新しくしてから復元してください。"))
    return Manifest(
        format=fmt,
        created_at=m.get("created_at", ""),
        includes_key=bool(m.get("includes_key")),
        categories=m.get("categories", {}),
    )


@dataclass
class RestoreResult:
    ok: bool
    message: str
    restored: list[str] = field(default_factory=list)   # 戻した区分
    tables: int = 0
    rows: int = 0
    files: list[str] = field(default_factory=list)
    restored_key: bool = False


def _decode_value(v):
    """バックアップのJSONから、DBに入れる値へ戻す。"""
    if isinstance(v, dict) and "__bytes__" in v:
        return bytes.fromhex(v["__bytes__"])
    return v


async def restore(data: bytes, categories: list[str], *,
                  restore_key: bool = False) -> RestoreResult:
    """選んだ区分だけ復元する。`categories` が全区分なら全体復元。

    ⚠️⚠️ **選んだ区分のテーブルは、いまの中身を消して入れ替える。**
       消すのは選んだ区分のテーブルだけ。選んでいない区分は触らない。

    ⚠️ 外部キーの検査を一時的に外して入れ替える。部分復元のとき、
       戻す区分の子テーブルが、戻さない区分の親を参照していると、
       検査が有効なままだと入らないことがある。入れ替え後に
       整合性を確かめ、壊れていればロールバックする。

    ⚠️ お金に関わる（users / orders / charge）。**途中で失敗したら
       全部なかったことにする**（1つのトランザクションで行う）。
    """
    man = read_manifest(data)
    if not man.ok:
        return RestoreResult(False, man.error)

    chosen = [c for c in CATEGORY_ORDER if c in set(categories)]
    if not chosen:
        return RestoreResult(False, "復元する区分が選ばれていません。")

    # 戻すテーブルを集める（依存順＝親→子）。
    want_tables: set[str] = set()
    for c in chosen:
        want_tables |= CATEGORY_TABLES[c]
    ordered = [t.name for t in Base.metadata.sorted_tables if t.name in want_tables]

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            payload: dict[str, list[dict]] = {}
            for t in ordered:
                path = f"tables/{t}.json"
                if path not in names:
                    return RestoreResult(
                        False, f"バックアップに {t} が入っていません（壊れています）。")
                payload[t] = json.loads(z.read(path).decode("utf-8"))

            # ファイル（学習内容など）
            file_payload: dict[str, bytes] = {}
            for c in chosen:
                for fn in CATEGORY_FILES.get(c, []):
                    p = f"files/{fn}"
                    if p in names:
                        file_payload[fn] = z.read(p)

            key_bytes = None
            if restore_key and man.includes_key and "secret/encryption_key.txt" in names:
                key_bytes = z.read("secret/encryption_key.txt")
    except (zipfile.BadZipFile, ValueError) as e:
        return RestoreResult(False, f"書庫を読めませんでした（{e}）")

    # ⚠️ DBの入れ替えが1行でも失敗したら、**全部なかったことにする**。
    #    session_scope が例外でロールバックするので、ここで捕まえて
    #    「失敗した。何も変わっていない」と正直に返す。
    #    （ファイルの書き戻しは、DBが成功してから行う）
    try:
        total_rows = await _apply_restore(ordered, payload)
    except Exception as e:
        log.warning("復元のDB入れ替えに失敗しました（元に戻しました）: %s", e)
        return RestoreResult(
            False,
            f"復元できませんでした（{str(e)[:120]}）。"
            "中身は変わっていません。",
        )

    # ファイルを書き戻す（DBが入ってから。順序の意味は薄いが揃える）
    written: list[str] = []
    for fn, blob in file_payload.items():
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            tmp = _DATA / (fn + ".restoring")
            tmp.write_bytes(blob)
            tmp.replace(_DATA / fn)
            written.append(fn)
        except OSError as e:
            log.warning("ファイル %s を戻せませんでした: %s", fn, e)

    # ⚠️ 学習ファイルを戻したら、プロセス内のキャッシュも読み直す。
    #    呼ばないと、再起動するまで古い内容のまま使われる。
    if any(fn in ("slot_bridge.json", "slot_rules.json") for fn in written):
        try:
            from services.mcd import slot_bridge, slot_rules
            if "slot_bridge.json" in written:
                slot_bridge.reload()
            if "slot_rules.json" in written:
                slot_rules.reload()
        except Exception:
            log.warning("学習内容の読み直しに失敗しました（再起動で反映されます）",
                        exc_info=True)

    restored_key = False
    if key_bytes is not None:
        try:
            tmp = _DATA / "encryption_key.txt.restoring"
            tmp.write_bytes(key_bytes)
            tmp.replace(_DATA / "encryption_key.txt")
            restored_key = True
            written.append("encryption_key.txt(鍵)")
        except OSError as e:
            log.warning("暗号化キーを戻せませんでした: %s", e)

    labels = "・".join(CATEGORY_LABEL.get(c, c) for c in chosen)
    return RestoreResult(
        ok=True,
        message=f"{labels} を復元しました。",
        restored=chosen, tables=len(ordered), rows=total_rows,
        files=written, restored_key=restored_key,
    )


async def _apply_restore(ordered: list[str], payload: dict[str, list[dict]]) -> int:
    """選んだテーブルを、1トランザクションで入れ替える。

    ⚠️ 外部キー検査は**この接続の中だけ**外す。終わったら元に戻す。
       （PostgreSQL では session replication など別の手段になるが、
         この構成は SQLite 前提。他方言のときは注意）
    """
    total = 0
    async with session_scope() as s:
        bind = s.get_bind()
        is_sqlite = bind.dialect.name == "sqlite"
        if is_sqlite:
            await s.execute(text("PRAGMA foreign_keys=OFF"))

        # 消すのは子→親（念のため。検査を切っているので実害は無い）
        for t in reversed(ordered):
            await s.execute(text(f"DELETE FROM {t}"))

        # 入れるのは親→子
        for t in ordered:
            rows = payload.get(t, [])
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            collist = ", ".join(cols)
            stmt = text(f"INSERT INTO {t} ({collist}) VALUES ({placeholders})")
            for r in rows:
                await s.execute(stmt, {c: _decode_value(r.get(c)) for c in cols})
            total += len(rows)

        if is_sqlite:
            # ⚠️ 戻す前に、外部キーが壊れていないか確かめる。
            #    壊れていれば例外→ session_scope がロールバックする。
            broken = (await s.execute(text("PRAGMA foreign_key_check"))).fetchall()
            await s.execute(text("PRAGMA foreign_keys=ON"))
            if broken:
                raise RuntimeError(
                    "復元後の整合性が取れませんでした（外部キー違反 "
                    f"{len(broken)}件）。元に戻しました。"
                    "選んだ区分に、別区分への参照が含まれている可能性があります。"
                )
    return total

