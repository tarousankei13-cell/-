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
import utils  # noqa: E402

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

print("\n=== 10. 二重応答 ===")
# 1つの操作に2回応答すると Discord がエラーを返す。
# 分岐ごとに応答するのは正しいので、「応答の後に必ず return がある」ことを見る。
for name, src in sources.items():
    tree = trees[name]
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        body = ast.get_source_segment(src, node) or ''
        sends = body.count('interaction.response.send_message')
        if sends < 2:
            continue
        returns = len(re.findall(r'^\s+return\b', body, re.M))
        check(sends <= returns + 1,
              f"{name}:{node.name} 応答 {sends} 回に対し return {returns} 回 "
              f"(分岐ごとに終わっている)")

print("\n=== 11. Embed をループで組み立てる箇所の上限 ===")
# フィールドは25個まで。ページ送りの1ページ分が25を超えていないか。
for name in ('commands.py', 'main.py'):
    src = sources[name]
    for m in re.finditer(r'per_page\s*=\s*(\d+)', src):
        size = int(m.group(1))
        check(size <= 20, f"{name}: 1ページ {size} 件 (Embed のフィールド上限25に収まる)")
    for m in re.finditer(r'for \w+ in [^\n]*\[:(\d+)\]', src):
        limit = int(m.group(1))
        # そのループが add_field を含むかを粗く見る
        tail = src[m.end():m.end() + 400]
        if '.add_field(' in tail.split('\n\n')[0]:
            check(limit <= 25,
                  f"{name}: ループ上限 {limit} が Embed のフィールド上限を超えない")

print("\n=== 12. 再試行しても無駄なエラーを再試行しない ===")
check('NON_RETRYABLE_ERRORS' in sources['charge_service.py'],
      "再試行の判定で NON_RETRYABLE_ERRORS を使っている")

print("\n=== 13. 使われていない定数が残っていない ===")
# config.py 自身の関数 (provider_family など) から参照される定数も「使用中」とみなす。
# 定義部だけを除きたいので、config.py は関数本体のみを対象に含める。
_config_tree = ast.parse(sources['config.py'])
_config_helpers = "\n".join(
    ast.get_source_segment(sources['config.py'], node) or ''
    for node in _config_tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
)
used_src = "\n".join(
    [v for k, v in sources.items() if k != 'config.py'] + [_config_helpers]
)
dead = [
    key for key, value in vars(config).items()
    if key.isupper() and not key.startswith('_')
    and key not in ('BOT_VERSION', 'SCHEMA_VERSION', 'BASE_DIR', 'LOG_FORMAT')
    and f"config.{key}" not in used_src
    and key not in used_src
]
check(not dead, f"未使用の設定定数がない ({sorted(dead)})")

print("\n=== 14. 利用者へ見せるリンクの検証 ===")
check('is_safe_link' in sources['utils.py']
      and 'is_safe_link' in sources['commands.py'],
      "表示するリンクは形式を検証してから登録する")

print("\n=== 15. 受け取り方の設定 ===")
check(set(config.KYASH_MODE_PROVIDER) == set(config.KYASH_MODE_LABELS),
      "Kyash の方式に漏れがない")
check(set(config.KYASH_MODE_PROVIDER.values()) <= set(config.KYASH_PROVIDERS),
      "Kyash の方式が実在の provider を指している")
check(set(config.PAYPAY_MODE_LABELS) == set(config.PAYPAY_MODE_DESCRIPTIONS),
      "PayPay の方式に説明が揃っている")

print("\n=== 16. 送金リンクの完全な URL を保存しない ==="[:26] + " ===")
for name, src in sources.items():
    check('INSERT INTO charge_transactions' not in src or 'link_url' not in src,
          f"{name}: 取引に link_url を保存していない")

print("\n=== 17. Discord へ渡す絵文字 ===")
# ボタン・選択メニューに絵文字でない文字を渡すと Discord は 400 を返し、
# そのメッセージ全体が送れなくなる。利用者には
# 「アプリケーションは応答しませんでした」と見え、ボタンが押せなくなる。
# (実際に Litecoin の "Ł" (U+0141) でこの不具合が起きた)
for dict_name in ("STATUS_EMOJI", "PROVIDER_EMOJI", "RANK_MEDALS",
                  "SHOP_ITEM_TYPE_EMOJI"):
    table = getattr(config, dict_name, None)
    check(isinstance(table, dict), f"config.{dict_name} がある")
    for key, value in (table or {}).items():
        points = " ".join(f"U+{ord(c):04X}" for c in str(value))
        check(utils.is_discord_emoji(value),
              f"config.{dict_name}[{key!r}] が絵文字 ({value!r} {points})")

