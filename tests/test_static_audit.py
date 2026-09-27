"""静的監査: 実行しなくても分かる不整合を洗い出す。

Bot を起動せずにソースを読んで、次のような「気付きにくい壊れ方」を検出する。

* custom_id の衝突・未登録の Persistent View (再起動後にボタンが死ぬ)
* 到達できない状態遷移
* 案内文の無いエラーコード
* CREATE TABLE と後方互換列の食い違い (古い DB で落ちる)
* トランザクションの入れ子 (実行時にしか出ない例外)
* 秘密情報をログへ渡していないか
* defer の後に Modal を出していないか (Discord が拒否する)
* 金額を float で扱っていないか
"""
from __future__ import annotations
import ast, re, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import config  # noqa: E402

FAIL: list[str] = []
OK: list[str] = []

def check(cond: bool, label: str) -> None:
    (OK if cond else FAIL).append(label)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")

sources = {p.name: p.read_text() for p in BASE.glob('*.py')}
trees = {name: ast.parse(src) for name, src in sources.items()}

print("=== 1. custom_id の衝突 ===")
ids = {k: v for k, v in vars(config.CustomID).items() if not k.startswith('_')}
check(len(set(ids.values())) == len(ids),
      f"custom_id に重複がない ({len(ids)}件)")
check(all(isinstance(v, str) and v.startswith('chargebot:') for v in ids.values()),
      "custom_id が共通の接頭辞を持つ")
check(all(len(v) <= 100 for v in ids.values()),
      "custom_id が Discord の上限 (100文字) に収まる")

print("\n=== 2. Persistent View の登録漏れ ===")
ui_src = sources['ui.py']
used_ids = set(re.findall(r'custom_id=config\.CustomID\.(\w+)', ui_src))
check(used_ids <= set(ids), f"存在しない custom_id を参照していない ({len(used_ids)}件使用)")
unused = set(ids) - used_ids
check(not unused, f"未使用の custom_id がない ({sorted(unused)})")
# timeout=None の View が main.py で add_view されているか
persistent = set()
for node in ast.walk(trees['ui.py']):
    if isinstance(node, ast.ClassDef):
        src = ast.get_source_segment(ui_src, node) or ''
        if 'timeout=None' in src and 'custom_id=config.CustomID' in src:
            persistent.add(node.name)
registered = set(re.findall(r'self\.add_view\(ui\.(\w+)\(\)\)', sources['main.py']))
check(persistent <= registered,
      f"Persistent View がすべて再登録されている (未登録: {sorted(persistent - registered)})")

print("\n=== 3. 状態遷移の到達性 ===")
for name, table, terminal in (
    ("取引", config.ALLOWED_TRANSITIONS, None),
    ("申請", config.REQUEST_TRANSITIONS, None),
    ("返金申請", config.REFUND_TRANSITIONS, None),
):
    states = set(table)
    targets = {t for v in table.values() for t in v}
    check(targets <= states, f"{name}: 遷移先がすべて定義済みの状態")
    dead = [s for s, v in table.items() if not v]
    check(bool(dead), f"{name}: 終端状態がある ({dead})")

print("\n=== 4. エラーコードの網羅 ===")
codes = {v for k, v in vars(config.ErrorCode).items() if not k.startswith('_')}
check(codes <= set(config.USER_ERROR_MESSAGES),
      f"全エラーコードに利用者向けメッセージ ({len(codes)}件)")
check(codes <= set(config.USER_ERROR_NEXT_ACTIONS),
      "全エラーコードに次の行動の案内")
used_codes = set()
for name, src in sources.items():
    used_codes |= set(re.findall(r'ErrorCode\.(\w+)', src))
declared = {k for k in vars(config.ErrorCode) if not k.startswith('_')}
check(used_codes <= declared,
      f"未定義のエラーコードを参照していない ({len(used_codes)}件を使用)")
never_used = declared - used_codes
check(not never_used, f"使われていないエラーコードがない ({sorted(never_used)})")

print("\n=== 5. DB の列名 ===")
schema_src = sources['database.py']
tables: dict[str, set[str]] = {}
for match in re.finditer(r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n    \)', schema_src, re.S):
    name, body = match.group(1), match.group(2)
    cols = set()
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r'^([a-z_][a-z0-9_]*)\s+(INTEGER|TEXT|REAL|BLOB|NUMERIC)', line)
        if m:
            cols.add(m.group(1))
    tables[name] = cols
import database  # noqa: E402

for table, column, _ddl in database._FORWARD_COMPAT_COLUMNS:
    check(table in tables and column in tables[table],
          f"後方互換列 {table}.{column} が CREATE TABLE にもある")

print("\n=== 6. 書き込みトランザクションの入れ子 ===")
# BEGIN IMMEDIATE は run(write=True) の中に1箇所だけ置く。
# 各メソッドが個別に BEGIN すると「トランザクションの入れ子」で落ちる。
for name, src in sources.items():
    hits = re.findall(r'conn\.execute\(\s*["\']BEGIN', src)
    if name == 'database.py':
        check(len(hits) == 1, f"{name}: BEGIN は run(write=True) の1箇所だけ ({len(hits)}件)")
    else:
        check(not hits, f"{name}: BEGIN を手書きしていない")

print("\n=== 7. 秘密情報のログ出力 ===")
# 秘密情報「そのもの」を書式に渡していないかを見る。
# 説明文の中に OTP などの語が出るのは問題ないので、%s へ渡す引数だけを対象にする。
secret_args = re.compile(
    r'logger\.(?:debug|info|warning|error|exception)\([^)]*?,\s*'
    r'(?:[\w.]*\b(?:access_token|password|otp_code|cookie|authorization|secret|'
    r'token_enc|client_secret)\b[\w.]*)\s*[,)]',
    re.I | re.S,
)
for name, src in sources.items():
    hits = secret_args.findall(src)
    check(not hits, f"{name}: 秘密情報の値をログの引数に渡していない")
# マスキング関数が実際に使われているか
check('sanitize_for_log' in sources['utils.py']
      and 'SanitizingFormatter' in sources['main.py'],
      "ログのマスキング機構が組み込まれている")

print("\n=== 8. defer とレスポンスの順序 ===")
# Modal を出す前に defer していないか (defer 後は send_modal できない)
bad = []
for name, src in sources.items():
    for match in re.finditer(r'async def (\w+)\(.*?\n(?=    async def |\Z)', src, re.S):
        body = match.group(0)
        if 'send_modal' not in body:
            continue
        defer_pos = body.find('response.defer')
        modal_pos = body.find('response.send_modal')
        if defer_pos != -1 and defer_pos < modal_pos:
            bad.append(f"{name}:{match.group(1)}")
check(not bad, f"defer の後に send_modal していない ({bad})")

print("\n=== 9. money は Decimal / int のみ ===")
money_float = []
for name, src in sources.items():
    for m in re.finditer(r'(float\(\s*(?:amount|price|balance|credited|received)\w*\s*\))', src):
        money_float.append(f"{name}: {m.group(1)}")
check(not money_float, f"金額を float にしていない ({money_float})")

print("\n=== 10. 送金リンクの完全な URL を保存しない ===")
for name, src in sources.items():
    check('INSERT INTO charge_transactions' not in src or 'link_url' not in src,
          f"{name}: 取引に link_url を保存していない")

print("\n" + "=" * 70)
print(f"結果: {len(OK)} 件成功 / {len(FAIL)} 件失敗")
for f in FAIL:
    print(f"  x {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
