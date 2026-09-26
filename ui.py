"""Discord UI (Embed / Persistent View / Modal)。

デザイン方針: 黒・ダーク・シンプル・高級感。スマートフォンでの視認性を最優先とし、
紫一色にはしない。利用者の個人情報・処理状況は原則 Ephemeral で表示する。

チャージパネルとランキングパネルは完全に独立したパネルとして実装する
(チャージパネルにランキング機能は入れない)。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Sequence

import discord

import config
import utils

if TYPE_CHECKING:  # 実行時の循環 import を避ける
    from database import GuildSettings

logger = logging.getLogger(config.LOGGER_BOT)

SEPARATOR = "───────────────────────"


# ---------------------------------------------------------------------------
# Embed ビルダー
# ---------------------------------------------------------------------------

def charge_panel_embed(settings: "GuildSettings", *, kyash_ready: bool) -> discord.Embed:
    """常設チャージパネルの Embed。"""
    if settings.emergency_stop:
        state = "🔴 緊急停止中"
    elif settings.maintenance:
        state = "🟠 メンテナンス中"
    elif not kyash_ready:
        state = "🟠 一時停止中"
    else:
        state = "🟢 通常受付中"

    embed = discord.Embed(
        title=settings.panel_title or "💰 チャージシステム",
        description=(
            f"{SEPARATOR}\n"
            + (
                settings.panel_description
                or "下のボタンからチャージを開始できます。\n"
                   "金額を入力したあと、Kyashの**送金リンク**を送信してください。"
            )
            + f"\n{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.BASE,
    )
    embed.add_field(name="現在のチャージ率", value=f"**{utils.fmt_rate(settings.charge_rate)}**", inline=True)
    embed.add_field(name="最低チャージ額", value=f"**{utils.fmt_yen(settings.minimum_charge)}**", inline=True)
    embed.add_field(name="最大チャージ額", value=f"**{utils.fmt_yen(settings.maximum_charge)}**", inline=True)
    embed.add_field(name="現在の状態", value=state, inline=False)
    embed.set_footer(text="サーバー内部残高システム / 現金化・出金には対応していません")
    return embed


def ranking_embed(
    guild: discord.Guild | None,
    entries: Sequence[tuple[int, int, str]],
    settings: "GuildSettings",
    *,
    updated_at: int,
    ranking_type: str = config.RankingType.BALANCE,
) -> discord.Embed:
    """ランキングパネルの Embed。

    Args:
        entries: ``(user_id, value, display)`` の並び (既に順位順)。
        ranking_type: 集計方式 (残高 / 週間 / 月間 / 招待)。
    """
    unit = "人" if ranking_type == config.RankingType.INVITE else ""
    lines: list[str] = [SEPARATOR]
    if not entries:
        lines.append("まだランキングデータがありません。")
    else:
        for index, (user_id, value, display) in enumerate(entries, start=1):
            medal = config.RANK_MEDALS.get(index, f"**{index}位**")
            lines.append(f"{medal}　{display}")
            lines.append(f"　　`{utils.fmt_int(value)}{unit}`")
    lines.append(SEPARATOR)

    embed = discord.Embed(
        title=config.RANKING_TYPE_TITLES.get(ranking_type, "🏆 RANKING"),
        description="\n".join(lines)[:4000],
        color=config.Color.RANKING,
    )
    basis = {
        config.RankingType.BALANCE: "現在の内部残高",
        config.RankingType.WEEKLY: "直近7日の獲得残高",
        config.RankingType.MONTHLY: "今月の獲得残高",
        config.RankingType.INVITE: "確定した招待数",
    }.get(ranking_type, "ランキング")
    embed.set_footer(
        text=f"{basis} TOP {settings.ranking_limit} / 最終更新 {utils.format_jst(updated_at)}"
    )
    if guild is not None and guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def ranking_disabled_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏆 SERVER BALANCE RANKING",
        description=f"{SEPARATOR}\nこのサーバーではランキング表示が無効になっています。\n{SEPARATOR}",
        color=config.Color.NEUTRAL,
    )
    return embed


def balance_embed(user: discord.abc.User, balance: int, summary: dict[str, int]) -> discord.Embed:
    """残高確認 (Ephemeral)。"""
    embed = discord.Embed(
        title="💳 あなたの残高",
        description=SEPARATOR,
        color=config.Color.INFO,
    )
    embed.add_field(name="現在残高", value=f"**{utils.fmt_int(balance)}**", inline=False)
    embed.add_field(name="累計チャージ", value=f"{utils.fmt_int(summary['count'])}回", inline=True)
    embed.add_field(name="累計送金額", value=utils.fmt_yen(summary["sent"]), inline=True)
    embed.add_field(name="累計獲得", value=utils.fmt_int(summary["credited"]), inline=True)
    embed.set_footer(text=f"{user.display_name} / このサーバー内の残高です")
    return embed


def history_embed(
    rows: Sequence[Any], *, page: int, total_pages: int, total: int
) -> discord.Embed:
    """チャージ履歴 (Ephemeral / ページング)。"""
    embed = discord.Embed(
        title="📜 チャージ履歴",
        description=f"{SEPARATOR}\n全 {utils.fmt_int(total)} 件" if total else f"{SEPARATOR}\n履歴はまだありません。",
        color=config.Color.NEUTRAL,
    )
    for row in rows:
        status = row["status"]
        emoji = config.STATUS_EMOJI.get(status, "⚪")
        label = config.STATUS_LABELS.get(status, status)
        received = row["received_amount"] if row["received_amount"] is not None else row["requested_amount"]
        credited = row["credited_amount"]
        value_lines = [
            f"送金額: {utils.fmt_yen(received)}",
            f"チャージ率: {utils.fmt_rate(row['charge_rate'])}",
            f"獲得残高: {utils.fmt_int(credited) if credited is not None else '-'}",
            f"状態: {emoji} {label}",
            f"ID: `{row['id']}`",
        ]
        embed.add_field(
            name=utils.format_jst(row["created_at"]),
            value="\n".join(value_lines),
            inline=False,
        )
    embed.set_footer(text=f"ページ {page}/{max(1, total_pages)}")
    return embed


def help_embed(settings: "GuildSettings") -> discord.Embed:
    """ヘルプ (Ephemeral)。"""
    embed = discord.Embed(
        title="❓ ヘルプ",
        description=(
            f"{SEPARATOR}\n"
            "このBotは、このサーバー内だけで使える**内部残高**を管理します。\n"
            "現金や外部サービスとの交換・出金には対応していません。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    embed.add_field(
        name="チャージのしかた",
        value=(
            "1. `💰 チャージ` を押す\n"
            "2. チャージしたい金額を入力する\n"
            "3. Kyashアプリで**同じ金額**の送金リンクを作る\n"
            "4. `🔗 送金リンクを送信` を押してリンクを貼る\n"
            "5. 自動で受け取り、残高が加算されます"
        ),
        inline=False,
    )
    embed.add_field(name="チャージ率", value=utils.fmt_rate(settings.charge_rate), inline=True)
    embed.add_field(name="最低 / 最大", value=f"{utils.fmt_yen(settings.minimum_charge)} / {utils.fmt_yen(settings.maximum_charge)}", inline=True)
    embed.add_field(name="1日の上限", value=utils.fmt_yen(settings.daily_limit), inline=True)
    embed.add_field(
        name="注意事項",
        value=(
            "・入力した金額と送金リンクの金額が**一致**していないと受け取れません\n"
            "・請求リンクではなく**送金リンク**を作成してください\n"
            "・リンクの入力期限は約15分です\n"
            "・処理の目安は通常10〜60秒程度です (混雑時は順番待ちになります)"
        ),
        inline=False,
    )
    embed.add_field(
        name="うまくいかないとき",
        value="結果はDMでお知らせします。解決しない場合はサーバーの管理者へお問い合わせください。",
        inline=False,
    )
    embed.set_footer(text="送金リンクは公開チャンネルに貼らないでください")
    return embed


def achievement_embed(
    *,
    user_mention: str,
    received_amount: int,
    charge_rate: Decimal | str,
    credited_amount: int | None,
    status: str,
    tx_id: str,
    timestamp: int,
    proxy: bool = False,
) -> discord.Embed:
    """実績チャンネルへ投稿する Embed。

    代理実績でも利用者から見た見た目は通常実績と同一にする
    (DB 上の ``source`` で区別する)。
    """
    if status == config.TxStatus.COMPLETED:
        state_text, color = "🟢 チャージ完了", config.Color.SUCCESS
    elif status in (config.TxStatus.FAILED, config.TxStatus.CANCELLED, config.TxStatus.EXPIRED):
        state_text, color = "🔴 チャージ失敗", config.Color.DANGER
    elif status == config.TxStatus.MANUAL_REVIEW:
        state_text, color = "🟠 確認中", config.Color.WARNING
    else:
        state_text, color = "🟡 チャージ処理中", config.Color.WARNING

    embed = discord.Embed(title="🏆 チャージ実績", description=SEPARATOR, color=color)
    embed.add_field(name="ユーザー", value=user_mention, inline=False)
    embed.add_field(name="送金額", value=utils.fmt_yen(received_amount), inline=True)
    embed.add_field(name="チャージ率", value=utils.fmt_rate(charge_rate), inline=True)
    embed.add_field(
        name="獲得残高",
        value=f"**{utils.fmt_int(credited_amount)}**" if credited_amount is not None else "-",
        inline=True,
    )
    embed.add_field(name="状態", value=state_text, inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.set_footer(text="内部残高システム")
    return embed


def dm_success_embed(
    *,
    guild_name: str,
    received_amount: int,
    charge_rate: Decimal | str,
    credited_amount: int,
    balance_after: int,
    tx_id: str,
    timestamp: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ チャージ完了",
        description=f"{SEPARATOR}\n**{guild_name}** でのチャージが完了しました。\n{SEPARATOR}",
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="送金額", value=utils.fmt_yen(received_amount), inline=True)
    embed.add_field(name="チャージ率", value=utils.fmt_rate(charge_rate), inline=True)
    embed.add_field(name="獲得残高", value=f"**{utils.fmt_int(credited_amount)}**", inline=True)
    embed.add_field(name="チャージ後残高", value=f"**{utils.fmt_int(balance_after)}**", inline=False)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    return embed


def dm_failure_embed(
    *, guild_name: str, tx_id: str, error_code: str, timestamp: int, requested_amount: int | None = None
) -> discord.Embed:
    """失敗通知 (利用者には安全な一般向けメッセージのみ表示する)。"""
    message = config.USER_ERROR_MESSAGES.get(error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR])
    embed = discord.Embed(
        title="⚠️ チャージを完了できませんでした",
        description=f"{SEPARATOR}\n**{guild_name}**\n{message}\n{SEPARATOR}",
        color=config.Color.DANGER,
    )
    if requested_amount is not None:
        embed.add_field(name="申請額", value=utils.fmt_yen(requested_amount), inline=True)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    embed.set_footer(text="送金リンクが未使用の場合は、Kyashアプリからキャンセルできます")
    return embed


def dm_review_embed(*, guild_name: str, tx_id: str, timestamp: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔎 処理結果を確認しています",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            "現在、受け取り結果の確認を行っています。\n"
            "確認が完了ししだい結果をお知らせしますので、そのままお待ちください。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="日時", value=utils.format_jst(timestamp), inline=True)
    return embed


def log_embed(
    title: str, description: str = "", *, color: int = config.Color.NEUTRAL,
    fields: Sequence[tuple[str, str, bool]] = (),
) -> discord.Embed:
    """ログチャンネル向け Embed (秘密情報は渡さないこと)。"""
    embed = discord.Embed(
        title=title, description=utils.truncate(description, 3800), color=color
    )
    for name, value, inline in fields:
        embed.add_field(name=utils.truncate(name, 250), value=utils.truncate(str(value), 1000), inline=inline)
    embed.timestamp = discord.utils.utcnow()
    return embed


def error_embed(error_code: str, *, admin_detail: str | None = None) -> discord.Embed:
    """利用者向けエラー表示 (内部詳細は含めない)。"""
    embed = discord.Embed(
        title="⚠️ 実行できませんでした",
        description=config.USER_ERROR_MESSAGES.get(
            error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR]
        ),
        color=config.Color.DANGER,
    )
    if admin_detail:
        embed.add_field(name="詳細 (管理者向け)", value=utils.truncate(admin_detail, 900), inline=False)
    return embed


def info_embed(title: str, description: str, *, color: int = config.Color.INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=config.Color.SUCCESS)


def shop_panel_embed(settings: "GuildSettings", items: Sequence[Any]) -> discord.Embed:
    """常設ショップパネルの Embed。"""
    embed = discord.Embed(
        title="🛒 ロールショップ",
        description=(
            f"{SEPARATOR}\n"
            "内部残高でロールを購入できます。\n"
            "下のボタンから商品を選んでください。\n"
            f"{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.ACCENT,
    )
    if not settings.shop_enabled:
        embed.add_field(name="状態", value="⚫ 現在ショップは停止中です", inline=False)
        return embed
    if not items:
        embed.add_field(name="商品", value="現在購入できる商品はありません。", inline=False)
        return embed
    for item in list(items)[:10]:
        duration = int(item["duration_days"])
        stock = int(item["stock"])
        details = [f"価格: **{utils.fmt_int(int(item['price']))}**"]
        details.append("期間: " + (f"{duration}日" if duration > 0 else "無期限"))
        if stock >= 0:
            details.append(f"在庫: {stock}")
        if int(item["purchase_limit"]) > 0:
            details.append(f"購入上限: {item['purchase_limit']}回")
        if item["description"]:
            details.append(utils.truncate(str(item["description"]), 200))
        embed.add_field(
            name=f"<@&{int(item['role_id'])}> {item['name']}",
            value="\n".join(details),
            inline=False,
        )
    embed.set_footer(text="購入すると内部残高が消費され、ロールが付与されます")
    return embed


def purchase_success_embed(
    *, item_name: str, role_id: int, price: int, balance_after: int,
    expires_at: int | None, purchase_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ 購入が完了しました",
        description=f"{SEPARATOR}\n<@&{role_id}> を付与しました。\n{SEPARATOR}",
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="商品", value=item_name, inline=True)
    embed.add_field(name="支払い", value=utils.fmt_int(price), inline=True)
    embed.add_field(name="残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(
        name="有効期限",
        value=utils.format_jst(expires_at) if expires_at else "無期限",
        inline=True,
    )
    embed.add_field(name="購入ID", value=f"`{purchase_id}`", inline=True)
    return embed


def my_items_embed(rows: Sequence[Any]) -> discord.Embed:
    """所持ロール (購入履歴) の表示。"""
    embed = discord.Embed(
        title="📦 購入履歴",
        description=SEPARATOR if rows else f"{SEPARATOR}\n購入履歴はありません。",
        color=config.Color.INFO,
    )
    for row in list(rows)[:10]:
        embed.add_field(
            name=f"{config.PURCHASE_STATUS_LABELS.get(str(row['status']), str(row['status']))} "
                 f"{row['item_name']}",
            value=(
                f"ロール: <@&{int(row['role_id'])}>\n"
                f"価格: {utils.fmt_int(int(row['price']))}\n"
                f"購入: {utils.format_jst(int(row['created_at']))}\n"
                + (f"期限: {utils.format_jst(row['expires_at'])}\n" if row["expires_at"] else "")
                + f"購入ID: `{row['id']}`"
            ),
            inline=False,
        )
    return embed


def invite_panel_embed(settings: "GuildSettings", campaign: Any | None) -> discord.Embed:
    """招待キャンペーンのパネル。"""
    if campaign is None:
        return discord.Embed(
            title="🤝 招待キャンペーン",
            description=f"{SEPARATOR}\n現在開催中のキャンペーンはありません。\n{SEPARATOR}",
            color=config.Color.NEUTRAL,
        )
    conditions: list[str] = []
    if campaign["require_charge"]:
        conditions.append("招待した人が**チャージを1回完了**したら確定")
    if int(campaign["require_days"] or 0) > 0:
        conditions.append(f"参加から**{campaign['require_days']}日**の滞在で確定")
    if not conditions:
        conditions.append("参加が確認できた時点で確定")
    embed = discord.Embed(
        title=f"🤝 {campaign['name']}",
        description=(
            f"{SEPARATOR}\n"
            "あなた専用の招待リンクで友達を招待すると、内部残高がもらえます。\n"
            f"{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.ACCENT,
    )
    embed.add_field(
        name="報酬",
        value=(
            f"招待した人: **{utils.fmt_int(int(campaign['inviter_reward']))}**\n"
            f"招待された人: **{utils.fmt_int(int(campaign['invited_reward']))}**"
        ),
        inline=True,
    )
    embed.add_field(name="確定条件", value="\n".join(f"・{c}" for c in conditions), inline=True)
    limits = [
        f"アカウント作成から{campaign['min_account_age_days']}日以上",
        f"1日あたり{campaign['daily_limit']}人まで" if int(campaign["daily_limit"] or 0) > 0 else "1日の上限なし",
        f"累計{campaign['total_limit']}人まで" if int(campaign["total_limit"] or 0) > 0 else "累計上限なし",
    ]
    embed.add_field(name="条件・上限", value="\n".join(f"・{x}" for x in limits), inline=False)
    embed.add_field(
        name="注意事項",
        value=(
            "・自分自身の招待、再入場、Botアカウントは無効です\n"
            "・一度サーバーに参加したことがある人は対象外です\n"
            "・不自然な招待は保留され、管理者が確認します"
        ),
        inline=False,
    )
    if campaign["ends_at"]:
        embed.set_footer(text=f"終了予定 {utils.format_jst(int(campaign['ends_at']))}")
    return embed


def invite_link_embed(*, url: str, code: str, summary: dict[str, int], created: bool) -> discord.Embed:
    embed = discord.Embed(
        title="🔗 あなたの招待リンク",
        description=(
            f"{SEPARATOR}\n{url}\n{SEPARATOR}\n"
            + ("新しく発行しました。" if created else "既に発行済みのリンクです。")
            + "\nこのリンク経由の参加だけが報酬の対象になります。"
        ),
        color=config.Color.ACCENT,
    )
    embed.add_field(name="確定", value=f"{summary['confirmed']}人", inline=True)
    embed.add_field(name="保留中", value=f"{summary['pending']}人", inline=True)
    embed.add_field(name="要確認", value=f"{summary['hold']}人", inline=True)
    embed.add_field(name="無効", value=f"{summary['rejected']}人", inline=True)
    embed.add_field(name="獲得報酬", value=utils.fmt_int(summary["reward"]), inline=True)
    embed.set_footer(text=f"招待コード: {code}")
    return embed


def invite_status_embed(
    *, summary: dict[str, int], rank: int | None, total: int, records: Sequence[Any]
) -> discord.Embed:
    embed = discord.Embed(
        title="📊 あなたの招待状況",
        description=SEPARATOR,
        color=config.Color.INFO,
    )
    embed.add_field(name="確定した招待", value=f"**{summary['confirmed']}人**", inline=True)
    embed.add_field(name="獲得報酬", value=f"**{utils.fmt_int(summary['reward'])}**", inline=True)
    embed.add_field(
        name="順位",
        value=f"{rank}位 / {total}人" if rank else "対象外",
        inline=True,
    )
    embed.add_field(name="保留中", value=f"{summary['pending']}人", inline=True)
    embed.add_field(name="要確認", value=f"{summary['hold']}人", inline=True)
    embed.add_field(name="無効", value=f"{summary['rejected']}人", inline=True)
    if records:
        lines = [
            f"{config.INVITE_STATUS_LABELS.get(str(r['status']), str(r['status']))} "
            f"<@{int(r['invited_id'])}> ({utils.format_jst(int(r['joined_at']))})"
            for r in list(records)[:10]
        ]
        embed.add_field(name="最近の招待", value="\n".join(lines), inline=False)
    embed.set_footer(text="保留中の招待は条件を満たすと自動で確定します")
    return embed


def invite_reward_embed(
    *, guild_name: str, label: str, amount: int, balance_after: int, campaign_name: str
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎉 {label}を獲得しました",
        description=f"{SEPARATOR}\n**{guild_name}**\n{campaign_name}\n{SEPARATOR}",
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="獲得", value=f"**{utils.fmt_int(amount)}**", inline=True)
    embed.add_field(name="現在残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    return embed


def refund_notice_embed(
    *, guild_name: str, tx_id: str, amount: int, balance_after: int, reason: str
) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ チャージが取り消されました",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            "管理者により、以下のチャージが取り消されました。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.DANGER,
    )
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(name="回収された残高", value=utils.fmt_int(amount), inline=True)
    embed.add_field(name="現在残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(name="理由", value=utils.truncate(reason, 500), inline=False)
    embed.set_footer(text="心当たりがない場合はサーバーの管理者へお問い合わせください")
    return embed


def daily_summary_embed(
    *, guild_name: str, start: int, end: int, summary: dict[str, Any],
    distribution: dict[str, Any],
) -> discord.Embed:
    """日次サマリ (ログ/サマリチャンネルへ自動投稿)。"""
    success_rate = (summary["success"] / summary["total"] * 100) if summary["total"] else 0.0
    embed = discord.Embed(
        title="📅 デイリーサマリ",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"{utils.format_jst(start)} 〜 {utils.format_jst(end)}\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    embed.add_field(
        name="チャージ",
        value=(
            f"件数: **{utils.fmt_int(summary['total'])}**\n"
            f"成功: {utils.fmt_int(summary['success'])} / 失敗: {utils.fmt_int(summary['failed'])}\n"
            f"成功率: {success_rate:.1f}%"
        ),
        inline=True,
    )
    embed.add_field(
        name="金額",
        value=(
            f"送金: **{utils.fmt_yen(summary['received'])}**\n"
            f"付与: **{utils.fmt_int(summary['credited'])}**\n"
            f"利用者: {utils.fmt_int(summary['users'])}人"
        ),
        inline=True,
    )
    embed.add_field(
        name="消費 / 招待",
        value=(
            f"消費: {utils.fmt_int(summary['spend_total'])} ({summary['spend_count']}件)\n"
            f"招待確定: {summary['invites_confirmed']}人\n"
            f"要確認: {summary['review']}件"
        ),
        inline=True,
    )
    if summary["errors"]:
        embed.add_field(
            name="失敗の内訳",
            value="\n".join(f"`{code}`: {count}件" for code, count in summary["errors"]),
            inline=False,
        )
    if summary["top"]:
        embed.add_field(
            name="上位チャージャー",
            value="\n".join(
                f"{i}. <@{uid}> {utils.fmt_int(total)}"
                for i, (uid, total) in enumerate(summary["top"], start=1)
            ),
            inline=False,
        )
    embed.add_field(
        name="残高の状況",
        value=(
            f"残高合計: {utils.fmt_int(distribution['total'])}\n"
            f"保有者: {distribution['count']}人 (残高0: {distribution['zero']}人)\n"
            f"中位値: {utils.fmt_int(distribution['median'])} / "
            f"上位10%占有: {distribution['top10_share']:.1f}%"
        ),
        inline=False,
    )
    return embed


def admin_panel_embed(
    *,
    guild_name: str,
    settings: "GuildSettings",
    kyash: dict[str, Any],
    queue: dict[str, int],
    stats: dict[str, Any],
    metrics: dict[str, Any],
    review_count: int,
    updated_at: int,
) -> discord.Embed:
    """管理者ダッシュボード (常設・自動更新)。"""
    problems: list[str] = []
    if settings.emergency_stop:
        problems.append("🔴 緊急停止中")
    if settings.maintenance:
        problems.append("🟠 メンテナンス中")
    if kyash.get("status") != config.KyashAccountStatus.ACTIVE:
        problems.append(f"🟠 Kyash: {kyash.get('status')}")
    days_left = kyash.get("token_days_left")
    if days_left is not None and days_left <= config.KYASH_TOKEN_WARN_DAYS:
        problems.append(f"🟠 トークン残り {days_left:.1f} 日")
    if review_count:
        problems.append(f"🟠 手動確認 {review_count} 件")
    headroom = kyash.get("wallet_headroom")
    if headroom is not None and headroom <= 0:
        problems.append("🔴 受取残高しきい値に到達")

    embed = discord.Embed(
        title="🛠 管理ダッシュボード",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + ("\n".join(problems) if problems else "🟢 異常はありません")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.DANGER if problems else config.Color.SUCCESS,
    )
    embed.add_field(
        name="キュー",
        value=(
            f"待機: **{queue.get(config.TxStatus.QUEUED, 0)}**\n"
            f"処理中: {queue.get(config.TxStatus.PROCESSING, 0)}\n"
            f"要確認: {review_count}"
        ),
        inline=True,
    )
    embed.add_field(
        name="本日",
        value=(
            f"件数: **{utils.fmt_int(stats.get('today_count', 0))}**\n"
            f"送金: {utils.fmt_yen(stats.get('today_sent', 0))}\n"
            f"付与: {utils.fmt_int(stats.get('today_credited', 0))}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Kyash",
        value=(
            f"{config.KYASH_STATUS_LABELS.get(str(kyash.get('status')), str(kyash.get('status')))}\n"
            + (f"トークン残り: {days_left:.1f}日\n" if days_left is not None else "")
            + (f"しきい値まで: {utils.fmt_yen(headroom)}" if headroom is not None else "しきい値: 未設定")
        ),
        inline=True,
    )
    embed.add_field(
        name="設定",
        value=(
            f"チャージ率: {utils.fmt_rate(settings.charge_rate)}\n"
            f"上限: {utils.fmt_yen(settings.maximum_charge)} / 日次 {utils.fmt_yen(settings.daily_limit)}\n"
            f"残高上限: {utils.fmt_int(settings.max_balance) if settings.max_balance else '無制限'}"
        ),
        inline=True,
    )
    embed.add_field(
        name="累計",
        value=(
            f"成功: {utils.fmt_int(stats.get('success', 0))} / "
            f"失敗: {utils.fmt_int(stats.get('failed', 0))}\n"
            f"付与総額: {utils.fmt_int(stats.get('credited', 0))}\n"
            f"利用者: {utils.fmt_int(stats.get('users', 0))}人"
        ),
        inline=True,
    )
    embed.add_field(
        name="メトリクス",
        value=(
            f"平均受取: {metrics.get('receive_avg_seconds', 0)}秒\n"
            f"購入: {metrics.get('purchases', 0)} / 返金: {metrics.get('purchase_refunds', 0)}\n"
            f"招待確定: {metrics.get('invites_confirmed', 0)} / "
            f"保留: {metrics.get('invites_hold', 0)}"
        ),
        inline=True,
    )
    embed.set_footer(text=f"最終更新 {utils.format_jst(updated_at, with_seconds=True)}")
    return embed


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------

async def safe_respond(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """応答済みかどうかに応じて response / followup を使い分ける。"""
    kwargs: dict[str, Any] = {}
    if embed is not None:
        kwargs["embed"] = embed
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
    except discord.HTTPException as exc:
        logger.warning("Interaction への応答に失敗しました: %s", utils.safe_error_text(exc))


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------

class AmountModal(discord.ui.Modal, title="チャージ金額の入力"):
    """チャージ金額を入力する Modal。"""

    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="チャージ金額 (円)",
        placeholder="例: 1000",
        required=True,
        min_length=1,
        max_length=config.AMOUNT_INPUT_MAX_LEN,
    )

    def __init__(self, settings: "GuildSettings") -> None:
        super().__init__(timeout=300)
        self.amount.placeholder = (
            f"{settings.minimum_charge}〜{settings.maximum_charge} の範囲で入力"
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.client.handle_amount_submit(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("金額入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class LinkModal(discord.ui.Modal, title="送金リンクの送信"):
    """Kyash 送金リンクを入力する Modal (公開チャンネルへ平文を出さない)。"""

    link: discord.ui.TextInput = discord.ui.TextInput(
        label="Kyash 送金リンク",
        placeholder="https://kyash.me/payments/xxxxxxxx",
        required=True,
        min_length=3,
        max_length=300,
    )

    def __init__(self, tx_id: str) -> None:
        super().__init__(timeout=config.LINK_WAIT_SECONDS)
        self._tx_id = tx_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        raw = str(self.link.value)
        # 入力値はできるだけ早く破棄する
        self.link.default = None
        await interaction.client.handle_link_submit(interaction, self._tx_id, raw)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("リンク入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class KyashLoginModal(discord.ui.Modal, title="受取用Kyashアカウントのログイン"):
    """管理者のみが使用する Kyash ログイン Modal (Ephemeral 応答)。"""

    email: discord.ui.TextInput = discord.ui.TextInput(
        label="メールアドレス", placeholder="Kyash に登録したメールアドレス", required=True, max_length=200
    )
    password: discord.ui.TextInput = discord.ui.TextInput(
        label="パスワード", placeholder="保存されません", required=True, max_length=200
    )

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        email = str(self.email.value).strip()
        password = str(self.password.value)
        self.password.default = None
        await interaction.client.handle_kyash_login(interaction, email, password)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("Kyashログの入力でエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class KyashOtpModal(discord.ui.Modal, title="SMS認証コードの入力"):
    """OTP 入力 Modal。OTP はログにも DB にも保存しない。"""

    otp: discord.ui.TextInput = discord.ui.TextInput(
        label="認証コード (6桁)", placeholder="SMSに届いた番号", required=True, min_length=4, max_length=10
    )

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        code = str(self.otp.value).strip()
        self.otp.default = None
        await interaction.client.handle_kyash_otp(interaction, code)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("OTP入力でエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class ChargePanelView(discord.ui.View):
    """常設チャージパネル (Persistent View)。

    ランキング機能はこのパネルには含めない (ランキングは専用パネル)。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="チャージ", emoji="💰", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.CHARGE_START, row=0,
    )
    async def charge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_charge_button(interaction)

    @discord.ui.button(
        label="残高", emoji="💳", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_BALANCE, row=0,
    )
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_balance_button(interaction)

    @discord.ui.button(
        label="履歴", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HISTORY, row=1,
    )
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_history_button(interaction)

    @discord.ui.button(
        label="ヘルプ", emoji="❓", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HELP, row=1,
    )
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_help_button(interaction)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_REFRESH, row=1,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_panel_refresh_button(interaction)


