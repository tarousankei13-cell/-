import discord
from datetime import datetime
from typing import Optional, List, Dict
from config import Config


def _footer(text: str = "Shop Bot") -> Dict:
    return {"text": text, "icon_url": None}


def _ts() -> str:
    return datetime.utcnow().isoformat()


# ── Shop / Product embeds ────────────────────────────────────────────────────

def shop_home(categories) -> discord.Embed:
    e = discord.Embed(
        title="🛒  ショップへようこそ！",
        description="カテゴリーを選択して商品を見てみましょう！",
        color=Config.COLOR_SHOP,
        timestamp=datetime.utcnow()
    )
    if not categories:
        e.description = "現在商品カテゴリーが登録されていません。"
    for cat in categories:
        e.add_field(
            name=f"{cat['emoji']} {cat['name']}",
            value=cat['description'] or "商品一覧はこちら",
            inline=True
        )
    e.set_footer(text="Shop Bot • カテゴリーを選んでください")
    return e


def product_list(category, products, page: int = 1, total_pages: int = 1) -> discord.Embed:
    e = discord.Embed(
        title=f"{category['emoji']} {category['name']}",
        description=category["description"] or "",
        color=Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    if not products:
        e.description = "このカテゴリーに商品がありません。"
    for p in products:
        stock_str = "∞" if p["stock"] == -1 else str(p["stock"])
        e.add_field(
            name=f"#{p['id']}  {p['name']}",
            value=f"{Config.CURRENCY_EMOJI} **{p['price']:,}** {Config.CURRENCY_NAME}  |  在庫: `{stock_str}`\n{p['description'][:60]}{'…' if len(p['description']) > 60 else ''}",
            inline=False
        )
    e.set_footer(text=f"Shop Bot • ページ {page}/{total_pages}")
    return e


def product_detail(product, rating: Dict, reviews: List) -> discord.Embed:
    stock_str = "∞" if product["stock"] == -1 else str(product["stock"])
    stars = "⭐" * int(rating["avg"]) + "☆" * (5 - int(rating["avg"]))
    e = discord.Embed(
        title=f"{product['cat_emoji']} {product['name']}",
        description=product["description"],
        color=Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    e.add_field(name="💰 価格", value=f"**{product['price']:,}** {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="📦 在庫", value=stock_str, inline=True)
    e.add_field(name="🛍️ 売上", value=f"{product['sold_count']:,} 個", inline=True)
    e.add_field(name="⭐ 評価", value=f"{stars}  ({rating['avg']} / 5.0 · {rating['count']} 件)", inline=False)
    if product["image_url"]:
        e.set_image(url=product["image_url"])
    e.set_footer(text=f"商品ID: {product['id']} • Shop Bot")
    return e


def search_results(query: str, products) -> discord.Embed:
    e = discord.Embed(
        title=f"🔍  検索結果：「{query}」",
        color=Config.COLOR_INFO,
        timestamp=datetime.utcnow()
    )
    if not products:
        e.description = "一致する商品が見つかりませんでした。"
    for p in products[:10]:
        stock_str = "∞" if p["stock"] == -1 else str(p["stock"])
        e.add_field(
            name=f"#{p['id']} {p['cat_emoji']} {p['name']}",
            value=f"{Config.CURRENCY_EMOJI} **{p['price']:,}**  |  在庫: `{stock_str}`",
            inline=False
        )
    e.set_footer(text="Shop Bot • /shop で詳細を確認")
    return e


# ── Cart / Order embeds ──────────────────────────────────────────────────────

def cart_embed(items, total: int) -> discord.Embed:
    e = discord.Embed(
        title="🛒  ショッピングカート",
        color=Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    if not items:
        e.description = "カートに商品がありません。\n`/shop` で商品を追加しましょう！"
    else:
        lines = []
        for it in items:
            warn = " ⚠️ 在庫不足" if not it["in_stock"] else ""
            lines.append(f"**{it['name']}**  ×{it['quantity']}  = {it['price'] * it['quantity']:,} {Config.CURRENCY_NAME}{warn}")
        e.description = "\n".join(lines)
        e.add_field(name="💳 合計", value=f"**{total:,}** {Config.CURRENCY_NAME}", inline=False)
    e.set_footer(text="Shop Bot • /checkout でご注文")
    return e


def order_confirm(order_id: int, items, total: int) -> discord.Embed:
    e = discord.Embed(
        title="✅  ご注文ありがとうございます！",
        description=f"注文番号: **#{order_id:05d}**",
        color=Config.COLOR_SUCCESS,
        timestamp=datetime.utcnow()
    )
    lines = [f"{it['name']} × {it['quantity']}  = {it['price'] * it['quantity']:,} {Config.CURRENCY_NAME}" for it in items]
    e.add_field(name="📋 注文内容", value="\n".join(lines), inline=False)
    e.add_field(name="💳 合計金額", value=f"**{total:,}** {Config.CURRENCY_NAME}", inline=False)
    e.set_footer(text="Shop Bot • ご利用ありがとうございます")
    return e


def order_detail(order, order_items) -> discord.Embed:
    status_map = {
        "pending":   ("⏳", "保留中",   Config.COLOR_WARNING),
        "completed": ("✅", "完了",     Config.COLOR_SUCCESS),
        "cancelled": ("❌", "キャンセル", Config.COLOR_ERROR),
        "processing":("🔄", "処理中",   Config.COLOR_INFO),
    }
    icon, label, color = status_map.get(order["status"], ("❓", order["status"], Config.COLOR_PRIMARY))
    e = discord.Embed(
        title=f"📦  注文 #{order['id']:05d}",
        color=color,
        timestamp=datetime.utcnow()
    )
    e.add_field(name="ステータス", value=f"{icon} {label}", inline=True)
    e.add_field(name="注文日時",   value=order["created_at"][:10], inline=True)
    e.add_field(name="合計金額",   value=f"{order['total_price']:,} {Config.CURRENCY_NAME}", inline=True)
    lines = [f"{oi['product_name']} × {oi['quantity']}  = {oi['subtotal']:,} {Config.CURRENCY_NAME}" for oi in order_items]
    e.add_field(name="注文内容", value="\n".join(lines) or "—", inline=False)
    if order["notes"]:
        e.add_field(name="備考", value=order["notes"], inline=False)
    e.set_footer(text="Shop Bot")
    return e


# ── Economy embeds ───────────────────────────────────────────────────────────

def balance_embed(member: discord.Member, user_row) -> discord.Embed:
    e = discord.Embed(
        title=f"💰  {member.display_name} の残高",
        color=Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name=f"{Config.CURRENCY_EMOJI} 残高",    value=f"**{user_row['balance']:,}** {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="🛍️ 累計購入", value=f"{user_row['total_spent']:,} {Config.CURRENCY_NAME}", inline=True)
    e.set_footer(text="Shop Bot")
    return e


def leaderboard_embed(rows, guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title="🏆  残高ランキング",
        color=Config.COLOR_WARNING,
        timestamp=datetime.utcnow()
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"`{i+1}`"
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"ID:{row['user_id']}"
        lines.append(f"{medal} **{name}** — {row['balance']:,} {Config.CURRENCY_NAME}")
    e.description = "\n".join(lines) or "データがありません。"
    e.set_footer(text="Shop Bot")
    return e


def transaction_history(rows, member: discord.Member) -> discord.Embed:
    e = discord.Embed(
        title=f"📊  取引履歴 — {member.display_name}",
        color=Config.COLOR_INFO,
        timestamp=datetime.utcnow()
    )
    type_icons = {
        "purchase": "🛒", "daily": "🎁", "transfer_in": "📥",
        "transfer_out": "📤", "refund": "💸", "admin": "⚙️", "welcome": "🎉"
    }
    lines = []
    for tx in rows:
        icon = type_icons.get(tx["type"], "💱")
        sign = "+" if tx["amount"] >= 0 else ""
        lines.append(f"{icon} {tx['description']}  **{sign}{tx['amount']:,}**  `{tx['created_at'][:10]}`")
    e.description = "\n".join(lines) or "取引履歴がありません。"
    e.set_footer(text="Shop Bot")
    return e


# ── Review embeds ────────────────────────────────────────────────────────────

def reviews_embed(product, reviews) -> discord.Embed:
    rating_icons = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}
    e = discord.Embed(
        title=f"⭐  {product['name']} のレビュー",
        color=Config.COLOR_WARNING,
        timestamp=datetime.utcnow()
    )
    if not reviews:
        e.description = "まだレビューがありません。"
    for rv in reviews[:8]:
        stars = rating_icons.get(rv["rating"], str(rv["rating"]))
        e.add_field(
            name=f"{stars}  (ID:{rv['user_id']}) · {rv['created_at'][:10]}",
            value=rv["comment"] or "*(コメントなし)*",
            inline=False
        )
    e.set_footer(text="Shop Bot • /review でレビューを投稿")
    return e


# ── Admin / Stats embeds ─────────────────────────────────────────────────────

def stats_embed(stats: Dict) -> discord.Embed:
    e = discord.Embed(
        title="📊  ショップ統計",
        color=Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    e.add_field(name="📦 商品数",      value=f"{stats['total_products']:,}", inline=True)
    e.add_field(name="👥 ユーザー数",   value=f"{stats['total_users']:,}", inline=True)
    e.add_field(name="🛒 総注文数",    value=f"{stats['total_orders']:,}", inline=True)
    e.add_field(name="💰 総売上",      value=f"{stats['total_revenue']:,} {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="⏳ 保留中注文",  value=f"{stats['pending_orders']:,}", inline=True)
    if stats["top_product"]:
        e.add_field(name="🏆 売上No.1",  value=f"{stats['top_product']['name']} ({stats['top_product']['sold_count']}個)", inline=True)
    e.set_footer(text="Shop Bot")
    return e


# ── Generic embeds ───────────────────────────────────────────────────────────

def success(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅  {title}", description=description, color=Config.COLOR_SUCCESS, timestamp=datetime.utcnow())


def error(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"❌  {title}", description=description, color=Config.COLOR_ERROR, timestamp=datetime.utcnow())


def info(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"ℹ️  {title}", description=description, color=Config.COLOR_INFO, timestamp=datetime.utcnow())
