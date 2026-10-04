"""
プロキシ（通信の出口）の設定

BOT が外へ出る通信を、どの出口から出すか。
ここを間違えると
  ・設定したつもりで素のIPから出ていた（海外IPで弾かれる）
  ・プロキシのパスワードがログや監査ログに残った
  ・socks を設定した瞬間に全ての通信が止まった
のどれかが起きる。全部確かめる。

⚠️ 実際に通信して、外から見えるIPが変わることまで見る（最後の節）。
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/px.db")
    from core import proxy, settings
    from core.http import build_async_client
    await settings.load_all()

    # 環境変数が設定されていると判定が変わるので、この試験の間だけ外す
    saved_env = os.environ.pop(proxy.ENV_KEY, None)

    # ── 伏せ字 ──────────────────────────────────────────
    print("\n[ パスワードを隠す ]")
    check("パスワードが消える",
          proxy.mask("http://u:pw@h:8080") == "http://u:***@h:8080",
          proxy.mask("http://u:pw@h:8080"))
    check("パスワードの文字が残らない", "pw" not in proxy.mask("http://u:pw@h:8080"))
    check("認証なしはそのまま",
          proxy.mask("http://h:8080") == "http://h:8080")
    check("空は『なし』", proxy.mask("") == "（なし）")
    check("None でも落ちない", proxy.mask(None) == "（なし）")
    check("壊れた値でも生のまま出さない",
          "pw" not in proxy.mask("http://u:pw@["))

    # ── Discord 用の分解 ────────────────────────────────
    print("\n[ Discord 用に分ける ]")
    u, a = proxy.split_auth("http://u:pw@h:8080")
    check("URLから認証情報が抜ける", u == "http://h:8080", u)
    check("利用者名とパスワードが取れる", a == ("u", "pw"), a)
    u, a = proxy.split_auth("http://h:8080")
    check("認証なしは None", u == "http://h:8080" and a is None, (u, a))
    check("空は両方 None", proxy.split_auth("") == (None, None))

    # ── 受け付ける形 ────────────────────────────────────
    print("\n[ 設定できる形かを見る ]")
    check("http は通る", proxy.valid("http://h:8080"))
    check("https も通る", proxy.valid("https://h:8080"))
    check("空（解除）は通る", proxy.valid(""))
    check("ftp は断る", not proxy.valid("ftp://h"))
    check("ホスト無しは断る", not proxy.valid("http://"))
    check("ただの文字列は断る", not proxy.valid("こわれています"))
    check("断る理由が日本語で返る", "ホスト名" in proxy.problem("http://"),
          proxy.problem("http://"))
    if proxy.socks_ready():
        check("socksio があれば socks は通る", proxy.valid("socks5://h:1080"))
    else:
        check("socksio が無ければ socks は断る",
              not proxy.valid("socks5://h:1080"))
        check("理由に入れ方が書いてある",
              "httpx[socks]" in proxy.problem("socks5://h:1080"),
              proxy.problem("socks5://h:1080"))

    # ── 優先順位 ────────────────────────────────────────
    print("\n[ どの設定が勝つか ]")
    proxy.set_bootstrap("")
    check("何も無ければ None", proxy.resolve("mcd") is None, proxy.resolve("mcd"))

    proxy.set_bootstrap("http://boot:1", source="main.py")
    check("設定欄の値が効く", proxy.resolve("mcd") == "http://boot:1")
    check("出どころが分かる", proxy.source_of("mcd") == "main.py",
          proxy.source_of("mcd"))

    os.environ[proxy.ENV_KEY] = "http://env:1"
    check("環境変数は設定欄より強い", proxy.resolve("mcd") == "http://env:1",
          proxy.resolve("mcd"))
    del os.environ[proxy.ENV_KEY]

    await settings.set_value("proxy_all", "http://all:1")
    check("全体設定は設定欄より強い", proxy.resolve("mcd") == "http://all:1",
          proxy.resolve("mcd"))
    check("出どころが全体設定になる",
          "全体" in proxy.source_of("mcd"), proxy.source_of("mcd"))

    await settings.set_value("proxy_mcd", "http://mcd:1")
    check("サービス設定は全体より強い", proxy.resolve("mcd") == "http://mcd:1",
          proxy.resolve("mcd"))
    check("他のサービスは全体のまま",
          proxy.resolve("kyash") == "http://all:1", proxy.resolve("kyash"))
    check("口座ごとの指定が一番強い",
          proxy.resolve("mcd", account="http://own:1") == "http://own:1")
    check("口座の指定が空なら無視される",
          proxy.resolve("mcd", account="  ") == "http://mcd:1")

    # PayPay は古い名前でも読めること（設定し直さずに動くように）
    await settings.set_value("paypay_proxy", "http://old:1")
    check("古い paypay_proxy も読む",
          proxy.resolve("paypay") == "http://old:1", proxy.resolve("paypay"))
    await settings.set_value("proxy_paypay", "http://new:1")
    check("新しいキーが優先される",
          proxy.resolve("paypay") == "http://new:1", proxy.resolve("paypay"))
    from services.paypay import accounts as pp_accounts
    check("PayPay の既定取得も同じ値を返す",
          pp_accounts.config_proxy() == "http://new:1",
          pp_accounts.config_proxy())
    await settings.set_value("paypay_proxy", "")
    await settings.set_value("proxy_paypay", "")

    # 一覧表示にパスワードが出ないこと
    await settings.set_value("proxy_all", "http://u:pw@h:1")
    shown = proxy.describe()
    check("一覧にパスワードが出ない",
          all("pw" not in u for _, u in shown), shown)
    check("一覧は全体＋サービス分", len(shown) == 1 + len(proxy.SERVICES),
          len(shown))

    # ── ログ・監査ログに残らないこと ───────────────────
    print("\n[ パスワードを記録に残さない ]")
    check("プロキシは伏せて記録",
          "pw" not in str(settings.safe_value("proxy_all", "http://u:pw@h:1")),
          settings.safe_value("proxy_all", "http://u:pw@h:1"))
    check("合い言葉は中身を出さない",
          "s3cret" not in str(settings.safe_value("web_push_secret", "s3cret")),
          settings.safe_value("web_push_secret", "s3cret"))
    check("秘密でない設定はそのまま",
          settings.safe_value("charge_min", 500) == 500)
    check("ホストまでは残す（調べ物のため）",
          "h:1" in str(settings.safe_value("proxy_mcd", "http://u:pw@h:1")))

    from core import audit
    from db.models import AuditLog
    from sqlalchemy import select
    await settings.set_value(
        "proxy_kyash", "http://u:leaked@h:1", updated_by=777,
    )
    async with session_scope() as s:
        rows = (await s.execute(
            select(AuditLog).where(AuditLog.target == "proxy_kyash")
        )).scalars().all()
    blob = " ".join(f"{r.before} {r.after}" for r in rows)
    check("監査ログに記録は残る", len(rows) >= 1, len(rows))
    check("監査ログにパスワードは残らない", "leaked" not in blob, blob)
    await settings.set_value("proxy_kyash", "")

    # ── socks を設定しても通信が死なないこと ───────────
    print("\n[ socks を設定しても止まらない ]")
    await settings.set_value("proxy_all", "socks5://h:1080")
    await settings.set_value("proxy_mcd", "")
    if proxy.socks_ready():
        check("socksio があればそのまま使う",
              proxy.resolve("mcd") == "socks5://h:1080")
    else:
        check("使えない socks は外して直接つなぐ",
              proxy.resolve("mcd") is None, proxy.resolve("mcd"))
        c = build_async_client(timeout=1.0, service="mcd")
        check("クライアントが作れる（通信が死なない）", c is not None)
        await c.aclose()

    # ── 各通信がプロキシを引くこと ─────────────────────
    print("\n[ すべての通信が設定を見るか ]")
    await settings.set_value("proxy_all", "http://127.0.0.1:1/")
    for name in ("mcd", "kyash", "paypay", "web"):
        await settings.set_value(f"proxy_{name}", f"http://127.0.0.1:1{name[0]}")
    import inspect
    import core.http as http_mod
    sig = inspect.signature(http_mod.build_async_client)
    check("クライアント生成が service を受ける", "service" in sig.parameters)

    # 実コードの呼び出し箇所に service か proxy が付いているか
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parent.parent
    bare = []
    for f in root.rglob("*.py"):
        if "tests" in f.parts or f.name in ("http.py", "proxy.py"):
            continue
        for m in re.finditer(r"build_async_client\((.*?)\)", f.read_text(), re.S):
            if "service=" not in m.group(1) and "proxy=" not in m.group(1):
                bare.append(f"{f.relative_to(root)}: {m.group(1)[:40]}")
    check("プロキシを通さない通信が残っていない ★", not bare, bare)

    await settings.set_value("proxy_all", "")
    for name in ("mcd", "kyash", "paypay", "web"):
        await settings.set_value(f"proxy_{name}", "")

    # ── 実際に通信して出口を確かめる ───────────────────
    print("\n[ 実際に通信する ]")
    direct_ok, direct = await proxy.check("")
    if not direct_ok:
        print(f"  ⏭  外に出られない環境のため省略（{direct}）")
    else:
        check("直接つないで自分のIPが取れた ★", bool(direct), direct)
        print(f"      直接の出口 … {direct}")

        up = os.environ.get("HTTPS_PROXY", "")
        if up:
            via_ok, via = await proxy.check(up)
            check("プロキシ経由でも通る ★", via_ok, via)
            check("出口のIPが変わる ★", via_ok and via != direct, (direct, via))
            print(f"      プロキシ経由の出口 … {via}")
        else:
            print("  ⏭  使えるプロキシが無いため経由の確認は省略")

        bad_ok, bad = await proxy.check("http://127.0.0.1:9")
        check("通らないプロキシは失敗として返る", not bad_ok, bad)
        check("失敗の理由が読める", "Error" in bad or "error" in bad, bad)

    # ── カタログ用の共用接続が設定に追従するか ─────────
    print("\n[ 使い回している接続も切り替わるか ]")
    proxy.set_bootstrap("")        # 設定欄の値も外しておく
    http_mod._catalog = None
    http_mod._catalog_proxy = None
    c1 = http_mod.catalog_client()
    check("1本目が作れる", c1 is not None)
    check("最初はプロキシなし", http_mod._catalog_proxy is None,
          http_mod._catalog_proxy)
    await settings.set_value("proxy_mcd", "http://127.0.0.1:1")
    c2 = http_mod.catalog_client()
    check("設定を変えると作り直される ★", c2 is not c1)
    check("新しい設定が入っている",
          http_mod._catalog_proxy == "http://127.0.0.1:1",
          http_mod._catalog_proxy)
    c3 = http_mod.catalog_client()
    check("変えていなければ使い回す", c3 is c2)
    await settings.set_value("proxy_mcd", "")
    await http_mod.close_shared()
    check("終了でプロキシの記憶も消える", http_mod._catalog_proxy is None)

    # ── コマンドが生えているか ─────────────────────────
    print("\n[ コマンド ]")
    import cogs.config_cmd as cc
    names = sorted(c.name for c in cc.ConfigCog.proxy_group.commands)
    check("/proxy に4つのコマンドがある",
          names == ["clear", "set", "show", "test"], names)
    check("/proxy は独立したコマンド（/config は上限いっぱい）",
          cc.ConfigCog.proxy_group.parent is None)
    check("/config の子は25以内",
          len(cc.ConfigCog.group.commands) <= 25,
          len(cc.ConfigCog.group.commands))
    check("選べるサービスが全部ある",
          {c.value for c in cc.ConfigCog._SERVICE_CHOICES}
          == set(proxy.SERVICES) | {""},
          [c.value for c in cc.ConfigCog._SERVICE_CHOICES])

    if saved_env is not None:
        os.environ[proxy.ENV_KEY] = saved_env
    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
