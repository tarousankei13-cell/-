import discord
from datetime import datetime
from typing import Optional, List, Dict
from config import Config


# ── Shop / Product embeds ────────────────────────────────────────────────────

def shop_home(categories, flash_sales=None) -> discord.Embed:
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
    if flash_sales:
        names = [f"🔥 **{s['product_name']}** `{s['discount_percent']}%OFF`" for s in flash_sales[:3]]
        e.add_field(name="⚡ 開催中のセール", value="\n".join(names), inline=False)
    e.set_footer(text="Shop Bot • カテゴリーを選んでください")
    return e


def product_list(category, products, page: int = 1, total_pages: int = 1, flash_sales: dict = None) -> discord.Embed:
    flash_sales = flash_sales or {}
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
        sale = flash_sales.get(p["id"])
        if sale:
            price_str = f"~~{p['price']:,}~~ → **{sale['sale_price']:,}** 🔥`{sale['discount_percent']}%OFF`"
        else:
            price_str = f"**{p['price']:,}**"
        digital_badge = " `💾DIGITAL`" if p.get("is_digital") else ""
        e.add_field(
            name=f"#{p['id']}  {p['name']}{digital_badge}",
            value=f"{Config.CURRENCY_EMOJI} {price_str} {Config.CURRENCY_NAME}  |  在庫: `{stock_str}`\n{p['description'][:60]}{'…' if len(p['description']) > 60 else ''}",
            inline=False
        )
    e.set_footer(text=f"Shop Bot • ページ {page}/{total_pages}")
    return e


def product_detail(product, rating: Dict, reviews: List, sale=None) -> discord.Embed:
    stock_str = "∞" if product["stock"] == -1 else str(product["stock"])
    avg = rating["avg"]
    filled = int(avg)
    stars = "⭐" * filled + "☆" * (5 - filled)

    e = discord.Embed(
        title=f"{product['cat_emoji']} {product['name']}",
        description=product["description"],
        color=Config.COLOR_FLASH if sale else Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )

    if sale:
        e.add_field(name="💰 価格", value=f"~~{sale['original_price']:,}~~ → **{sale['sale_price']:,}** {Config.CURRENCY_NAME}  🔥`{sale['discount_percent']}%OFF`", inline=True)
        e.add_field(name="⏰ セール終了", value=sale["end_time"][:16].replace("T", " "), inline=True)
    else:
        e.add_field(name="💰 価格", value=f"**{product['price']:,}** {Config.CURRENCY_NAME}", inline=True)

    e.add_field(name="📦 在庫",    value=stock_str, inline=True)
    e.add_field(name="🛍️ 売上",    value=f"{product['sold_count']:,} 個", inline=True)
    e.add_field(name="⭐ 評価",    value=f"{stars}  ({avg} / 5.0 · {rating['count']} 件)", inline=False)

    if product.get("is_digital"):
        e.add_field(name="💾 デジタル商品", value="購入後、自動でキーをDMでお届けします", inline=False)

    if product.get("tags"):
        tags = "  ".join(f"`{t.strip()}`" for t in product["tags"].split(",") if t.strip())
        e.add_field(name="🏷️ タグ", value=tags, inline=False)

    if product["image_url"]:
        e.set_image(url=product["image_url"])

    e.set_footer(text=f"商品ID: {product['id']} • Shop Bot")
    return e


def search_results(query: str, products, flash_sales: dict = None) -> discord.Embed:
    flash_sales = flash_sales or {}
    e = discord.Embed(
        title=f"🔍  検索結果：「{query}」",
        color=Config.COLOR_INFO,
        timestamp=datetime.utcnow()
    )
    if not products:
        e.description = "一致する商品が見つかりませんでした。"
    for p in products[:10]:
        stock_str = "∞" if p["stock"] == -1 else str(p["stock"])
        sale = flash_sales.get(p["id"])
        if sale:
            price_str = f"~~{p['price']:,}~~ **{sale['sale_price']:,}** 🔥"
        else:
            price_str = f"**{p['price']:,}**"
        e.add_field(
            name=f"#{p['id']} {p['cat_emoji']} {p['name']}",
            value=f"{Config.CURRENCY_EMOJI} {price_str}  |  在庫: `{stock_str}`",
            inline=False
        )
    e.set_footer(text="Shop Bot • /shop で詳細を確認")
    return e