class RankingPanelView(discord.ui.View):
    """ランキングパネル (Persistent View)。チャージパネルとは完全に独立。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_REFRESH, row=0,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_ranking_refresh_button(interaction)

    @discord.ui.button(
        label="自分の順位", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_MYRANK, row=0,
    )
    async def my_rank(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_my_rank_button(interaction)


class LinkSubmitView(discord.ui.View):
    """金額確定後に送金リンクを受け付ける Ephemeral View。"""

    def __init__(self, tx_id: str, *, owner_id: int, timeout: float) -> None:
        super().__init__(timeout=max(30.0, timeout))
        self._tx_id = tx_id
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="送金リンクを送信", emoji="🔗", style=discord.ButtonStyle.primary)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LinkModal(self._tx_id))

    @discord.ui.button(label="キャンセル", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_charge_cancel(interaction, self._tx_id)
        self.stop()


class HistoryView(discord.ui.View):
    """履歴のページング (Ephemeral)。"""

    PAGE_SIZE = 5

    def __init__(self, *, owner_id: int, guild_id: int, page: int = 1) -> None:
        super().__init__(timeout=300)
        self._owner_id = owner_id
        self.guild_id = guild_id
        self.page = page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(1, self.page - 1)
        await interaction.client.render_history(interaction, self, edit=True)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        await interaction.client.render_history(interaction, self, edit=True)


class ConfirmView(discord.ui.View):
    """危険操作の確認 (2段階確認にも対応)。"""

    def __init__(
        self,
        *,
        owner_id: int,
        confirm_label: str = "実行する",
        danger: bool = True,
        stages: int = 1,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.value: bool | None = None
        self._owner_id = owner_id
        self._stages = max(1, stages)
        self._stage = 0
        self._confirm_label = confirm_label
        self.confirm.label = confirm_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._stage += 1
        if self._stage < self._stages:
            remaining = self._stages - self._stage
            button.label = f"本当に実行する (残り{remaining}回)"
            await interaction.response.edit_message(
                embed=info_embed(
                    "⚠️ 最終確認",
                    "この操作は取り消せません。実行する場合はもう一度ボタンを押してください。",
                    color=config.Color.DANGER,
                ),
                view=self,
            )
            return
        self.value = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=info_embed("⏳ 実行中", "処理を実行しています…", color=config.Color.WARNING),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=info_embed("キャンセルしました", "操作は実行されていません。", color=config.Color.NEUTRAL),
            view=self,
        )
        self.stop()


class KyashLoginStartView(discord.ui.View):
    """OTP 入力を促す Ephemeral View。"""

    def __init__(self, *, owner_id: int) -> None:
        super().__init__(timeout=config.KYASH_LOGIN_PENDING_TTL)
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="認証コードを入力", emoji="🔑", style=discord.ButtonStyle.primary)
    async def enter_otp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(KyashOtpModal())


class ShopPanelView(discord.ui.View):
    """常設ショップパネル (Persistent View)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ショップを開く", emoji="🛒", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.SHOP_OPEN, row=0,
    )
    async def open_shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_shop_open_button(interaction)

    @discord.ui.button(
        label="購入履歴", emoji="📦", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.SHOP_MYITEMS, row=0,
    )
    async def my_items(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_shop_myitems_button(interaction)


class ShopSelectView(discord.ui.View):
    """商品選択 (Ephemeral)。在庫や価格が変わるため都度生成する。"""

    def __init__(self, items: Sequence[Any], *, owner_id: int) -> None:
        super().__init__(timeout=180)
        self._owner_id = owner_id
        options: list[discord.SelectOption] = []
        for item in list(items)[:25]:
            duration = int(item["duration_days"])
            stock = int(item["stock"])
            description = (
                f"{utils.fmt_int(int(item['price']))} / "
                + (f"{duration}日" if duration > 0 else "無期限")
                + (f" / 在庫{stock}" if stock >= 0 else "")
            )
            options.append(
                discord.SelectOption(
                    label=utils.truncate(str(item["name"]), 90),
                    value=str(int(item["id"])),
                    description=utils.truncate(description, 90),
                )
            )
        self.select.options = options or [
            discord.SelectOption(label="購入できる商品がありません", value="none")
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.select(placeholder="購入する商品を選択してください", min_values=1, max_values=1)
    async def select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        value = select.values[0]
        if value == "none":
            await safe_respond(
                interaction, embed=info_embed("商品がありません", "現在購入できる商品はありません。")
            )
            return
        await interaction.client.on_shop_select(interaction, int(value))


class InvitePanelView(discord.ui.View):
    """招待キャンペーンのパネル (Persistent View)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="招待リンクを取得", emoji="🔗", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.INVITE_GET, row=0,
    )
    async def get_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_invite_get_button(interaction)

    @discord.ui.button(
        label="自分の招待状況", emoji="📊", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.INVITE_STATUS, row=0,
    )
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_invite_status_button(interaction)

    @discord.ui.button(
        label="招待ランキング", emoji="🏆", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.INVITE_RANK, row=0,
    )
    async def ranking(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_invite_rank_button(interaction)


class AdminPanelView(discord.ui.View):
    """管理者ダッシュボード (Persistent View)。押下時に毎回権限を確認する。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.primary,
        custom_id=config.CustomID.ADMIN_REFRESH, row=0,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_admin_refresh_button(interaction)

    @discord.ui.button(
        label="メンテ切替", emoji="🛠", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.ADMIN_MAINTENANCE, row=0,
    )
    async def maintenance(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.client.on_admin_maintenance_button(interaction)

    @discord.ui.button(
        label="キュー", emoji="🗃", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.ADMIN_QUEUE, row=1,
    )
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_admin_queue_button(interaction)

    @discord.ui.button(
        label="要確認", emoji="🟠", style=discord.ButtonStyle.danger,
        custom_id=config.CustomID.ADMIN_REVIEW, row=1,
    )
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.on_admin_review_button(interaction)
