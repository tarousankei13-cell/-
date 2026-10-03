"""代理実績の検証 — 通常の実績と見た目が1ミリも変わらないことを確かめる"""
import asyncio, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from core.crypto import init_cipher
from db.session import init_db, session_scope, close_db
from core import settings, subsidy, users as user_repo
from ui import flows, embeds

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

SENT = []

class FakeChannel:
    def __init__(self, cid): self.id = cid
    async def send(self, **kw): SENT.append(kw); return None

class FakeClient:
    def get_channel(self, cid): return FakeChannel(cid)

def dump(e: discord.Embed) -> dict:
    """埋め込みを比較できる形に落とす。色・タイトル・全フィールドを含む。"""
    return {
        "title": e.title, "description": e.description, "color": e.color.value if e.color else None,
        "fields": [(f.name, f.value, f.inline) for f in e.fields],
        "footer": e.footer.text if e.footer else None,
        "image": e.image.url if e.image else None,
        "author": e.author.name if e.author else None,
        "thumbnail": e.thumbnail.url if e.thumbnail else None,
    }

async def main():
    tmp = tempfile.mkdtemp()
    init_cipher("dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0")
    await init_db(f"sqlite+aiosqlite:///{tmp}/a.db")
    await settings.load_all()
    await settings.set_value("channel_achievement", 999)

    UID = 555001
    await user_repo.get_or_create(UID)
    client = FakeClient()

    ARGS = dict(
        discord_id=UID, display_name="テスト太郎",
        list_price=800, subsidy_rate=40.0, user_amount=480,
        store_name="南砂町店", receipt_number="7161",
        pickup_label="店内（カウンター受取）", daily_count=12,
    )

    print("\n[1] 通常の実績と代理の実績が同一か（既定の表示項目）")
    SENT.clear()
    await flows.send_achievement(client, **ARGS)   # 通常経路が呼ぶのと同じ関数
    normal = dump(SENT[0]["embed"])
    SENT.clear()
    await flows.send_achievement(client, **ARGS)   # 代理経路
    proxy = dump(SENT[0]["embed"])
    check("埋め込みが完全一致", normal == proxy,
          [k for k in normal if normal[k] != proxy[k]])
    check("タイトルに代理を示す文字が無い",
          "代理" not in (normal["title"] or "") and "管理" not in (normal["title"] or ""),
          normal["title"])
    check("本文にも代理の痕跡が無い",
          all("代理" not in str(v) for v in normal.values()), normal)
    check("author/footer に印が付いていない",
          normal["author"] is None and normal["footer"] is None, normal)

    print("\n[2] 表示項目の設定にそのまま従うか")
    await settings.set_value("achievement_fields", ["anon_code", "user_amount"])
    SENT.clear()
    await flows.send_achievement(client, **ARGS)
    names = [n for n, _, _ in dump(SENT[0]["embed"])["fields"]]
    check(f"2項目だけ表示（実際{len(names)}）", len(names) == 2, names)
    check("定価は出ていない", not any("定価" in n for n in names), names)

    await settings.set_value("achievement_fields",
                             ["anon_code", "list_price", "subsidy_rate", "user_amount",
                              "daily_count", "store_name", "receipt_number", "pickup_label"])
    SENT.clear()
    await flows.send_achievement(client, **ARGS)
    names = [n for n, _, _ in dump(SENT[0]["embed"])["fields"]]
    # ⚠️ 負担率は公開パネルに出さない（運営の持ち出しが分かってしまう）。
    #    設定に入れても無視されるので、全部ONでも7項目になる。
    check(f"全項目ONでも7項目（実際{len(names)}）", len(names) == 7, names)
    check("負担率は公開パネルに出ない ★",
          not any("負担" in n for n in names), names)

    print("\n[3] 匿名コードが本人のものと一致するか")
    SENT.clear()
    await flows.send_achievement(client, **ARGS)
    vals = {n: v for n, v, _ in dump(SENT[0]["embed"])["fields"]}
    expected = user_repo.anon_code(UID)
    check(f"匿名コード {expected}", any(expected in v for v in vals.values()), vals)

    print("\n[4] 金額の計算（負担率を省略した場合）")
    u, s_ = subsidy.calculate(800, 40.0)
    check(f"800円 × 負担40% → 利用者 {u}円 / 負担 {s_}円", u == 480 and s_ == 320)
    u2, _ = subsidy.calculate(590, 40.0)
    check(f"590円 × 負担40% → 利用者 {u2}円（添付画像と一致）", u2 == 354, u2)

    print("\n[5] チャンネル未設定のとき")
    await settings.set_value("channel_achievement", None)
    SENT.clear()
    sent = await flows.send_achievement(client, **ARGS)
    check("送信せず False を返す", sent is False and not SENT)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
