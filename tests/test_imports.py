"""全モジュールが読み込めるかを確認する"""
import sys, os, importlib, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULES = [
    "config", "emoji",
    "db.models", "db.session",
    "core.crypto", "core.locks", "core.ledger", "core.settings",
    "core.subsidy", "core.users", "core.saga",
    "services.mcd.protocol", "services.mcd.client", "services.mcd.menu",
    "services.mcd.accounts", "services.mcd.stores",
    "services.kyash.client", "services.kyash.accounts", "services.kyash.charge",
    "services.receipt", "services.tasks",
    "ui.embeds", "ui.panels", "ui.flows", "ui.menu_flows", "ui.admin_flows",
    "cogs._checks", "cogs.admin", "cogs.panel", "cogs.config_cmd",
    "cogs.account", "cogs.tasks",
    "main",
]
ok = fail = 0
for m in MODULES:
    try:
        importlib.import_module(m)
        print(f"  ✅ {m}"); ok += 1
    except Exception as e:
        print(f"  ❌ {m}\n      {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        fail += 1
print(f"\n{'='*46}\n  読み込み成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
