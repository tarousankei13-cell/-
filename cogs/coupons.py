import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from config import Config
import utils.embeds as E


class CreateCouponModal(discord.ui.Modal, title="クーポンを作成"):
    code          = discord.ui.TextInput(label="クーポンコード",             max_length=20)
    discount_type = discord.ui.TextInput(label="種別: percent / fixed",     default="percent", max_length=10)
    discount_val  = discord.ui.TextInput(label="割引値（%または固定額）",   max_length=6)
    min_purchase  = discord.ui.TextInput(label="最低購入金額（0=制限なし）", default="0", max_length=10)
    expires_days  = discord.ui.TextInput(label="有効日数（0=無期限）",       default="30", max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val  = int(self.discount_val.value)
            minp = int(self.min_purchase.value)
            days = int(self.expires_days.value)
            dtype = self.discount_type.value.lower().strip()
            if dtype not in ("percent", "fixed"):
                raise ValueError
            if dtype == "percent" and not (1 <= val <= 100):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                embed=E.error("入力エラー", "種別は `percent` か `fixed`、割引値は整数で入力してください。"),
                ephemeral=True
            )
            return

        from datetime import timedelta
        expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
        db = interaction.client.db
        try:
            cid = await db.create_coupon(
                code=self.code.value.upper(),
                discount_type=dtype,
                discount_value=val,
                min_purchase=minp,
                max_uses=-1,
                per_user_limit=1,
                expires_at=expires_at,
                created_by=interaction.user.id
            )
        except Exception:
            await interaction.response.send_message(
                embed=E.error("作成失敗", "同じコードのクーポンが既に存在します。"),
                ephemeral=True
            )
            return

        label = f"{val}%OFF" if dtype == "percent" else f"{val:,}{Config.CURRENCY_NAME}OFF"
        await interaction.response.send_message(
            embed=E.success(
                "クーポンを作成しました！",
                f"コード: `{self.code.value.upper()}`\n割引: **{label}**\n最低購入: {minp:,} {Config.CURRENCY_NAME}\n有効期限: {expires_at[:10] if expires_at else '無期限'}"
            ),
            ephemeral=True
        )


class Coupons(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    # ── /coupon check ──────────────────────────────────────────────────────

    @app_commands.command(name="coupon", description="クーポンコードを確認します")
    @app_commands.describe(code="クーポンコード")
    async def coupon_check(self, interaction: discord.Interaction, code: str):
        result = await self.db.validate_coupon(code, interaction.user.id, 999999999)
        if not result["valid"] and result.get("reason") == "クーポンコードが見つかりません。":
            await interaction.response.send_message(embed=E.error("無効なコード", result["reason"]), ephemeral=True)
            return

        coupon = await self.db.get_coupon(code)
        if not coupon:
            await interaction.response.send_message(embed=E.error("クーポンが見つかりません"), ephemeral=True)
            return

        label = f"{coupon['discount_value']}%OFF" if coupon["discount_type"] == "percent" else f"{coupon['discount_value']:,}{Config.CURRENCY_NAME}OFF"
        uses_str = f"{coupon['uses_count']}/{coupon['max_uses']}" if coupon["max_uses"] != -1 else f"{coupon['uses_count']} 回使用済"
        expire_str = coupon["expires_at"][:10] if coupon["expires_at"] else "無期限"

        embed = discord.Embed(title=f"🎟️  クーポン `{coupon['code']}`", color=Config.COLOR_SUCCESS)
        embed.add_field(name="割引", value=label, inline=True)
        embed.add_field(name="最低購入", value=f"{coupon['min_purchase']:,} {Config.CURRENCY_NAME}", inline=True)
        embed.add_field(name="有効期限", value=expire_str, inline=True)
        embed.add_field(name="使用回数", value=uses_str, inline=True)
        embed.add_field(name="ステータス", value="✅ 有効" if coupon["is_active"] else "❌ 無効", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Admin coupon commands ──────────────────────────────────────────────

    @app_commands.command(name="coupon_create", description="クーポンを作成します（管理者専用）")
    async def coupon_create(self, interaction: discord.Interaction):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        await interaction.response.send_modal(CreateCouponModal())

    @app_commands.command(name="coupon_list", description="クーポン一覧を表示します（管理者専用）")
    async def coupon_list(self, interaction: discord.Interaction):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return

        coupons = await self.db.get_all_coupons()
        if not coupons:
            await interaction.response.send_message(embed=E.info("クーポンなし", "クーポンが登録されていません。"), ephemeral=True)
            return

        embed = discord.Embed(title="🎟️  クーポン一覧", color=Config.COLOR_PRIMARY)
        for c in coupons[:15]:
            label = f"{c['discount_value']}%OFF" if c["discount_type"] == "percent" else f"{c['discount_value']:,}pt OFF"
            status = "✅" if c["is_active"] else "❌"
            uses = f"{c['uses_count']}" + (f"/{c['max_uses']}" if c["max_uses"] != -1 else "")
            expire = c["expires_at"][:10] if c["expires_at"] else "無期限"
            embed.add_field(
                name=f"{status} `{c['code']}` — {label}",
                value=f"使用数: {uses} | 有効期限: {expire} | 最低: {c['min_purchase']:,}pt",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="coupon_disable", description="クーポンを無効化します（管理者専用）")
    @app_commands.describe(code="クーポンコード")
    async def coupon_disable(self, interaction: discord.Interaction, code: str):
        if not (interaction.user.guild_permissions.administrator or any(r.name == Config.ADMIN_ROLE for r in interaction.user.roles)):
            await interaction.response.send_message(embed=E.error("権限不足"), ephemeral=True)
            return
        coupon = await self.db.get_coupon(code)
        if not coupon:
            await interaction.response.send_message(embed=E.error("クーポンが見つかりません"), ephemeral=True)
            return
        await self.db.deactivate_coupon(coupon["id"])
        await interaction.response.send_message(
            embed=E.success("クーポンを無効化しました", f"コード `{coupon['code']}` を無効化しました。"),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Coupons(bot))