# ── Cart / Order embeds ───────────────────────────────────────────────────────

def cart_embed(items, total: int, coupon_result: dict = None) -> discord.Embed:
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
            digital = " 💾" if it.get("is_digital") else ""
            lines.append(f"**{it['name']}**{digital}  ×{it['quantity']}  = {it['price'] * it['quantity']:,} {Config.CURRENCY_NAME}{warn}")
        e.description = "\n".join(lines)

        if coupon_result and coupon_result.get("valid"):
            e.add_field(name="💳 小計", value=f"{total:,} {Config.CURRENCY_NAME}", inline=True)
            e.add_field(name="🎟️ クーポン割引", value=f"-{coupon_result['discount']:,} {Config.CURRENCY_NAME}", inline=True)
            e.add_field(name="✅ 合計", value=f"**{coupon_result['final']:,}** {Config.CURRENCY_NAME}", inline=True)
        else:
            e.add_field(name="💳 合計", value=f"**{total:,}** {Config.CURRENCY_NAME}", inline=False)
    e.set_footer(text="Shop Bot • /checkout でご注文")
    return e


def order_confirm(order_id: int, items, total: int, discount: int = 0, gift_recipient=None) -> discord.Embed:
    e = discord.Embed(
        title="✅  ご注文ありがとうございます！",
        description=f"注文番号: **#{order_id:05d}**",
        color=Config.COLOR_SUCCESS,
        timestamp=datetime.utcnow()
    )
    lines = [f"{it['name']} × {it['quantity']}  = {it['price'] * it['quantity']:,} {Config.CURRENCY_NAME}" for it in items]
    e.add_field(name="📋 注文内容", value="\n".join(lines), inline=False)
    if discount > 0:
        e.add_field(name="🎟️ クーポン割引", value=f"-{discount:,} {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="💳 合計金額",  value=f"**{total:,}** {Config.CURRENCY_NAME}", inline=True)
    if gift_recipient:
        e.add_field(name="🎁 ギフト先", value=gift_recipient.mention, inline=False)
    e.set_footer(text="Shop Bot • ご利用ありがとうございます")
    return e


def order_detail(order, order_items) -> discord.Embed:
    status_map = {
        "pending":    ("⏳", "保留中",   Config.COLOR_WARNING),
        "completed":  ("✅", "完了",     Config.COLOR_SUCCESS),
        "cancelled":  ("❌", "キャンセル", Config.COLOR_ERROR),
        "processing": ("🔄", "処理中",   Config.COLOR_INFO),
    }
    icon, label, color = status_map.get(order["status"], ("❓", order["status"], Config.COLOR_PRIMARY))
    e = discord.Embed(title=f"📦  注文 #{order['id']:05d}", color=color, timestamp=datetime.utcnow())
    e.add_field(name="ステータス", value=f"{icon} {label}", inline=True)
    e.add_field(name="注文日時",   value=order["created_at"][:10], inline=True)
    if order.get("is_gift") and order.get("recipient_id"):
        e.add_field(name="🎁 ギフト先", value=f"<@{order['recipient_id']}>", inline=True)
    if order.get("original_price") and order["original_price"] != order["total_price"]:
        e.add_field(name="小計",    value=f"{order['original_price']:,} {Config.CURRENCY_NAME}", inline=True)
        e.add_field(name="割引",    value=f"-{order['original_price'] - order['total_price']:,} {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="合計金額",   value=f"{order['total_price']:,} {Config.CURRENCY_NAME}", inline=True)
    lines = [f"{oi['product_name']} × {oi['quantity']}  = {oi['subtotal']:,} {Config.CURRENCY_NAME}" for oi in order_items]
    e.add_field(name="注文内容", value="\n".join(lines) or "—", inline=False)
    if order["notes"]:
        e.add_field(name="備考", value=order["notes"], inline=False)
    e.set_footer(text="Shop Bot")
    return e


def checkout_preview(items, subtotal: int, coupon_result: dict = None) -> discord.Embed:
    e = discord.Embed(title="🛒  注文内容の確認", color=Config.COLOR_PRIMARY, timestamp=datetime.utcnow())
    lines = [f"{it['name']} × {it['quantity']}  = {it['price'] * it['quantity']:,} {Config.CURRENCY_NAME}" for it in items]
    e.add_field(name="📋 注文内容", value="\n".join(lines), inline=False)
    e.add_field(name="💳 小計",   value=f"{subtotal:,} {Config.CURRENCY_NAME}", inline=True)
    if coupon_result and coupon_result.get("valid"):
        e.add_field(name="🎟️ クーポン割引", value=f"-{coupon_result['discount']:,} {Config.CURRENCY_NAME}", inline=True)
        e.add_field(name="✅ お支払い合計", value=f"**{coupon_result['final']:,}** {Config.CURRENCY_NAME}", inline=True)
    e.set_footer(text="下のボタンで注文を確定してください")
    return e


# ── Economy embeds ────────────────────────────────────────────────────────────

def balance_embed(member: discord.Member, user_row, tier=None) -> discord.Embed:
    e = discord.Embed(
        title=f"💰  {member.display_name} の残高",
        color=tier["color"] if tier else Config.COLOR_PRIMARY,
        timestamp=datetime.utcnow()
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name=f"{Config.CURRENCY_EMOJI} 残高",  value=f"**{user_row['balance']:,}** {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="🛍️ 累計購入", value=f"{user_row['total_spent']:,} {Config.CURRENCY_NAME}", inline=True)
    if tier:
        e.add_field(name="🏅 ランク",    value=f"{tier['emoji']} {tier['name']}", inline=True)
    if user_row.get("daily_streak", 0) > 0:
        e.add_field(name="🔥 ストリーク", value=f"{user_row['daily_streak']} 日連続", inline=True)
    e.set_footer(text="Shop Bot")
    return e


def leaderboard_embed(rows, guild: discord.Guild, mode: str = "balance") -> discord.Embed:
    title = "🏆  残高ランキング" if mode == "balance" else "🛍️  累計購入ランキング"
    e = discord.Embed(title=title, color=Config.COLOR_WARNING, timestamp=datetime.utcnow())
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"ID:{row['user_id']}"
        val = row["balance"] if mode == "balance" else row["total_spent"]
        lines.append(f"{medal} **{name}** — {val:,} {Config.CURRENCY_NAME}")
    e.description = "\n".join(lines) or "データがありません。"
    e.set_footer(text="Shop Bot")
    return e


def transaction_history(rows, member: discord.Member) -> discord.Embed:
    e = discord.Embed(title=f"📊  取引履歴 — {member.display_name}", color=Config.COLOR_INFO, timestamp=datetime.utcnow())
    type_icons = {
        "purchase": "🛒", "daily": "🎁", "transfer_in": "📥", "transfer_out": "📤",
        "refund": "💸", "admin": "⚙️", "welcome": "🎉", "achievement": "🏆",
        "gift_sent": "🎁", "referral": "🤝"
    }
    lines = []
    for tx in rows:
        icon = type_icons.get(tx["type"], "💱")
        sign = "+" if tx["amount"] >= 0 else ""
        lines.append(f"{icon} {tx['description']}  **{sign}{tx['amount']:,}**  `{tx['created_at'][:10]}`")
    e.description = "\n".join(lines) or "取引履歴がありません。"
    e.set_footer(text="Shop Bot")
    return e


# ── Achievement embeds ────────────────────────────────────────────────────────

def achievements_embed(all_achievements, user_achievements) -> discord.Embed:
    earned_ids = {a["id"] for a in user_achievements}
    e = discord.Embed(title="🏆  実績一覧", color=Config.COLOR_GOLD, timestamp=datetime.utcnow())
    earned_count = len(earned_ids)
    e.description = f"解除済み: **{earned_count}** / {len(all_achievements)} 個"
    for ach in all_achievements:
        if ach["is_secret"] and ach["id"] not in earned_ids:
            continue
        status = "✅" if ach["id"] in earned_ids else "⬜"
        reward_str = f"  (+{ach['reward']} {Config.CURRENCY_NAME})" if ach["reward"] else ""
        e.add_field(
            name=f"{status} {ach['icon']} {ach['name']}{reward_str}",
            value=ach["description"],
            inline=True
        )
    return e


def new_achievement_embed(ach) -> discord.Embed:
    e = discord.Embed(
        title=f"🎉  実績解除！",
        description=f"**{ach['icon']} {ach['name']}**\n{ach['description']}",
        color=Config.COLOR_GOLD,
        timestamp=datetime.utcnow()
    )
    if ach["reward"]:
        e.add_field(name="報酬", value=f"+{ach['reward']:,} {Config.CURRENCY_NAME}")
    return e


# ── Flash sale embeds ─────────────────────────────────────────────────────────

def flash_sale_announce(sale, product) -> discord.Embed:
    e = discord.Embed(
        title="⚡  フラッシュセール開始！",
        description=f"**{product['name']}** が限定価格でご購入いただけます！",
        color=Config.COLOR_FLASH,
        timestamp=datetime.utcnow()
    )
    e.add_field(name="通常価格",    value=f"~~{sale['original_price']:,}~~ {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="🔥 セール価格", value=f"**{sale['sale_price']:,}** {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="割引率",       value=f"**{sale['discount_percent']}%OFF**", inline=True)
    e.add_field(name="⏰ 終了時間",  value=sale["end_time"][:16].replace("T", " "), inline=False)
    if product["image_url"]:
        e.set_thumbnail(url=product["image_url"])
    e.set_footer(text="Shop Bot • /shop で購入！")
    return e


# ── Review embeds ─────────────────────────────────────────────────────────────

def reviews_embed(product, reviews) -> discord.Embed:
    rating_icons = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}
    e = discord.Embed(title=f"⭐  {product['name']} のレビュー", color=Config.COLOR_WARNING, timestamp=datetime.utcnow())
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


# ── Admin / Stats embeds ──────────────────────────────────────────────────────

def stats_embed(stats: Dict) -> discord.Embed:
    e = discord.Embed(title="📊  ショップ統計", color=Config.COLOR_PRIMARY, timestamp=datetime.utcnow())
    e.add_field(name="📦 商品数",        value=f"{stats['total_products']:,}", inline=True)
    e.add_field(name="👥 ユーザー数",    value=f"{stats['total_users']:,}", inline=True)
    e.add_field(name="🛒 総注文数",      value=f"{stats['total_orders']:,}", inline=True)
    e.add_field(name="💰 総売上",        value=f"{stats['total_revenue']:,} {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="📅 本日の売上",    value=f"{stats['today_revenue']:,} {Config.CURRENCY_NAME}", inline=True)
    e.add_field(name="📅 本日の注文",    value=f"{stats['today_orders']} 件", inline=True)
    e.add_field(name="⏳ 保留中注文",    value=f"{stats['pending_orders']:,}", inline=True)
    e.add_field(name="🎟️ クーポン使用",  value=f"{stats['total_coupons_used']:,} 回", inline=True)
    e.add_field(name="🎫 チケット",      value=f"{stats['active_tickets']} 件", inline=True)
    if stats["top_product"]:
        e.add_field(name="🏆 売上No.1", value=f"{stats['top_product']['name']} ({stats['top_product']['sold_count']}個)", inline=False)
    e.set_footer(text="Shop Bot")
    return e


# ── Generic embeds ────────────────────────────────────────────────────────────

def success(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅  {title}", description=description, color=Config.COLOR_SUCCESS, timestamp=datetime.utcnow())

def error(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"❌  {title}", description=description, color=Config.COLOR_ERROR, timestamp=datetime.utcnow())

def info(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"ℹ️  {title}", description=description, color=Config.COLOR_INFO, timestamp=datetime.utcnow())

def warning(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"⚠️  {title}", description=description, color=Config.COLOR_WARNING, timestamp=datetime.utcnow())
