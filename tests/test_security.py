"""安全性の検証 — 認証情報の扱いと権限チェックが崩れていないか"""
import re, sys, os, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}\n{extra}")

ROOT = pathlib.Path(__file__).parent.parent
SRC = [p for p in ROOT.rglob("*.py")
       if "tests" not in p.parts and ".git" not in p.parts and "data" not in p.parts]

print("\n[1] 認証情報をログへ出していないか")
SECRET = re.compile(r'(token|password|paseto|secret|refresh|encryption_key)', re.I)
SAFE = re.compile(r'mask\(|\[:8\]|が空|がありません|を取得|に失敗|しました|が必要|正しく|更新|期限|残り')
risky = []
for p in SRC:
    for i, line in enumerate(p.read_text().splitlines(), 1):
        st = line.strip()
        if not re.search(r'\blog(ger)?\.(debug|info|warning|error|critical|exception)\b', st):
            continue
        if SECRET.search(st) and not SAFE.search(st):
            risky.append(f"      {p.relative_to(ROOT)}:{i}  {st[:100]}")
check("機密情報をログへ出していない", not risky, "\n".join(risky))

print("\n[2] コマンドの権限チェック")
missing, total = [], 0
for p in (ROOT / "cogs").glob("*.py"):
    src = p.read_text()
    for m in re.finditer(r'((?:^\s*@[^\n]+\n)+)\s*async def (\w+)\(', src, re.M):
        deco, fn = m.group(1), m.group(2)
        if "command(" not in deco:
            continue
        total += 1
        # self_checked() は「中で相手を見て判断する」ことを明示した印。
        # 付け忘れと見分けるために、印があるものだけを許す。
        if not any(d in deco for d in
                   ("admin_only()", "owner_only()", "self_checked()")):
            missing.append(f"      {p.name}: {fn}")
check(f"全{total}コマンドに権限チェックがある", not missing, "\n".join(missing))
check("コマンドが十分に定義されている", total >= 40, total)

print("\n[3] カード情報を保存していないか")
from db import models
cols = [c.name for t in models.Base.metadata.sorted_tables for c in t.columns]
banned = [c for c in cols if re.search(r'card_number|pan|cvv|security_code', c, re.I)]
check("カード番号・CVVの列が無い", not banned, banned)
check("card_id だけを保持している", "card_id" in cols)

print("\n[4] 認証情報は暗号化して保存しているか")
import sqlalchemy as sa
enc_expected = {
    ("mcd_accounts", "email"), ("mcd_accounts", "refresh_token"),
    ("kyash_accounts", "email"), ("kyash_accounts", "password"),
    ("kyash_accounts", "access_token"),
    ("mcd_tokens", "access_token"), ("mcd_tokens", "root_paseto"),
}
for table, base in sorted(enc_expected):
    t = models.Base.metadata.tables[table]
    has_enc = f"{base}_enc" in t.columns
    plain = base in t.columns
    check(f"{table}.{base} は暗号化列", has_enc and not plain,
          f"      _enc={has_enc} 平文={plain}")

print("\n[5] 残高は元帳から導出しているか")
# 残高そのものを持つ列があると、同時実行で必ず壊れる。
# 残高は ledger の合計として求める設計であることを確かめる。
balance_cols = [
    f"{t.name}.{c.name}"
    for t in models.Base.metadata.sorted_tables for c in t.columns
    if re.fullmatch(r'balance|amount_balance|current_balance', c.name)
]
check("残高そのものを持つ列が無い", not balance_cols, balance_cols)
check("ledger テーブルがある", "ledger" in models.Base.metadata.tables)

# 元帳への記帳が ledger.py だけを通っているか
writers = []
for p in SRC:
    if p.name in ("ledger.py", "models.py"):
        continue                      # 記帳の実装と、モデル定義そのものは除く
    for i, line in enumerate(p.read_text().splitlines(), 1):
        if re.search(r'(?<!class )\bLedger\s*\(', line) and "class " not in line:
            writers.append(f"      {p.relative_to(ROOT)}:{i}  {line.strip()[:80]}")
check("Ledger行を作るのは core/ledger.py だけ", not writers, "\n".join(writers))

# ロックを取らずに記帳できないことを確かめる
ledger_src = (ROOT / "core/ledger.py").read_text()
guarded = len(re.findall(r'assert_locked\(discord_id\)', ledger_src))
check(f"残高を動かす関数にロック確認がある（{guarded}箇所）", guarded >= 5, guarded)

print("\n[6] 決済をリトライしていないか")
client = (ROOT / "services/mcd/client.py").read_text()
m = re.search(r'async def authorise_order.*?(?=\n    async def )', client, re.S)
check("AuthoriseOrder に retry_on_401=False がある",
      m and "retry_on_401=False" in m.group(0))
saga = (ROOT / "core/saga.py").read_text()
check("決済中の状態が自動返金の対象外", "MCD_AUTHORISING, MCD_AUTHORISED" in saga.replace("\n", " ").replace("    ", ""))

print("\n[7] main.py に実際の認証情報が残っていないか")
main = (ROOT / "main.py").read_text()
tok = re.search(r'^DISCORD_TOKEN = "(.*)"', main, re.M)
key = re.search(r'^ENCRYPTION_KEY = "(.*)"', main, re.M)
check("DISCORD_TOKEN が空", tok and tok.group(1) == "", tok.group(1)[:12] if tok else "?")
check("ENCRYPTION_KEY が空", key and key.group(1) == "", key.group(1)[:12] if key else "?")

print(f"\n{'='*46}\n  成功 {ok} / 失敗 {fail}\n{'='*46}")
sys.exit(1 if fail else 0)
