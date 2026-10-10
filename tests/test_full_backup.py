"""まるごとバックアップと、項目を選んでの復元

⚠️ お金（残高・注文・チャージ口座）に関わる。ここが狂うと取り返しが
   つかないので、次を必ず固定する。

   ① **全テーブルがどこかの区分に入っている**（漏れると全体復元で消える）
   ② バイト列（暗号化済みトークン）が往復して壊れない
   ③ 選んだ区分だけ戻り、選ばない区分は触らない
   ④ 途中で失敗したら全部なかったことにする（1トランザクション）
   ⑤ 外部キーが壊れる復元はロールバックする
   ⑥ 暗号化キーは既定で入れない。入れたときだけ戻せる
   ⑦ 壊れたファイル・空・区分0個でも落ちない
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from core import ledger as L
from core import settings
from core import users as urepo
from core.crypto import init_cipher
from db.models import Base, McdAccount, McdToken
from db.session import close_db, init_db, session_scope, user_scope
from services import full_backup as FB

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


async def seed():
    await urepo.get_or_create(555)
    async with user_scope(555) as s:
        await L.charge(s, 555, 3000, receipt_id="rx")
    async with session_scope() as s:
        s.add(McdAccount(
            id=7, label="acc7", email_enc=bytes([0, 255, 16, 32]),
            refresh_token_enc=bytes([1, 2, 3]), device_uid="d",
            wmop_device_id="w", fb_instance_id="f",
            home_lat=35.0, home_lng=139.0, status="ACTIVE"))
        s.add(McdToken(mcd_account_id=7, access_token_enc=bytes([9, 9, 9])))
    await settings.set_value("charge_rate", 120)


async def main() -> int:
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    d = tempfile.mkdtemp()
    await init_db(f"sqlite+aiosqlite:///{d}/b.db")
    await settings.load_all()
    # 学習ファイル・鍵を test 用の場所へ向ける
    FB._DATA = __import__("pathlib").Path(d)

    print("\n[1] 全テーブルが区分に入っているか ★")
    missing = FB.check_coverage()
    check("区分漏れのテーブルが無い ★", not missing, missing)
    # 区分どうしが重複していないこと
    seen = {}
    dup = []
    for cat, tabs in FB.CATEGORY_TABLES.items():
        for t in tabs:
            if t in seen:
                dup.append(t)
            seen[t] = cat
    check("1つのテーブルが2区分に入っていない ★", not dup, dup)
    check("db/models の全テーブルを数える",
          set(seen) == set(Base.metadata.tables),
          set(Base.metadata.tables) - set(seen))

    await seed()

    print("\n[2] バックアップを作る ★")
    r = await FB.make_backup()
    check("29テーブルぶん入っている ★", r.tables == len(Base.metadata.tables), r.tables)
    check("行数を数えている", r.rows >= 4, r.rows)
    check("鍵は既定で入れない ★", not r.included_key)
    man = FB.read_manifest(r.data)
    check("目録が読める", man.ok, man.error)
    check("区分の件数が出る",
          dict((c, n) for c, _, n in man.category_choices()).get("users") == 3,
          man.category_choices())

    print("\n[3] 選んだ区分だけ戻る ★")
    async with user_scope(555) as s:
        bal0 = await L.user_balance(s, 555)
    async with session_scope() as s:
        await s.execute(text("UPDATE ledger SET amount = 0"))
    await settings.set_value("charge_rate", 999)   # settings を変える
    async with user_scope(555) as s:
        check("壊れている（残高0）", await L.user_balance(s, 555) == 0)
    rr = await FB.restore(r.data, ["users"])
    check("users 復元は成功 ★", rr.ok, rr.message)
    async with user_scope(555) as s:
        check("残高が元に戻る ★", await L.user_balance(s, 555) == bal0,
              await L.user_balance(s, 555))
    check("選ばなかった settings は触っていない ★",
          int(settings.get("charge_rate")) == 999, settings.get("charge_rate"))

    print("\n[4] バイト列と子テーブルが往復しても壊れない ★")
    async with session_scope() as s:
        await s.execute(text("DELETE FROM mcd_tokens"))
        await s.execute(text("UPDATE mcd_accounts SET email_enc = X'00'"))
    rr = await FB.restore(r.data, ["mcd"])
    check("mcd 復元は成功", rr.ok, rr.message)
    async with session_scope() as s:
        row = (await s.execute(
            text("SELECT email_enc FROM mcd_accounts WHERE id=7"))).fetchone()
        tok = (await s.execute(
            text("SELECT access_token_enc FROM mcd_tokens WHERE mcd_account_id=7"))
        ).fetchone()
    check("バイト列が元通り ★", row and bytes(row[0]) == bytes([0, 255, 16, 32]),
          bytes(row[0]).hex() if row else "無し")
    check("子テーブル（token）も戻る ★",
          tok and bytes(tok[0]) == bytes([9, 9, 9]))

    print("\n[5] 全体復元 ★")
    rr = await FB.restore(r.data, list(FB.CATEGORY_ORDER))
    check("全区分を戻せる ★", rr.ok, rr.message)
    check("全テーブルを戻した", rr.tables == len(Base.metadata.tables), rr.tables)
    # settings も戻ったので 120 に
    await settings.load_all()
    check("settings も戻る（charge_rate=120）★",
          int(settings.get("charge_rate")) == 120, settings.get("charge_rate"))

    print("\n[6] 暗号化キー ★")
    (FB._DATA / "encryption_key.txt").write_text("test-key-contents")
    rk = await FB.make_backup(include_key=True)
    check("入れたときは included_key ★", rk.included_key)
    check("既定では入らない ★", not (await FB.make_backup()).included_key)
    man_k = FB.read_manifest(rk.data)
    check("目録にも鍵ありと出る", man_k.includes_key)
    # 鍵を消して、restore_key で戻す
    (FB._DATA / "encryption_key.txt").unlink()
    rr = await FB.restore(rk.data, ["settings"], restore_key=True)
    check("restore_key で鍵が戻る ★", rr.restored_key)
    check("鍵ファイルが復活する ★",
          (FB._DATA / "encryption_key.txt").read_text() == "test-key-contents")
    # restore_key を指定しなければ鍵は戻らない
    (FB._DATA / "encryption_key.txt").unlink()
    rr = await FB.restore(rk.data, ["settings"])
    check("指定しなければ鍵は戻さない ★", not rr.restored_key)
    check("鍵ファイルは作られない ★",
          not (FB._DATA / "encryption_key.txt").exists())

    print("\n[7] 壊れた入力でも落ちない ★")
    check("zipでない", bool(FB.read_manifest(b"not a zip").error))
    check("空", bool(FB.read_manifest(b"").error))
    check("manifest無しのzip", bool(FB.read_manifest(_empty_zip()).error))
    rr = await FB.restore(r.data, [])
    check("区分0個は断る ★", not rr.ok and "選" in rr.message, rr.message)
    rr = await FB.restore(b"xxxx", ["users"])
    check("壊れた書庫の復元は断る ★", not rr.ok, rr.message)

    print("\n[7b] 途中で失敗したら、何も変えない ★")
    # ⚠️ お金に関わる。1行でも入らなければ、全部なかったことにする。
    import io as _io, zipfile as _zip, json as _json
    z = _zip.ZipFile(_io.BytesIO(r.data))
    cols = ["id", "idempotency_key", "discord_id", "state", "mcd_account_id", "store_id"]
    out = _io.BytesIO()
    with _zip.ZipFile(out, "w") as w:
        for n in z.namelist():
            data = z.read(n)
            if n == "tables/orders.json":
                # NOT NULL を満たさない壊れた行（入れた瞬間に失敗する）
                data = _json.dumps([{c: {"id": "x", "idempotency_key": "k",
                    "discord_id": 1, "state": "QUOTED", "mcd_account_id": None,
                    "store_id": "1"}.get(c) for c in cols}]).encode()
            w.writestr(n, data)
    async with session_scope() as s:
        before = (await s.execute(text("SELECT COUNT(*) FROM orders"))).scalar()
    rr = await FB.restore(out.getvalue(), ["orders"])
    check("失敗を正直に返す（例外で落ちない）★", not rr.ok, rr.message)
    check("中身は変わっていないと言う ★", "変わって" in rr.message, rr.message)
    async with session_scope() as s:
        after = (await s.execute(text("SELECT COUNT(*) FROM orders"))).scalar()
    check("orders の件数が変わっていない ★（ロールバック）", before == after,
          (before, after))

    print("\n[7c] 学習ファイルを戻したら、その場で反映される ★")
    # ⚠️ ファイルを戻すだけでは、プロセス内のキャッシュは古いまま。
    #    再起動まで反映されないと「復元したのに効かない」になる。
    from services.mcd import slot_bridge as SB
    SB.STORE_PATH = FB._DATA / "slot_bridge.json"
    SB.reload()
    SB.forget_all()
    SB.learn("8800001", "8800009")        # KNOWNに無いコードを1件おぼえる
    rb = await FB.make_backup()           # ← この状態をバックアップ
    SB.forget_all()                       # 全部忘れる
    check("いったん忘れた", SB.bridge_for("8800001") == "")
    rr = await FB.restore(rb.data, ["menu"])
    check("menu 復元は成功", rr.ok, rr.message)
    check("復元で学習が戻る（再起動不要）★",
          SB.bridge_for("8800001") == "8800009", SB.bridge_for("8800001"))

    print("\n[8] 区分漏れがあるとバックアップを作らせない ★")
    # わざと1区分からテーブルを抜く
    saved = FB.CATEGORY_TABLES["users"]
    FB.CATEGORY_TABLES["users"] = {"users", "carts"}   # ledger を抜く
    try:
        await FB.make_backup()
        check("区分漏れなら例外 ★", False, "作れてしまった")
    except RuntimeError as e:
        check("区分漏れなら例外 ★", "ledger" in str(e), str(e))
    finally:
        FB.CATEGORY_TABLES["users"] = saved

    await close_db()
    print(f"\n{'='*52}\n  成功 {ok} / 失敗 {fail}\n{'='*52}")
    return 1 if fail else 0


def _empty_zip() -> bytes:
    import io, zipfile
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("hello.txt", "x")
    return b.getvalue()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
