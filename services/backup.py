"""
バックアップの保存先と復元

これまではDiscordの管理者チャンネルへ送るだけだった。
そのチャンネルが消えると復旧できなくなるため、保存先を選べるようにした。

  channel   管理者チャンネル（既定）
  dm        オーナーのDM。チャンネルが消えても残る
  local     サーバー上の data/backups。世代を保って古いものから消す
  all       上の全部

⚠️ この控えには**残高と注文履歴**が入っている。
   登録済みアカウントの認証情報は暗号化されたまま入っているので、
   復元するには `data/encryption_key.txt` も必要になる。
   鍵が無くてもお金の記録は戻せる（アカウントは入れ直しになる）。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config

log = logging.getLogger("bot.backup")

_ROOT = Path(__file__).parent.parent
LOCAL_DIR = _ROOT / "data" / "backups"

# 残しておく世代の数
KEEP = 14


@dataclass
class Saved:
    """保存した結果。"""
    name: str
    size: int
    where: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.where)


def save_local(name: str, data: bytes, keep: int | None = None) -> Path:
    """
    サーバー上に残す。古い世代は消す。

    ディスクを食い潰さないよう、残す数に上限を設ける。
    """
    keep = keep if keep is not None else int(
        _setting("backup_keep", KEEP)
    )
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCAL_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)

    # 新しい順に並べて、超えたぶんを消す
    files = sorted(
        LOCAL_DIR.glob("backup-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for old in files[max(keep, 1):]:
        try:
            old.unlink()
            log.info("古いバックアップを消しました: %s", old.name)
        except OSError:
            pass
    return path


def _setting(key: str, default):
    from core import settings

    return settings.get(key, default)


def list_local() -> list[tuple[str, int, float]]:
    """保存してあるもの。新しい順に (名前, 大きさ, 更新時刻)。"""
    if not LOCAL_DIR.exists():
        return []
    out = []
    for p in LOCAL_DIR.glob("backup-*.db"):
        st = p.stat()
        out.append((p.name, st.st_size, st.st_mtime))
    return sorted(out, key=lambda x: x[2], reverse=True)


def verify(data: bytes) -> tuple[bool, str]:
    """
    中身が壊れていないか確かめる。

    復元する前に必ず通す。壊れたファイルで上書きすると取り返しがつかない。
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        con = sqlite3.connect(path)
        try:
            result = con.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                return False, f"ファイルが壊れています（{result[0] if result else '?'}）"
            tables = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            needed = {"ledger", "orders", "users"}
            missing = needed - tables
            if missing:
                return False, f"必要な内容が入っていません（{', '.join(sorted(missing))}）"
            n = con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            return True, f"元帳 {n:,} 件を含む、正しいバックアップです"
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return False, f"データベースとして読めません（{e}）"
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def restore(data: bytes, database_url: str) -> tuple[bool, str]:
    """
    バックアップから戻す。

    ⚠️ いまの内容は失われる。戻す前に、いまの内容も別名で残しておく。
       SQLite のときだけ対応する（PostgreSQL は運用側の手順で戻すこと）。
    """
    if not database_url.startswith("sqlite"):
        return False, (
            "PostgreSQL では、この操作からは戻せません。"
            "pg_restore などの手順で戻してください。"
        )

    good, message = verify(data)
    if not good:
        return False, message

    # sqlite+aiosqlite:///./data/bot.db → ./data/bot.db
    db_path = Path(database_url.split("///", 1)[-1])
    if not db_path.is_absolute():
        db_path = _ROOT / db_path

    # いまの内容を残しておく（戻した結果が思っていたものと違ったとき用）
    if db_path.exists():
        stamp = config.now_jst().strftime("%Y%m%d-%H%M%S")
        keep_path = db_path.with_name(f"{db_path.stem}-before-restore-{stamp}.db")
        try:
            shutil.copy2(db_path, keep_path)
            log.info("復元前の内容を残しました: %s", keep_path.name)
        except OSError as e:
            return False, f"いまの内容を退避できなかったため中止しました: {e}"

    try:
        tmp = db_path.with_suffix(".restoring")
        tmp.write_bytes(data)
        tmp.replace(db_path)
    except OSError as e:
        return False, f"書き込めませんでした: {e}"

    # 同時に作られる補助ファイルは、古い内容を指しているので消す
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        try:
            side.unlink(missing_ok=True)
        except OSError:
            pass

    log.warning("バックアップから復元しました: %s", db_path)
    return True, (
        f"{message}\n復元しました。**BOTを再起動してください。**"
    )
