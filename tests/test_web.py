"""
注文番号ページの検証

・合い言葉を知っている人だけが見られるか
・注文番号を順に試して他人の注文を覗けないか
・出してはいけない情報（金額・誰が頼んだか）が漏れていないか
・期限が効くか
・落ちても注文に影響しないか
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def get(app, path):
    """aiohttp のテストサーバを使わず、ハンドラを直に叩く。"""
    from aiohttp import web as aw
    from aiohttp.test_utils import make_mocked_request
    for resource in app.router.resources():
        for route in resource:
            info = resource.resolve  # noqa
    req = make_mocked_request("GET", path, app=app)
    match = await app.router.resolve(req)
    if match.http_exception is not None:
        return match.http_exception.status, ""
    req._match_info = match
    resp = await match.handler(req)
    return resp.status, resp.text, resp.headers


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/w.db")
    from core import settings
    await settings.load_all()

    from db.models import Order, new_uuid
    from services import web as W
    import core.saga as saga

    app = W.build_app()

    # ---- 注文を2件つくる ----
    tok_a, tok_b = W.new_view_token(), W.new_view_token()
    async with session_scope() as s:
        for tok, num, name, uid in (
            (tok_a, "7161", "南砂町店", 1111),
            (tok_b, "7162", "１８号安中店", 2222),
        ):
            s.add(Order(
                id=new_uuid(), idempotency_key=new_uuid(), discord_id=uid,
                state=saga.COMPLETED, hex_payload="x",
                store_id="13934", store_name=name, pickup_method="eatIn",
                list_price=800, subsidy_rate=40.0, user_amount=480,
                subsidy_amount=320, receipt_number=num, view_token=tok,
            ))

    print("\n[1] 合い言葉を知っていれば見られる ★")
    status, body, headers = await get(app, f"/order/{tok_a}")
    check("200 が返る", status == 200, status)
    check("注文番号が大きく出る ★", "7161" in body, body[:200])
    check("店舗名が出る", "南砂町店" in body, body[:300])
    check("受取方法が出る", "店内でお召し上がり" in body, body[:400])

    print("\n[2] 出してはいけない情報が漏れていない ★")
    check("金額を出さない ★", "480" not in body and "800" not in body, body[:600])
    check("誰が頼んだか出さない ★", "1111" not in body, body[:600])
    check("他人の注文番号が混ざらない ★", "7162" not in body)

    print("\n[3] 注文番号そのものでは開けない ★")
    # ⚠️ 注文番号は4桁しかない。URLにしていたら総当たりで全部覗ける。
    for guess in ("7161", "0000", "9999"):
        st, *_ = await get(app, f"/order/{guess}")
        check(f"「{guess}」では開けない ★", st == 200 and "見つかりません" in (await get(app, f"/order/{guess}"))[1],
              st)

    print("\n[4] 知らない合い言葉では開けない")
    st, b2, _ = await get(app, f"/order/{W.new_view_token()}")
    check("見つかりませんと出る", "見つかりません" in b2, b2[:200])
    check("注文番号は出ない", "7161" not in b2 and "7162" not in b2)
    st3, b3, _ = await get(app, "/order/" + "a" * 200)
    check("長すぎる合い言葉も弾く", "見つかりません" in b3)

    print("\n[5] 検索避けとキャッシュ無効 ★")
    check("noindex を付ける ★", "noindex" in headers.get("X-Robots-Tag", ""), dict(headers))
    check("本文にも noindex", "noindex" in body)
    check("キャッシュさせない ★", headers.get("Cache-Control") == "no-store", dict(headers))
    check("リファラを渡さない", headers.get("Referrer-Policy") == "no-referrer")
    check("MIME を推測させない", headers.get("X-Content-Type-Options") == "nosniff")

    print("\n[6] 期限が効く ★")
    await settings.set_value("receipt_page_hours", 12)
    check("直後は期限内", not W._expired(datetime.now(timezone.utc)))
    check("13時間前は期限切れ ★",
          W._expired(datetime.now(timezone.utc) - timedelta(hours=13)))
    check("11時間前はまだ開ける",
          not W._expired(datetime.now(timezone.utc) - timedelta(hours=11)))
    await settings.set_value("receipt_page_hours", 0)
    check("0 なら期限なし",
          not W._expired(datetime.now(timezone.utc) - timedelta(days=30)))
    await settings.set_value("receipt_page_hours", 12)
    check("日時が無ければ開けない", W._expired(None))

    print("\n[7] 公開URLの組み立て ★")
    await settings.set_value("web_enabled", False)
    check("無効ならリンクを出さない ★", W.page_url(tok_a) == "", W.page_url(tok_a))
    await settings.set_value("web_enabled", True)
    await settings.set_value("web_base_url", "")
    check("公開URL未設定でもリンクを出さない ★", W.page_url(tok_a) == "")
    await settings.set_value("web_base_url", "https://example.com/order/")
    check("末尾のスラッシュを重ねない ★",
          W.page_url(tok_a) == f"https://example.com/order/{tok_a}", W.page_url(tok_a))
    check("合い言葉が無ければ空", W.page_url(None) == "" and W.page_url("") == "")

    print("\n[8] 合い言葉は推測できない ★")
    toks = {W.new_view_token() for _ in range(500)}
    check("500回つくって重複なし ★", len(toks) == 500, len(toks))
    check("十分な長さがある ★", all(len(t) >= 20 for t in toks), min(len(t) for t in toks))
    check("URLに入れられる文字だけ",
          all(all(c.isalnum() or c in "-_" for c in t) for t in toks))

    print("\n[9] 死活確認")
    st, b, _ = await get(app, "/healthz")
    check("/healthz が応答する", st == 200, st)
    check("ok を返す", "true" in b.lower(), b)

    print("\n[10] 文字をそのまま埋め込まない（HTMLエスケープ）★")
    resp = W.render_order(
        receipt_number="<script>alert(1)</script>",
        store_name="<b>お店</b>", store_id="1", pickup_label="", created=None,
    )
    check("タグとして出さない ★", "<script>" not in resp.text, resp.text[:300])
    check("エスケープされている", "&lt;script&gt;" in resp.text)

    print("\n[11] 停止したあとに止めても落ちない")
    await W.stop()
    await W.stop()
    check("二重に止めても例外にならない", not W.running())

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
