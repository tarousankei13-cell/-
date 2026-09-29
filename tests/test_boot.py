"""起動シーケンスの検証 — Discordへ接続せずに、cogの読み込みとコマンド登録を確かめる"""
import asyncio, sys, os, tempfile, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from discord.ext import commands

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")

async def main():
    tmp = tempfile.mkdtemp()
    import main as bot_main
    bot_main.DATABASE_URL = f"sqlite+aiosqlite:///{tmp}/boot.db"
    bot_main.ENCRYPTION_KEY = "dGVzdC1rZXktMzJieXRlcy1mb3ItdGVzdGluZy0xMjM0"
    bot_main.DISCORD_TOKEN = "dummy"

    print("\n[1] BOTの組み立て")
    bot = bot_main.McdBot()
    bot.database_url = bot_main.DATABASE_URL
    bot.encryption_key = bot_main.ENCRYPTION_KEY
    check("command_prefix に / を使っていない",
          bot.command_prefix is commands.when_mentioned)
    check("message_content インテントを要求していない", not bot.intents.message_content)

    print("\n[2] 初期化とcogの読み込み")
    from core.crypto import init_cipher
    from db.session import init_db, close_db
    init_cipher(bot.encryption_key)
    await init_db(bot.database_url)
    from core import settings
    await settings.load_all()
    await bot._load_cogs()
    check(f"cogを{len(bot.cogs)}個読み込んだ", len(bot.cogs) >= 5, list(bot.cogs))

    print("\n[3] スラッシュコマンドの登録")
    cmds = bot.tree.get_commands()
    names = [c.name for c in cmds]
    check(f"トップレベル {len(names)}件: {', '.join(sorted(names))}", len(names) > 0)
    check("名前の重複がない（二重表示の原因）",
          len(names) == len(set(names)), [n for n in names if names.count(n) > 1])

    def full_path(group, cmd) -> str:
        """親グループを含めた呼び出し名。Discordはこの単位で区別する。"""
        parts, node = [cmd.name], getattr(cmd, "parent", None)
        while node is not None:
            parts.append(node.name)
            node = getattr(node, "parent", None)
        return "/" + " ".join(reversed(parts))

    total = 0
    for c in cmds:
        if isinstance(c, discord.app_commands.Group):
            # 同じ名前でも親グループが違えば別コマンドなので、
            # 呼び出し名まで含めて重複を見る。
            #（例: /config menu interval と /config store interval は別）
            paths = [full_path(c, s) for s in c.walk_commands()]
            dup = sorted({p for p in paths if paths.count(p) > 1})
            check(f"/{c.name} のサブコマンド {len(paths)}件に重複なし", not dup, dup)
            total += len(paths)
        else:
            total += 1
    check(f"総コマンド数 {total}件", total >= 20, total)

    print("\n[4] 利用者向けコマンドが無いこと（利用者はパネル操作のみ）")
    USER_WORDS = {"order", "balance", "charge", "history", "hex"}
    exposed = [n for n in names if n in USER_WORDS]
    check("利用者が使えるスラッシュコマンドは無い", not exposed, exposed)

    print("\n[5] 常設パネルのボタン")
    from ui.panels import PERSISTENT_VIEWS
    all_ids = []
    for factory in PERSISTENT_VIEWS:
        view = factory()
        check(f"{factory.__name__} は timeout=None", view.timeout is None)
        ids = [c.custom_id for c in view.children if hasattr(c, "custom_id")]
        check(f"{factory.__name__} の全ボタンにcustom_idがある",
              all(ids) and len(ids) == len(view.children), ids)
        all_ids += ids
    check("custom_idが全体で一意", len(all_ids) == len(set(all_ids)),
          [i for i in all_ids if all_ids.count(i) > 1])
    check("命名規則 panel:*:* に従っている",
          all(i.startswith("panel:") and i.count(":") == 2 for i in all_ids), all_ids)

    print("\n[6] 永続ビューの登録")
    for factory in PERSISTENT_VIEWS:
        bot.add_view(factory())
    check("add_view が成功", True)

    print("\n[7] コマンド定義のハッシュ（同期スキップ判定）")
    sig1 = bot._command_signature()
    sig2 = bot._command_signature()
    check("同じ定義なら同じハッシュ", sig1 == sig2)
    check("ハッシュが生成される", len(sig1) == 64, sig1[:16])

    print("\n[8] 絵文字がすべてUnicode（カスタム絵文字なし）")
    import emoji as E
    customs = [k for k, v in vars(E).items()
               if isinstance(v, str) and v.startswith("<:")]
    check("カスタム絵文字を使っていない", not customs, customs)

    await close_db()
    print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
