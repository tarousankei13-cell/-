"""
別置きの注文番号ページの検証

BOT とページを別の場所で動かす構成。実際にHTTPを通して、
・合い言葉が合わないと登録できないか
・残高や認証情報がページ側に渡っていないか
・ページが落ちていても注文が通るか
を確かめる。
"""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "receipt_site")
sys.path.insert(0, SITE)

if not os.path.isdir(SITE):
    # ページ側を同梱していない配り方もありうる。その場合は飛ばす。
    print("  （receipt_site/ が無いため、この検証は飛ばします）")
    sys.exit(0)

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

SECRET = "ひみつの合い言葉-12345"      # ⚠️ わざと日本語にしている（ASCII以外で壊れないか）
PORT = 18123
DIGEST = ""


async def main():
    tmp = tempfile.mkdtemp()
    os.environ["PUSH_SECRET"] = SECRET
    os.environ["DATA_DIR"] = tmp
    os.environ["EXPIRE_HOURS"] = "12"

    import importlib
    import main as site
    importlib.reload(site)
    from aiohttp import web as aw
    import aiohttp

    app = site.build_app()
    runner = aw.AppRunner(app, access_log=None)
    await runner.setup()
    await aw.TCPSite(runner, "127.0.0.1", PORT).start()
    base = f"http://127.0.0.1:{PORT}"
    print(f"\n  （ページを {base} で起動しました）")

    # ---- BOT 側 ----
    from core.crypto import init_cipher
    from db.session import init_db, close_db
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/bot.db")
    from core import settings
    await settings.load_all()
    from services import web_push, web as web_site
    global DIGEST
    DIGEST = web_push.secret_digest(SECRET)

    await settings.set_value("web_push_url", f"{base}/api/receipts")
    await settings.set_value("web_push_secret", SECRET)
    await settings.set_value("web_base_url", f"{base}/order")

    token = web_site.new_view_token()

    print("\n[1] BOT からページへ登録できる ★")
    sent = await web_push.send(
        token=token, receipt_number="7161", store_name="南砂町店",
        store_id="13934", pickup_label="店内でお召し上がり",
    )
    check("送れた ★", sent is True, sent)

    async with aiohttp.ClientSession() as cs:
        async with cs.get(f"{base}/order/{token}") as r:
            body = await r.text()
        check("ページが開く ★", r.status == 200, r.status)
        check("注文番号が出る ★", "7161" in body, body[:200])
        check("店舗名が出る", "南砂町店" in body, body[:300])
        check("受取方法が出る", "店内でお召し上がり" in body, body[:400])

        print("\n[2] ページ側に渡っていないもの ★")
        check("金額が無い ★", "480" not in body and "¥" not in body, body[:600])
        check("Discord ID が無い ★", "discord" not in body.lower())
        db = open(os.path.join(tmp, "receipts.db"), "rb").read()
        check("保存ファイルにも金額が無い ★", b"480" not in db)
        check("保存ファイルに合い言葉が無い ★", SECRET.encode() not in db)
        check("保存ファイルに認証情報が無い ★",
              b"refresh" not in db and b"token_enc" not in db)

        print("\n[3] 合い言葉が合わないと登録できない ★")
        payload = {"token": "nise", "receipt_number": "9999"}
        async with cs.post(f"{base}/api/receipts", json=payload,
                           headers={"X-Push-Secret": "ちがう"}) as r:
            check("断られる ★", r.status == 403, r.status)
        async with cs.post(f"{base}/api/receipts", json=payload) as r:
            check("合い言葉なしも断られる ★", r.status == 403, r.status)
        async with cs.get(f"{base}/order/nise") as r:
            check("登録されていない ★", r.status == 404, r.status)

        print("\n[3.5] 合い言葉はそのまま流れない ★")
        # ⚠️ HTTPヘッダは ASCII しか運べない。日本語の合い言葉を
        #    そのまま入れると送信側で落ち、受信側は500になる。
        check("指紋はASCIIだけ ★", DIGEST.isascii() and len(DIGEST) == 64, DIGEST[:20])
        check("合い言葉そのものを送らない ★", DIGEST != SECRET)
        async with cs.post(f"{base}/api/receipts",
                           json={"token": "raw", "receipt_number": "1"},
                           headers={"X-Push-Secret": "ちがう合い言葉"}) as r:
            check("ASCII以外が来ても500にしない ★", r.status == 403, r.status)

        print("\n[4] おかしな登録を弾く")
        async with cs.post(f"{base}/api/receipts", data="これはJSONではない",
                           headers={"X-Push-Secret": DIGEST}) as r:
            check("JSONでなければ400", r.status == 400, r.status)
        async with cs.post(f"{base}/api/receipts", json={"token": "x"},
                           headers={"X-Push-Secret": DIGEST}) as r:
            check("注文番号が無ければ400", r.status == 400, r.status)
        async with cs.post(
            f"{base}/api/receipts",
            json={"token": "y" * 100, "receipt_number": "1"},
            headers={"X-Push-Secret": DIGEST},
        ) as r:
            check("長すぎる合い言葉は400", r.status == 400, r.status)

        print("\n[5] タグを埋め込まれてもページが壊れない ★")
        bad_token = web_site.new_view_token()
        await web_push.send(
            token=bad_token, receipt_number="1234",
            store_name="<script>alert(1)</script>", store_id="1",
        )
        async with cs.get(f"{base}/order/{bad_token}") as r:
            b = await r.text()
        check("タグとして出さない ★", "<script>alert" not in b, b[:300])
        check("エスケープされている", "&lt;script&gt;" in b)

        print("\n[6] 知らないURLは見つからない")
        async with cs.get(f"{base}/order/" + "z" * 30) as r:
            check("404 を返す", r.status == 404, r.status)
            check("内容を漏らさない", "7161" not in await r.text())

        print("\n[7] 期限切れは出さない ★")
        old_token = web_site.new_view_token()
        await web_push.send(
            token=old_token, receipt_number="5555", store_name="古い店",
            created=datetime.now(timezone.utc) - timedelta(hours=13),
        )
        async with cs.get(f"{base}/order/{old_token}") as r:
            check("13時間前の注文は開けない ★", r.status == 404, r.status)

        print("\n[7.5] 注文時刻が無くても開ける ★")
        # ⚠️ 無いことを理由に期限切れ扱いにすると、登録はできるのに
        #    ページが開けない（必ず404）。実際にこれで踏んだ。
        bare = web_site.new_view_token()
        async with cs.post(
            f"{base}/api/receipts",
            json={"token": bare, "receipt_number": "8888", "store_name": "時刻なし店"},
            headers={"X-Push-Secret": DIGEST},
        ) as r:
            check("登録できる", r.status == 200, r.status)
        async with cs.get(f"{base}/order/{bare}") as r:
            b = await r.text()
        check("ページも開ける ★", r.status == 200, r.status)
        check("注文番号が出る ★", "8888" in b, b[:200])

        print("\n[8] 死活確認")
        async with cs.get(f"{base}/healthz") as r:
            d = await r.json()
        check("ok を返す", d.get("ok") is True, d)
        check("合い言葉の設定済みが分かる ★", d.get("ready") is True, d)

    print("\n[9] つながるか確かめられる ★")
    okay, note = await web_push.check()
    check("つながったと分かる ★", okay is True, note)

    print("\n[10] ページが落ちていても注文は通る ★")
    await runner.cleanup()
    sent = await web_push.send(token="dead", receipt_number="0001")
    check("送れないが例外は出ない ★", sent is False)
    okay, note = await web_push.check()
    check("つながらないと分かる ★", okay is False, note)
    check("理由を伝える", "つながりません" in note or "HTTP" in note, note)

    print("\n[11] 設定が無ければ何もしない")
    await settings.set_value("web_push_url", "")
    check("設定されていない", not web_push.configured())
    check("送らない", await web_push.send(token="a", receipt_number="1") is False)
    check("リンクも出さない", web_push.page_link("a") == "")

    print("\n[11.5] ページ側の設定の読み方 ★")
    # 直接書く / 環境変数 のどちらでも設定できる（BOT本体と同じ形）
    check("直接書いたほうが優先 ★", site._text("書いた", "PUSH_SECRET") == "書いた")
    check("空なら環境変数を見る ★", site._text("", "PUSH_SECRET") == SECRET, SECRET)
    check("どちらも無ければ既定値",
          site._text("", "存在しない変数", "きてい") == "きてい")
    # ⚠️ 既定値を設定ブロックに書くと、環境変数を入れても打ち消される
    check("書いていない（None）は環境変数を見る ★",
          site._number(None, "EXPIRE_HOURS", 12) == 12)
    check("0 と書いたら 0 のまま（期限なし）★",
          site._number(0, "EXPIRE_HOURS", 12) == 0)
    check("数字でない環境変数は既定値に戻す",
          site._number(None, "PATH", 12) == 12)

    print("\n[12] BOT とページの見た目が同じ ★")
    # ⚠️ 2か所に書くとずれる。receipt_page.py を共有している。
    from services import receipt_page as bot_side
    import receipt_page as site_side
    check("同じ内容のファイルを使っている ★",
          bot_side.STYLE == site_side.STYLE, "STYLE が違う")
    check("同じHTMLを作る ★",
          bot_side.order_html(receipt_number="1", store_name="あ")
          == site_side.order_html(receipt_number="1", store_name="あ"))
    check("見つからない画面も同じ",
          bot_side.not_found_html() == site_side.not_found_html())

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
