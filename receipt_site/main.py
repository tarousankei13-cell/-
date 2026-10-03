"""
注文番号ページ（単体で動く版）

マクドナルド注文BOTとは**別の場所**に置いて動かすためのもの。
BOT から注文番号を受け取り、店頭で見せるためのページを出すだけ。

ここに置かれないもの:
  ・利用者の残高、元帳、注文履歴
  ・マクドナルド / Kyash の認証情報
  ・Discord のトークン
  仮にこのサーバーを覗かれても、漏れるのは
  「注文番号・店舗名・受取方法・時刻」だけ。

必要な環境変数:
  PORT           待ち受けるポート（ホスティングが決める）
  HOST           待ち受けるアドレス（未指定なら 0.0.0.0）
  PUSH_SECRET    BOT と共有する合い言葉。**必須**
  DATA_DIR       保存先（既定 ./data）
  EXPIRE_HOURS   何時間で消すか（既定 12）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

import receipt_page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("receipt")

PUSH_SECRET = os.environ.get("PUSH_SECRET", "").strip()


def secret_digest(secret: str) -> str:
    """
    合い言葉を、そのまま送らずに指紋にして送る。

    ⚠️ HTTPヘッダは ASCII しか運べない。日本語の合い言葉を
       そのまま入れると、送る側で UnicodeEncodeError、
       受ける側で hmac.compare_digest が TypeError になる。
       指紋（16進）にすれば、どんな文字でも使える。

    合い言葉そのものが回線や中継のログに残らない利点もある。
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


PUSH_DIGEST = secret_digest(PUSH_SECRET) if PUSH_SECRET else ""
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
EXPIRE_HOURS = int(os.environ.get("EXPIRE_HOURS", "12") or 12)
MAX_BODY = 8 * 1024          # 注文1件はせいぜい数百バイト
TOKEN_MAX = 64

DB_PATH = DATA_DIR / "receipts.db"


# ============================================================
#  保存
# ============================================================

def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS receipts (
            token          TEXT PRIMARY KEY,
            receipt_number TEXT NOT NULL,
            store_name     TEXT,
            store_id       TEXT,
            pickup_label   TEXT,
            created_at     TEXT,
            saved_at       TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def save(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO receipts "
        "(token, receipt_number, store_name, store_id, pickup_label, created_at, saved_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            data["token"], data["receipt_number"],
            data.get("store_name", ""), data.get("store_id", ""),
            data.get("pickup_label", ""), data.get("created_at", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def load(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT receipt_number, store_name, store_id, pickup_label, created_at, saved_at "
        "FROM receipts WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    return {
        "receipt_number": row[0], "store_name": row[1] or "",
        "store_id": row[2] or "", "pickup_label": row[3] or "",
        "created_at": row[4] or "", "saved_at": row[5] or "",
    }


def sweep(conn: sqlite3.Connection) -> int:
    """期限切れを消す。注文番号は当日しか使わないので、長くは持たない。"""
    if EXPIRE_HOURS <= 0:
        return 0
    limit = (datetime.now(timezone.utc) - timedelta(hours=EXPIRE_HOURS)).isoformat()
    cur = conn.execute("DELETE FROM receipts WHERE saved_at < ?", (limit,))
    conn.commit()
    return cur.rowcount


def parse_time(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expired(created: datetime | None) -> bool:
    if EXPIRE_HOURS <= 0:
        return False
    if created is None:
        return True
    return datetime.now(timezone.utc) - created > timedelta(hours=EXPIRE_HOURS)


# ============================================================
#  経路
# ============================================================

def html_response(body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=body, status=status, content_type="text/html", charset="utf-8",
        headers=dict(receipt_page.HEADERS),
    )


def not_found() -> web.Response:
    return html_response(receipt_page.not_found_html(), status=404)


async def handle_page(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    if not token or len(token) > TOKEN_MAX:
        return not_found()

    data = load(request.app["db"], token)
    if data is None:
        return not_found()

    created = parse_time(data["created_at"])
    # 期限は注文時刻で測る。
    # ⚠️ 注文時刻が入っていないときは、受け取った時刻で代える。
    #    無いことを理由に期限切れ扱いにすると、登録はできるのに
    #    ページが開けない（必ず404になる）。
    if expired(created or parse_time(data["saved_at"])):
        return not_found()

    return html_response(
        receipt_page.order_html(
            receipt_number=data["receipt_number"],
            store_name=data["store_name"],
            store_id=data["store_id"],
            pickup_label=data["pickup_label"],
            created=created,
        )
    )


async def handle_push(request: web.Request) -> web.Response:
    """
    BOT からの登録を受ける。

    ⚠️ 合い言葉が合わなければ、何も教えずに断る。
       「トークンが違います」と返すと、総当たりの手がかりになる。
    """
    sent = request.headers.get("X-Push-Secret", "")
    # ⚠️ compare_digest は ASCII 以外を比較できない。指紋どうしを比べる。
    if (
        not PUSH_DIGEST
        or not sent.isascii()
        or not hmac.compare_digest(sent, PUSH_DIGEST)
    ):
        log.warning("合い言葉の違う登録を断りました（%s）", request.remote)
        return web.json_response({"ok": False}, status=403)

    if request.content_length and request.content_length > MAX_BODY:
        return web.json_response({"ok": False, "error": "too_large"}, status=413)

    try:
        data = json.loads(await request.text())
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    token = str(data.get("token") or "")
    number = str(data.get("receipt_number") or "")
    if not token or not number or len(token) > TOKEN_MAX:
        return web.json_response({"ok": False, "error": "missing"}, status=400)

    save(request.app["db"], {
        "token": token,
        "receipt_number": number[:16],
        "store_name": str(data.get("store_name") or "")[:128],
        "store_id": str(data.get("store_id") or "")[:8],
        "pickup_label": str(data.get("pickup_label") or "")[:32],
        "created_at": str(data.get("created_at") or ""),
    })
    removed = sweep(request.app["db"])
    if removed:
        log.info("期限切れを %d 件消しました", removed)
    log.info("注文番号を登録しました（%s）", number)
    return web.json_response({"ok": True})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ready": bool(PUSH_SECRET)})


async def handle_root(request: web.Request) -> web.Response:
    return html_response(
        receipt_page.shell(
            "ご注文番号",
            "<p class='bad'>ご注文番号のページです</p>"
            "<p class='note'>Discord に届いた控えのリンクからお進みください。</p>",
        )
    )


def build_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app["db"] = connect()
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_health)
    app.router.add_post("/api/receipts", handle_push)
    # 前段で /order/ を剥がす置き方と、剥がさない置き方の両方に備える
    app.router.add_get("/order/{token}", handle_page)
    app.router.add_get("/{token}", handle_page)
    return app


def main() -> None:
    if not PUSH_SECRET:
        log.error(
            "環境変数 PUSH_SECRET が設定されていません。\n"
            "BOT と同じ合い言葉を入れてください。設定するまで登録を受け付けません。"
        )
    host = os.environ.get("HOST", "").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        log.error("環境変数 PORT の値が数字ではありません")
        sys.exit(1)

    log.info("注文番号ページを開始します（%s:%s / 期限 %d時間）",
             host, port, EXPIRE_HOURS)
    web.run_app(build_app(), host=host, port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