# ui.py の emoji= に書かれたリテラルも同じ規則で検査する
_emoji_literals = re.findall(r'emoji\s*=\s*(["\'])(.*?)\1', sources['ui.py'])
check(len(_emoji_literals) >= 20,
      f"emoji= のリテラルを検査できている ({len(_emoji_literals)} 件)")
for _quote, literal in _emoji_literals:
    points = " ".join(f"U+{ord(c):04X}" for c in literal)
    check(utils.is_discord_emoji(literal),
          f"ui.py の emoji={literal!r} が絵文字 ({points})")

# 実行時に組み立てる絵文字は必ず検証を通してから渡す
for pattern in ("emoji=utils.safe_emoji(", "emoji=config."):
    found = sources['ui.py'].count(pattern)
    if pattern == "emoji=config.":
        check(found == 0,
              "設定値の絵文字を検証せずに渡していない "
              f"(直接渡し {found} 箇所)")
    else:
        check(found >= 2, f"設定値の絵文字は safe_emoji を通す ({found} 箇所)")

print("\n=== 18. 応答できないまま終わらせない ===")
# 400 で拒否されても、内容を削って必ず何かを返す (無応答 = ボタンが死ぬ)
check('async def safe_send_modal' in sources['ui.py'],
      "入力欄を開けなかったときの受け皿がある")
for name in ('main.py', 'commands.py'):
    check('interaction.response.send_modal' not in sources[name],
          f"{name}: 入力欄は safe_send_modal 経由で開く")
check(sources['ui.py'].count('interaction.response.send_modal') == 1,
      "ui.py で直接 send_modal するのは safe_send_modal だけ")

print("\n=== 19. コンポーネントの文字数上限 ===")
# Discord は上限を超えた内容を 400 で拒否し、メッセージごと送れなくなる。
# 結果は絵文字のときと同じで、ボタンが押せなくなったように見える。
_COMPONENT_LIMITS = {
    "SelectOption": {"label": 100, "value": 100, "description": 100},
    "TextInput": {"label": 45, "placeholder": 100},
    "button": {"label": 80, "custom_id": 100},
    "Select": {"placeholder": 150, "custom_id": 100},
}
_limit_violations: list[str] = []
_checked_fields = 0
_ui_tree = ast.parse(sources['ui.py'])
for _node in ast.walk(_ui_tree):
    if isinstance(_node, ast.ClassDef):
        # class Foo(discord.ui.Modal, title="...") の形
        for _kw in _node.keywords:
            if _kw.arg == "title" and isinstance(_kw.value, ast.Constant):
                _checked_fields += 1
                if len(str(_kw.value.value)) > 45:
                    _limit_violations.append(
                        f"ui.py:{_node.lineno} {_node.name}.title "
                        f"{len(str(_kw.value.value))}文字 (上限 45)")
        continue
    if not isinstance(_node, ast.Call):
        continue
    _name = ""
    if isinstance(_node.func, ast.Attribute):
        _name = _node.func.attr
    elif isinstance(_node.func, ast.Name):
        _name = _node.func.id
    _limits = _COMPONENT_LIMITS.get(_name)
    if _limits is None:
        continue
    for _kw in _node.keywords:
        _cap = _limits.get(_kw.arg or "")
        if _cap is None:
            continue
        _length = None
        if isinstance(_kw.value, ast.Constant) and isinstance(_kw.value.value, str):
            _length = len(_kw.value.value)
        elif (isinstance(_kw.value, ast.Call)
              and isinstance(_kw.value.func, ast.Attribute)
              and _kw.value.func.attr == "truncate"
              and len(_kw.value.args) >= 2
              and isinstance(_kw.value.args[1], ast.Constant)
              and isinstance(_kw.value.args[1].value, int)):
            _length = _kw.value.args[1].value
        if _length is None:
            continue
        _checked_fields += 1
        if _length > _cap:
            _limit_violations.append(
                f"ui.py:{_node.lineno} {_name}.{_kw.arg} "
                f"{_length}文字 (上限 {_cap})")
check(_checked_fields >= 30,
      f"コンポーネントの文字数を検査できている ({_checked_fields} 箇所)")
check(not _limit_violations,
      f"コンポーネントの文字数が上限内 ({_limit_violations})")
for _key, _value in vars(config.CustomID).items():
    if _key.startswith("_") or not isinstance(_value, str):
        continue
    check(len(_value) <= 100, f"CustomID.{_key} が100文字以内 ({len(_value)})")

print("\n" + "=" * 70)
print(f"結果: {len(OK)} 件成功 / {len(FAIL)} 件失敗")
for f in FAIL:
    print(f"  x {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
