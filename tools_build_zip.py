#!/usr/bin/env python3
"""
配布用のZIPを2つ作る。

  mcd-discord-bot.zip   … BOT本体（これだけで動く）
  mcd-receipt-site.zip  … 注文番号ページ（別の場所に置く用）

⚠️ 注文番号ページの描画は services/receipt_page.py が本体で、
   receipt_site/ 側はその写し。ずれていたらここで止める。
"""
import filecmp, pathlib, sys, zipfile

ROOT = pathlib.Path(__file__).parent
BOT = ROOT / "mcd-discord-bot.zip"
SITE = ROOT / "mcd-receipt-site.zip"

SKIP_DIRS = {".git", "__pycache__", "data", ".pytest_cache", ".ruff_cache"}
SKIP_SUFFIX = {".zip", ".pyc", ".db", ".log"}
SKIP_NAMES = {"tools_build_zip.py", "site_menu.html"}


def check_secrets_empty() -> None:
    """トークンが書き込まれたまま配らないように確かめる。"""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    for name in ("DISCORD_TOKEN", "ENCRYPTION_KEY"):
        line = f'{name} = ""'
        if line not in text:
            sys.exit(f"✗ main.py の {name} が空ではありません。消してから作り直してください。")


def check_page_copy() -> None:
    a = ROOT / "services" / "receipt_page.py"
    b = ROOT / "receipt_site" / "receipt_page.py"
    if not filecmp.cmp(a, b, shallow=False):
        sys.exit("✗ receipt_page.py が本体と写しでずれています。揃えてから作り直してください。")


def walk():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        if p.suffix in SKIP_SUFFIX or p.name in SKIP_NAMES:
            continue
        yield p, rel


def main() -> int:
    check_secrets_empty()
    check_page_copy()

    with zipfile.ZipFile(BOT, "w", zipfile.ZIP_DEFLATED) as z:
        n = 0
        for p, rel in walk():
            z.write(p, f"mcd-discord-bot/{rel.as_posix()}")
            n += 1
    print(f"✓ {BOT.name}  {n} ファイル  {BOT.stat().st_size/1048576:.2f} MB")

    with zipfile.ZipFile(SITE, "w", zipfile.ZIP_DEFLATED) as z:
        m = 0
        for p in sorted((ROOT / "receipt_site").iterdir()):
            if p.is_file() and p.suffix not in SKIP_SUFFIX:
                z.write(p, p.name)
                m += 1
    print(f"✓ {SITE.name}  {m} ファイル  {SITE.stat().st_size/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
