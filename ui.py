"""Discord UI (Embed / Persistent View / Modal)。

デザイン方針: 黒・ダーク・シンプル・高級感。スマートフォンでの視認性を最優先とし、
紫一色にはしない。利用者の個人情報・処理状況は原則 Ephemeral で表示する。

チャージパネルとランキングパネルは完全に独立したパネルとして実装する
(チャージパネルにランキング機能は入れない)。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Sequence, cast

import discord

import config
import utils

if TYPE_CHECKING:  # 実行時の循環 import を避ける
    from database import GuildSettings
    from main import ChargeBot

logger = logging.getLogger(config.LOGGER_BOT)

SEPARATOR = "───────────────────────"


def bot_of(interaction: discord.Interaction) -> "ChargeBot":
    """Interaction から Bot 本体を型付きで取得する。

    ``interaction.client`` は discord.py 上では ``Client`` 型のため、
    そのまま呼ぶとハンドラ名や引数の誤りを静的解析で検出できない。
    このヘルパ経由に統一することで mypy が呼び出しを検査できる。
    """
    return cast("ChargeBot", interaction.client)


# ---------------------------------------------------------------------------
# Embed ビルダー
# ---------------------------------------------------------------------------

def charge_panel_embed(
    settings: "GuildSettings",
    *,
    kyash_ready: bool,
    providers: Sequence[dict[str, Any]] | None = None,
) -> discord.Embed:
    """常設チャージパネルの Embed。

    「何ができるか」「どう操作するか」「いくら増えるか」が
    パネルを見るだけで分かるようにする。

    Args:
        providers: 各チャージ方式の利用可否 (``provider_availability`` の戻り値)。
            省略した場合は Kyash のみの表示になる。
    """
    usable = [p for p in (providers or []) if p["available"]]
    if settings.emergency_stop:
        state = "🔴 **緊急停止中** — 現在チャージを受け付けていません"
    elif settings.maintenance:
        state = "🟠 **メンテナンス中** — 残高・履歴の確認はできます"
    elif providers is not None and not usable:
        state = "🟠 **一時停止中** — 復旧までお待ちください"
    elif providers is None and not kyash_ready:
        state = "🟠 **一時停止中** — 復旧までお待ちください"
    else:
        state = "🟢 **受付中** — いつでもチャージできます"

    # 具体例で「いくら増えるか」を示す
    example_base = max(settings.minimum_charge, 1000)
    if example_base > settings.maximum_charge:
        example_base = settings.maximum_charge
    example_credit = utils.calc_credited_amount(example_base, settings.charge_rate)

    multi = len(usable) > 1
    embed = discord.Embed(
        title=settings.panel_title or "💰 チャージシステム",
        description=(
            f"{SEPARATOR}\n"
            + (
                settings.panel_description
                or ("送金すると、このサーバーで使える**内部残高**が増えます。"
                    if multi else
                    "Kyash で送金すると、このサーバーで使える**内部残高**が増えます。")
            )
            + f"\n{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.BASE,
    )
    if providers is not None and (usable or providers):
        lines = []
        for entry in providers:
            provider = str(entry["provider"])
            emoji = config.PROVIDER_EMOJI.get(provider, "💠")
            name = config.PROVIDER_LABELS.get(provider, provider)
            if entry["available"]:
                kind = ("自動反映" if provider == config.ChargeProvider.KYASH
                        else "管理者の承認制")
                lines.append(
                    f"{emoji} **{name}** — {utils.fmt_rate(entry['rate'])} / {kind}"
                )
            else:
                lines.append(f"{emoji} ~~{name}~~ — 🚫 {entry['reason']}")
        embed.add_field(name="💠 使えるチャージ方法", value="\n".join(lines), inline=False)
    else:
        embed.add_field(
            name="📈 チャージ率",
            value=f"**{utils.fmt_rate(settings.charge_rate)}**\n"
                  f"例) {utils.fmt_yen(example_base)} → **{utils.fmt_int(example_credit)}**",
            inline=True,
        )
    embed.add_field(
        name="💵 1回の金額",
        value=f"**{utils.fmt_yen(settings.minimum_charge)}** 〜\n"
              f"**{utils.fmt_yen(settings.maximum_charge)}**",
        inline=True,
    )
    embed.add_field(
        name="📅 1日の上限",
        value=f"**{utils.fmt_yen(settings.daily_limit)}**" if settings.daily_limit
        else "**無制限**",
        inline=True,
    )
    if multi:
        embed.add_field(
            name="🪜 チャージの手順",
            value=(
                "**1.** 下の `💰 チャージ` を押して**方法を選ぶ**\n"
                "**2.** 金額を入力する\n"
                "**3.** 表示された宛先へ送金する\n"
                "**4.** 画面の案内どおりに申請する\n"
                "　→ Kyash は自動、PayPay / LTC は管理者の承認後に反映されます"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="🪜 チャージの手順",
            value=(
                "**1.** 下の `💰 チャージ` を押して**金額を入力**\n"
                "**2.** Kyash アプリで**同じ金額**の送金リンクを作成\n"
                "**3.** `🔗 送金リンクを送信` を押して URL を貼る\n"
                "**4.** 自動で受け取り → 残高が増え、DM が届きます"
            ),
            inline=False,
        )
    embed.add_field(name="状態", value=state, inline=False)
    embed.set_footer(
        text="送金リンクや取引IDは公開チャンネルに貼らないでください / "
             "内部残高は現金化・出金できません"
    )
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
    embed.add_field(
        name="\u200b",
        value="🔄 最新の順位に更新　📜 自分の順位を確認 (自分にだけ表示されます)",
        inline=False,
    )
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


def balance_embed(
    user: discord.abc.User,
    balance: int,
    summary: dict[str, int],
    *,
    rank: int | None = None,
    rank_total: int = 0,
    shop_available: bool = False,
    spent: int = 0,
) -> discord.Embed:
    """残高確認 (Ephemeral)。"""
    embed = discord.Embed(
        title="💳 あなたの残高",
        description=f"{SEPARATOR}\n現在の残高は **{utils.fmt_int(balance)}** です。\n{SEPARATOR}",
        color=config.Color.INFO,
    )
    embed.add_field(
        name="📊 これまでの実績",
        value=(
            f"チャージ回数: **{utils.fmt_int(summary['count'])}回**\n"
            f"送金した合計: **{utils.fmt_yen(summary['sent'])}**\n"
            f"獲得した残高: **{utils.fmt_int(summary['credited'])}**"
            + (f"\n使った残高: **{utils.fmt_int(spent)}**" if spent else "")
        ),
        inline=True,
    )
    embed.add_field(
        name="🏆 順位",
        value=(
            f"**{rank}位** / {utils.fmt_int(rank_total)}人" if rank
            else "まだ順位はありません\n(残高が増えるとランクインします)"
        ),
        inline=True,
    )
    uses = ["`💰 チャージ` で残高を増やす"]
    if shop_available:
        uses.append("ショップパネルでロールと交換する")
    uses.append("`📜 履歴` で明細を確認する")
    embed.add_field(
        name="▶ できること",
        value="\n".join(f"・{u}" for u in uses),
        inline=False,
    )
    embed.set_footer(text=f"{user.display_name} / この残高はこのサーバー内でのみ使えます")
    return embed


def history_embed(
    rows: Sequence[Any],
    *,
    page: int,
    total_pages: int,
    total: int,
    open_requests: Sequence[Any] = (),
) -> discord.Embed:
    """チャージ履歴 (Ephemeral / ページング)。

    日時は Discord のタイムスタンプ表記にして、閲覧者のローカル時刻で表示する。
    """
    embed = discord.Embed(
        title="📜 チャージ履歴",
        description=(
            f"{SEPARATOR}\n全 **{utils.fmt_int(total)}** 件のうち "
            f"{len(rows)} 件を表示しています。\n{SEPARATOR}"
            if total else
            f"{SEPARATOR}\nまだ履歴がありません。\n"
            "`💰 チャージ` から最初のチャージをしてみましょう。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.NEUTRAL,
    )
    if open_requests:
        # 承認待ち・送金待ちの申請はまだ取引になっていないため、先に見せる
        lines: list[str] = []
        for req in list(open_requests)[:5]:
            provider = str(req["provider"])
            status = str(req["status"])
            line = (
                f"`#{int(req['id'])}` "
                f"{config.REQUEST_STATUS_LABELS.get(status, status)} "
                f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
                f"{config.PROVIDER_LABELS.get(provider, provider)} "
                f"{utils.fmt_yen(int(req['requested_amount']))} "
                f"→ 付与予定 **{utils.fmt_int(int(req['estimated_credit']))}**"
            )
            if status == config.RequestStatus.QUOTED:
                line += f" (送金期限 {utils.discord_ts(req['quote_expires_at'], 'R')})"
            lines.append(line)
        embed.add_field(
            name="📨 進行中の申請",
            value="\n".join(lines)
            + "\n※ 承認されると下の履歴に並びます。",
            inline=False,
        )
    for row in rows:
        status = str(row["status"])
        emoji = config.STATUS_EMOJI.get(status, "⚪")
        label = config.STATUS_LABELS.get(status, status)
        received = (
            row["received_amount"] if row["received_amount"] is not None
            else row["requested_amount"]
        )
        credited = row["credited_amount"]
        lines = [
            f"送金 **{utils.fmt_yen(received)}** × {utils.fmt_rate(row['charge_rate'])} "
            f"→ 獲得 **{utils.fmt_int(credited) if credited is not None else '-'}**",
            f"{utils.discord_ts(row['created_at'])} ({utils.discord_ts(row['created_at'], 'R')})",
            f"取引ID `{row['id']}`",
        ]
        if row["refunded_at"]:
            lines.append(f"⚠️ 取消済み ({utils.discord_ts(row['refunded_at'], 'R')})")
        embed.add_field(name=f"{emoji} {label}", value="\n".join(lines), inline=False)
    if rows:
        embed.add_field(
            name="状態の意味",
            value=(
                "🟢 完了 = 残高に反映済み / ⌛ リンク待ち = 送金リンクの送信待ち\n"
                "🟡 受取待ち・処理中 = 自動処理中 / 🟠 確認中 = 管理者が確認中\n"
                "🔴 失敗・⚫ 期限切れ = 残高は増えていません"
            ),
            inline=False,
        )
    embed.set_footer(text=f"ページ {page}/{max(1, total_pages)} / ◀️ ▶️ で移動できます")
    return embed


def help_embed(
    settings: "GuildSettings",
    *,
    shop_available: bool = False,
    campaign_name: str | None = None,
    example_amount: int | None = None,
    providers: Sequence[dict[str, Any]] | None = None,
) -> discord.Embed:
    """ヘルプ (Ephemeral)。何ができて、どう操作するかを順番に示す。"""
    base = example_amount or max(settings.minimum_charge, 1000)
    if base > settings.maximum_charge:
        base = settings.maximum_charge
    credit = utils.calc_credited_amount(base, settings.charge_rate)

    embed = discord.Embed(
        title="❓ ヘルプ",
        description=(
            f"{SEPARATOR}\n"
            "このサーバーで使える**内部残高**のしくみです。\n"
            "送金すると、チャージ率を掛けた残高が受け取れます。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    usable = [p for p in (providers or []) if p["available"]]
    manual_usable = [
        p for p in usable if p["provider"] in config.MANUAL_PROVIDERS
    ]
    if len(usable) > 1:
        embed.add_field(
            name="① 使えるチャージ方法",
            value="\n".join(
                f"{config.PROVIDER_EMOJI.get(str(p['provider']), '💠')} "
                f"**{config.PROVIDER_LABELS.get(str(p['provider']), p['provider'])}** "
                f"({utils.fmt_rate(p['rate'])}) — "
                f"{config.PROVIDER_DESCRIPTIONS.get(str(p['provider']), '')}"
                for p in usable
            ),
            inline=False,
        )
        embed.add_field(
            name="② チャージのしかた",
            value=(
                "**1.** `💰 チャージ` を押して**方法を選ぶ**\n"
                "**2.** チャージしたい金額を入力する (LTC も**円**で入力します)\n"
                "**3.** 表示された宛先へ、表示された金額をそのまま送る\n"
                "**4.** Kyash は送金リンクを貼るだけ。PayPay / LTC は\n"
                "　　`✅ 送金しました` から取引ID (txid) を入力して申請\n"
                "**5.** Kyash は自動、PayPay / LTC は管理者の承認後に反映されます"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="① チャージのしかた",
            value=(
                "**1.** `💰 チャージ` を押す\n"
                "**2.** チャージしたい金額を入力する\n"
                "**3.** Kyash アプリで「送る」→ **リンクで送る** で\n"
                "　　**同じ金額**の送金リンクを作る\n"
                "**4.** `🔗 送金リンクを送信` を押して URL を貼る\n"
                "**5.** 自動で受け取り、残高が増えます (結果は DM でお知らせ)"
            ),
            inline=False,
        )
    embed.add_field(
        name="いくら増えますか？",
        value=(
            f"チャージ率は **{utils.fmt_rate(settings.charge_rate)}** です。\n"
            f"例) **{utils.fmt_yen(base)}** 送金 → **{utils.fmt_int(credit)}** 獲得\n"
            "※ 端数は四捨五入します"
        ),
        inline=True,
    )
    embed.add_field(
        name="金額の条件",
        value=(
            f"1回: **{utils.fmt_yen(settings.minimum_charge)}** 〜 "
            f"**{utils.fmt_yen(settings.maximum_charge)}**\n"
            + (f"1日: **{utils.fmt_yen(settings.daily_limit)}** まで"
               if settings.daily_limit else "1日の上限: なし")
            + (f"\n残高上限: **{utils.fmt_int(settings.max_balance)}**"
               if settings.max_balance else "")
        ),
        inline=True,
    )
    embed.add_field(
        name="残高の使い道",
        value=(
            ("・ショップパネルからロールと交換できます\n" if shop_available else "")
            + ("・招待キャンペーンでも残高がもらえます "
               f"({campaign_name})\n" if campaign_name else "")
            + "・`💳 残高` `📜 履歴` でいつでも確認できます\n"
            "・現金化・出金・外部サービスとの交換はできません"
        ),
        inline=False,
    )
    cautions = [
        "・送った金額と申請した金額が**一致**していないと受け取れません",
        "・**請求リンクではなく送金リンク**を作成してください (Kyash)",
        f"・Kyash のリンク入力期限は約 {config.LINK_WAIT_SECONDS // 60} 分です",
        "・一度使ったリンク・取引ID は再利用できません",
        "・送金リンクや取引IDは**公開チャンネルに貼らないでください** (入力欄は非公開です)",
    ]
    if manual_usable:
        cautions.append(
            f"・PayPay / LTC の送金受付は約 {config.QUOTE_WAIT_SECONDS // 60} 分、"
            f"申請後の承認期限は {config.REQUEST_REVIEW_SECONDS // 3600} 時間です"
        )
        cautions.append("・**送金してから申請**してください (申請だけでは反映されません)")
        if any(p["provider"] == config.ChargeProvider.LTC for p in manual_usable):
            cautions.append(
                "・LTC は**表示された数量をそのまま**送ってください。"
                "レートは画面を開いた時点で確定します"
            )
            cautions.append("・**送金した LTC は返金できません**。宛先をよく確認してください")
    embed.add_field(name="気をつけること", value="\n".join(cautions), inline=False)
    timing = [
        "Kyash は通常 10〜60 秒ほどで完了します (混雑時は順番待ちになります)。",
    ]
    if manual_usable:
        timing.append(
            "PayPay / LTC は管理者が入金を確認してから反映するため、時間がかかります。"
            "進行状況は `📜 履歴` で確認できます。"
        )
    timing.append(
        "結果は必ず DM でお知らせします。DM が届かない設定の場合は履歴で確認してください。"
    )
    timing.append(
        "解決しないときは、**取引ID / 申請ID**を添えてサーバーの管理者へお問い合わせください。"
    )
    embed.add_field(
        name="処理時間 / うまくいかないとき", value="\n".join(timing), inline=False
    )
    embed.set_footer(text="内部残高システム / このサーバー専用の数値です")
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
    embed.add_field(name="日時", value=utils.discord_ts(timestamp), inline=True)
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.set_footer(text="内部残高システム")
    return embed


def shop_achievement_embed(
    *,
    user_mention: str,
    item_name: str,
    role_id: int,
    price: int,
    balance_after: int,
    expires_at: int | None,
    purchase_id: int,
    timestamp: int,
    status: str = config.PurchaseStatus.ACTIVE,
) -> discord.Embed:
    """ショップ購入の実績 (実績チャンネルへ投稿)。

    チャージ実績とデザインを揃え、残高以外の秘密情報は載せない。
    """
    if status == config.PurchaseStatus.ACTIVE:
        state_text, color = "🟢 購入完了", config.Color.SUCCESS
    elif status == config.PurchaseStatus.EXPIRED:
        state_text, color = "⚫ 期限切れ", config.Color.NEUTRAL
    elif status in (config.PurchaseStatus.REFUNDED, config.PurchaseStatus.FAILED):
        state_text, color = "↩️ 返金済み", config.Color.WARNING
    else:
        state_text, color = "🟡 処理中", config.Color.WARNING

    embed = discord.Embed(title="🛒 ショップ購入実績", description=SEPARATOR, color=color)
    embed.add_field(name="ユーザー", value=user_mention, inline=False)
    embed.add_field(name="商品", value=f"**{item_name}**", inline=True)
    embed.add_field(name="ロール", value=f"<@&{role_id}>", inline=True)
    embed.add_field(name="支払い", value=f"**{utils.fmt_int(price)}**", inline=True)
    embed.add_field(name="状態", value=state_text, inline=True)
    embed.add_field(
        name="有効期限",
        value=utils.discord_ts(expires_at) if expires_at else "無期限",
        inline=True,
    )
    embed.add_field(name="日時", value=utils.discord_ts(timestamp), inline=True)
    embed.add_field(name="購入後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(name="購入ID", value=f"`{purchase_id}`", inline=True)
    embed.set_footer(text="内部残高システム")
    return embed


def invite_achievement_embed(
    *,
    inviter_mention: str | None,
    invited_mention: str,
    inviter_reward: int,
    invited_reward: int,
    campaign_name: str,
    record_id: int,
    timestamp: int,
) -> discord.Embed:
    """招待報酬の確定実績 (実績チャンネルへ投稿)。"""
    embed = discord.Embed(
        title="🤝 招待実績",
        description=f"{SEPARATOR}\n**{campaign_name}**\n{SEPARATOR}",
        color=config.Color.ACCENT,
    )
    embed.add_field(name="招待した人", value=inviter_mention or "-", inline=True)
    embed.add_field(name="参加した人", value=invited_mention, inline=True)
    embed.add_field(name="状態", value="🟢 報酬確定", inline=True)
    embed.add_field(
        name="報酬",
        value=(
            f"招待した人: **{utils.fmt_int(inviter_reward)}**\n"
            f"参加した人: **{utils.fmt_int(invited_reward)}**"
        ),
        inline=True,
    )
    embed.add_field(name="日時", value=utils.discord_ts(timestamp), inline=True)
    embed.add_field(name="記録ID", value=f"`{record_id}`", inline=True)
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
    """チャージ完了の DM。"""
    embed = discord.Embed(
        title="✅ チャージが完了しました",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"残高が **+{utils.fmt_int(credited_amount)}** 増えました。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(
        name="内訳",
        value=(
            f"送金額: **{utils.fmt_yen(received_amount)}**\n"
            f"チャージ率: **{utils.fmt_rate(charge_rate)}**\n"
            f"獲得残高: **{utils.fmt_int(credited_amount)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="チャージ後の残高",
        value=f"**{utils.fmt_int(balance_after)}**",
        inline=True,
    )
    embed.add_field(
        name="取引情報",
        value=f"取引ID: `{tx_id}`\n日時: {utils.discord_ts(timestamp)}",
        inline=False,
    )
    embed.set_footer(text="残高はサーバーのパネルから確認できます")
    return embed


def dm_failure_embed(
    *, guild_name: str, tx_id: str, error_code: str, timestamp: int,
    requested_amount: int | None = None,
) -> discord.Embed:
    """失敗通知 (利用者には安全な一般向けメッセージと次の行動のみ)。"""
    message = config.USER_ERROR_MESSAGES.get(
        error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR]
    )
    action = config.USER_ERROR_NEXT_ACTIONS.get(error_code)
    embed = discord.Embed(
        title="⚠️ チャージを完了できませんでした",
        description=f"{SEPARATOR}\n**{guild_name}**\n{message}\n{SEPARATOR}",
        color=config.Color.DANGER,
    )
    if action:
        embed.add_field(name="▶ 次にどうすればいいですか？", value=action, inline=False)
    embed.add_field(
        name="この取引の情報",
        value=(
            (f"申請額: {utils.fmt_yen(requested_amount)}\n"
             if requested_amount is not None else "")
            + f"取引ID: `{tx_id}`\n日時: {utils.discord_ts(timestamp)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="残高について",
        value="**残高は増えていません。** 送金リンクが未使用の場合は Kyash アプリからキャンセルできます。",
        inline=False,
    )
    embed.set_footer(text="解決しない場合は取引IDを添えて管理者へお問い合わせください")
    return embed


def dm_review_embed(*, guild_name: str, tx_id: str, timestamp: int) -> discord.Embed:
    """確認中の通知。"""
    embed = discord.Embed(
        title="🔎 処理結果を確認しています",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            "受け取り結果の確認に時間がかかっています。\n"
            "**二重に受け取ることはありません**のでご安心ください。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(
        name="▶ どうすればいいですか？",
        value=(
            "・**そのままお待ちください** (確認が終わり次第 DM でお知らせします)\n"
            "・同じ送金リンクを再送信する必要はありません\n"
            "・新しくチャージをやり直さないでください"
        ),
        inline=False,
    )
    embed.add_field(
        name="この取引の情報",
        value=f"取引ID: `{tx_id}`\n日時: {utils.discord_ts(timestamp)}",
        inline=False,
    )
    return embed


def log_embed(
    title: str, description: str = "", *, color: int = config.Color.NEUTRAL,
    fields: Sequence[tuple[str, str, bool]] = (),
) -> discord.Embed:
    """ログチャンネル向け Embed (秘密情報は渡さないこと)。"""
    embed = discord.Embed(
        title=utils.truncate(title, EMBED_TITLE_MAX),
        description=utils.truncate(description, 3800),
        color=color,
    )
    for name, value, inline in list(fields)[:EMBED_FIELDS_MAX]:
        embed.add_field(
            name=utils.truncate(name, 250),
            value=utils.truncate(str(value), 1000) or "-",
            inline=inline,
        )
    embed.timestamp = discord.utils.utcnow()
    return clamp_embed(embed)


def error_embed(
    error_code: str, *, admin_detail: str | None = None, next_action: str | None = None
) -> discord.Embed:
    """利用者向けエラー表示。

    内部詳細やスタックトレースは出さず、「何が起きたか」と
    「次にどうすればよいか」をセットで示す。
    """
    embed = discord.Embed(
        title="⚠️ 実行できませんでした",
        description=config.USER_ERROR_MESSAGES.get(
            error_code, config.USER_ERROR_MESSAGES[config.ErrorCode.UNKNOWN_ERROR]
        ),
        color=config.Color.DANGER,
    )
    action = next_action or config.USER_ERROR_NEXT_ACTIONS.get(error_code)
    if action:
        embed.add_field(name="▶ 次にどうすればいいですか？", value=action, inline=False)
    if admin_detail:
        embed.add_field(name="詳細 (管理者向け)", value=utils.truncate(admin_detail, 900), inline=False)
    embed.set_footer(text=f"エラーコード: {error_code}")
    return embed


#: Kyash (自動) の手順は 3 段階。利用者が「今どこにいるか」を常に示す。
CHARGE_STEPS: Final[int] = 3
#: PayPay / LTC (承認制) は「方式選択」が入るため 4 段階。
MANUAL_CHARGE_STEPS: Final[int] = 4


def step_line(current: int, total: int = CHARGE_STEPS) -> str:
    """「ステップ 2 / 3 ●●○」の形で進行状況を返す。"""
    dots = "".join("●" if i < current else "○" for i in range(total))
    return f"`ステップ {current} / {total}`　{dots}"


def link_wait_embed(
    *,
    tx_id: str,
    amount: int,
    rate: Decimal,
    credited: int,
    expires_at: int | None = None,
    resumed: bool = False,
) -> discord.Embed:
    """ステップ2: 送金リンクの送信を待っている状態。"""
    embed = discord.Embed(
        title=("⌛ 送金リンクの送信をお待ちしています" if resumed
               else "🔗 次に「送金リンク」を送信してください"),
        description=(
            f"{step_line(2)}\n{SEPARATOR}\n"
            + ("前回の申請がまだ有効です。そのまま続けて送信できます。"
               if resumed else "Kyash アプリで送金リンクを作り、下のボタンから送ってください。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.WARNING if resumed else config.Color.ACCENT,
    )
    embed.add_field(
        name="この取引の内容",
        value=(
            f"送る金額: **{utils.fmt_yen(amount)}**\n"
            f"チャージ率: **{utils.fmt_rate(rate)}**\n"
            f"もらえる残高: **{utils.fmt_int(credited)}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="期限",
        value=(utils.discord_ts(expires_at, "R") if expires_at
               else f"約 {config.LINK_WAIT_SECONDS // 60} 分"),
        inline=True,
    )
    embed.add_field(
        name="▶ やること",
        value=(
            f"**1.** Kyash アプリで **{utils.fmt_yen(amount)}** ちょうどの送金リンクを作る\n"
            "**2.** 下の `🔗 リンクを送信` を押す\n"
            "**3.** 出てきた入力欄にリンクを貼って送信する"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ 注意",
        value=(
            "・金額が **1円でも違う**と受け付けられません\n"
            "・リンクを**公開チャンネルへ貼らない**でください (この画面はあなただけに見えています)\n"
            "・期限が切れたら最初からやり直してください"
        ),
        inline=False,
    )
    embed.set_footer(text=f"取引ID: {tx_id}")
    return embed


def link_accepted_embed(*, tx_id: str, amount: int, waiting: int) -> discord.Embed:
    """ステップ3: 受け取り処理待ち。"""
    embed = discord.Embed(
        title="✅ 送金リンクを受け付けました",
        description=(
            f"{step_line(3)}\n{SEPARATOR}\n"
            "あとは**自動で受け取り処理**を行います。操作は不要です。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="金額", value=f"**{utils.fmt_yen(amount)}**", inline=True)
    embed.add_field(
        name="順番待ち",
        value=("**あなたが次です**" if waiting <= 0 else f"あなたの前に **{waiting} 件**"),
        inline=True,
    )
    embed.add_field(
        name="この後の流れ",
        value=(
            "**1.** Bot が受け取り処理をします (通常は数十秒)\n"
            "**2.** 完了すると **DM** でお知らせします\n"
            "**3.** `💳 残高` で反映を確認できます\n\n"
            "DM が届かない場合は、サーバーからの DM を許可しているか確認してください。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"取引ID: {tx_id}")
    return embed


# Discord の Embed 上限。超えると送信が 400 で失敗し「Bot が無言になる」ため、
# 動的な文字列を載せる入口で必ず切り詰める。
EMBED_TITLE_MAX: Final[int] = 256
EMBED_DESC_MAX: Final[int] = 4096
EMBED_FIELDS_MAX: Final[int] = 25
EMBED_FIELD_NAME_MAX: Final[int] = 256
EMBED_FIELD_VALUE_MAX: Final[int] = 1024
EMBED_FOOTER_MAX: Final[int] = 2048
EMBED_TOTAL_MAX: Final[int] = 6000


def clamp_embed(embed: discord.Embed) -> discord.Embed:
    """Embed を Discord の上限内に収める (送信失敗を確実に防ぐ最後の砦)。"""
    if embed.title and len(embed.title) > EMBED_TITLE_MAX:
        embed.title = utils.truncate(embed.title, EMBED_TITLE_MAX)
    if embed.description and len(embed.description) > EMBED_DESC_MAX:
        embed.description = utils.truncate(embed.description, EMBED_DESC_MAX)
    if embed.footer and embed.footer.text and len(embed.footer.text) > EMBED_FOOTER_MAX:
        embed.set_footer(text=utils.truncate(embed.footer.text, EMBED_FOOTER_MAX))
    fields = list(embed.fields)
    if any(
        len(f.name or "") > EMBED_FIELD_NAME_MAX
        or len(f.value or "") > EMBED_FIELD_VALUE_MAX
        for f in fields
    ) or len(fields) > EMBED_FIELDS_MAX:
        embed.clear_fields()
        for f in fields[:EMBED_FIELDS_MAX]:
            embed.add_field(
                name=utils.truncate(f.name or "-", EMBED_FIELD_NAME_MAX),
                value=utils.truncate(f.value or "-", EMBED_FIELD_VALUE_MAX),
                inline=bool(f.inline),
            )
    # それでも合計が超える場合は末尾のフィールドから落とす
    while len(embed) > EMBED_TOTAL_MAX and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    if len(embed) > EMBED_TOTAL_MAX and embed.description:
        over = len(embed) - EMBED_TOTAL_MAX
        embed.description = utils.truncate(
            embed.description, max(1, len(embed.description) - over)
        )
    return embed


def info_embed(title: str, description: str, *, color: int = config.Color.INFO) -> discord.Embed:
    return discord.Embed(
        title=utils.truncate(title, EMBED_TITLE_MAX),
        description=utils.truncate(description, EMBED_DESC_MAX),
        color=color,
    )


def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(
        title=utils.truncate(title, EMBED_TITLE_MAX),
        description=utils.truncate(description, EMBED_DESC_MAX),
        color=config.Color.SUCCESS,
    )


def shop_panel_embed(settings: "GuildSettings", items: Sequence[Any]) -> discord.Embed:
    """常設ショップパネルの Embed。"""
    embed = discord.Embed(
        title="🛒 ロールショップ",
        description=(
            f"{SEPARATOR}\n"
            "チャージで増えた**内部残高**でロールを購入できます。\n"
            f"{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.ACCENT,
    )
    if not settings.shop_enabled:
        embed.add_field(
            name="状態", value="⚫ 現在ショップは停止中です (購入できません)", inline=False
        )
        return embed
    if not items:
        embed.add_field(
            name="商品",
            value="現在購入できる商品はありません。追加されるまでお待ちください。",
            inline=False,
        )
        return embed
    for item in list(items)[:10]:
        duration = int(item["duration_days"])
        stock = int(item["stock"])
        limit = int(item["purchase_limit"])
        lines = [
            f"💰 **{utils.fmt_int(int(item['price']))}**",
            f"⏳ {f'{duration}日間' if duration > 0 else '無期限 (買い切り)'}",
        ]
        if stock >= 0:
            lines.append(f"📦 残り **{stock}** 個" if stock else "📦 **在庫切れ**")
        if limit > 0:
            lines.append(f"🔒 1人 {limit} 回まで")
        detail = "　".join(lines)
        if item["description"]:
            detail += f"\n{utils.truncate(str(item['description']), 180)}"
        embed.add_field(
            name=f"🎫 {item['name']} → <@&{int(item['role_id'])}>",
            value=detail,
            inline=False,
        )
    embed.add_field(
        name="▶ 購入のしかた",
        value=(
            "**1.** `🛒 ショップを開く` を押す\n"
            "**2.** 商品を選ぶ (残高も表示されます)\n"
            "**3.** 内容を確認して `購入する` を押す\n"
            "→ 残高が引かれ、ロールがすぐに付与されます"
        ),
        inline=False,
    )
    embed.set_footer(text="購入後のキャンセルは管理者へご相談ください")
    return embed


def purchase_success_embed(
    *, item_name: str, role_id: int, price: int, balance_after: int,
    expires_at: int | None, purchase_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ 購入が完了しました",
        description=(
            f"{SEPARATOR}\n<@&{role_id}> を付与しました。\n"
            f"残高が **-{utils.fmt_int(price)}** されました。\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="商品", value=f"**{item_name}**", inline=True)
    embed.add_field(name="購入後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(
        name="有効期限",
        value=(
            f"{utils.discord_ts(expires_at)}\n({utils.discord_ts(expires_at, 'R')})"
            if expires_at else "無期限 (買い切り)"
        ),
        inline=True,
    )
    embed.add_field(name="購入ID", value=f"`{purchase_id}`", inline=True)
    if expires_at:
        embed.add_field(
            name="ご注意",
            value="有効期限が切れると**ロールは自動で外れます**。",
            inline=False,
        )
    embed.set_footer(text="購入履歴は 📦 ボタンから確認できます")
    return embed


def purchase_confirm_embed(
    *,
    item: Any,
    balance: int,
    owned: int,
    already_has_role: bool,
) -> tuple[discord.Embed, str | None]:
    """購入前の確認画面。

    Returns:
        ``(embed, blocker)``。``blocker`` が None でなければ購入できない理由。
        先に理由を示すことで「押したのに失敗した」を避ける。
    """
    price = int(item["price"])
    duration = int(item["duration_days"])
    stock = int(item["stock"])
    limit = int(item["purchase_limit"])

    blocker: str | None = None
    if duration == 0 and already_has_role:
        blocker = "すでにこのロールを持っています。"
    elif stock == 0:
        blocker = "在庫がありません。"
    elif limit > 0 and owned >= limit:
        blocker = f"購入できる回数の上限 ({limit}回) に達しています。"
    elif balance < price:
        blocker = (
            f"残高が **{utils.fmt_int(price - balance)}** 足りません。\n"
            "`💰 チャージ` で残高を増やしてから、もう一度お試しください。"
        )

    embed = discord.Embed(
        title="🛒 購入の確認" if blocker is None else "⚠️ いま購入できません",
        description=(
            f"{SEPARATOR}\n"
            + ("下の `購入する` を押すと**すぐに残高から引き落とし**、ロールが付きます。\n"
               if blocker is None else f"{blocker}\n")
            + SEPARATOR
        ),
        color=config.Color.ACCENT if blocker is None else config.Color.WARNING,
    )
    embed.add_field(name="商品", value=f"**{item['name']}**", inline=True)
    embed.add_field(name="もらえるロール", value=f"<@&{int(item['role_id'])}>", inline=True)
    embed.add_field(name="価格", value=f"**{utils.fmt_int(price)}**", inline=True)
    embed.add_field(
        name="有効期間",
        value=(f"**{duration}日間** (期限が来ると自動で外れます)" if duration > 0 else "**無期限**"),
        inline=False,
    )
    embed.add_field(name="いまの残高", value=f"**{utils.fmt_int(balance)}**", inline=True)
    embed.add_field(
        name="購入後の残高",
        value=(f"**{utils.fmt_int(balance - price)}**" if balance >= price
               else f"不足 **{utils.fmt_int(price - balance)}**"),
        inline=True,
    )
    if limit > 0:
        embed.add_field(
            name="購入回数", value=f"{owned} / {limit} 回", inline=True
        )
    if stock >= 0:
        embed.add_field(name="残り在庫", value=f"{stock} 個", inline=True)
    if item["description"]:
        embed.add_field(
            name="説明", value=utils.truncate(str(item["description"]), 900), inline=False
        )
    embed.set_footer(
        text="購入後の返金は管理者の操作が必要です" if blocker is None
        else "条件を満たすと購入できるようになります"
    )
    return embed, blocker


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
                f"購入: {utils.discord_ts(int(row['created_at']))}\n"
                + (
                    f"期限: {utils.discord_ts(row['expires_at'])} "
                    f"({utils.discord_ts(row['expires_at'], 'R')})\n"
                    if row["expires_at"] else "期限: 無期限\n"
                )
                + f"購入ID: `{row['id']}`"
            ),
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# チャージ方式 (PayPay / LTC の申請と審査)
# ---------------------------------------------------------------------------

def provider_select_embed(entries: Sequence[dict[str, Any]]) -> discord.Embed:
    """チャージ方式の選択画面 (ステップ 1/4)。

    使えない方式も理由つきで見せることで、「なぜ選べないのか」を説明する。
    """
    embed = discord.Embed(
        title="💰 チャージ方法を選んでください",
        description=(
            f"{step_line(1, MANUAL_CHARGE_STEPS)}\n{SEPARATOR}\n"
            "下のメニューから方法を選ぶと、次の手順を案内します。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.ACCENT,
    )
    for entry in entries:
        provider = str(entry["provider"])
        emoji = config.PROVIDER_EMOJI.get(provider, "💠")
        name = config.PROVIDER_LABELS.get(provider, provider)
        if entry["available"]:
            value = (
                f"{config.PROVIDER_DESCRIPTIONS.get(provider, '')}\n"
                f"チャージ率 **{utils.fmt_rate(entry['rate'])}** / "
                f"{utils.fmt_yen(int(entry['minimum']))} 〜 {utils.fmt_yen(int(entry['maximum']))}"
            )
        else:
            value = f"🚫 いま使えません — {entry['reason']}"
        embed.add_field(name=f"{emoji} {name}", value=value, inline=False)
    if not any(e["available"] for e in entries):
        embed.add_field(
            name="▶ 次にどうすればいいですか？",
            value="現在チャージを受け付けていません。管理者の案内をお待ちください。",
            inline=False,
        )
    return embed


def deposit_embed(quote: dict[str, Any]) -> discord.Embed:
    """入金先の案内 (ステップ 3/4)。

    LTC はここで提示した数量と単価が確定値になる。相場が動いても、
    この画面の金額を送れば申請できる。
    """
    provider = str(quote["provider"])
    destination = quote["destination"]
    amount = int(quote["amount"])
    asset_amount = quote.get("asset_amount")
    is_ltc = provider == config.ChargeProvider.LTC

    embed = discord.Embed(
        title=f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
              f"{config.PROVIDER_LABELS.get(provider, provider)} で送金してください",
        description=(
            f"{step_line(3, MANUAL_CHARGE_STEPS)}\n{SEPARATOR}\n"
            "**下の宛先へ、表示された金額をそのまま送ってください。**\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.ACCENT,
    )
    if is_ltc:
        embed.add_field(
            name="① 送る数量 (この数量をそのまま)",
            value=f"```\n{utils.fmt_asset(asset_amount, unit='')}\n```",
            inline=False,
        )
    else:
        embed.add_field(
            name="① 送る金額",
            value=f"```\n{amount}\n```",
            inline=False,
        )
    embed.add_field(
        name=f"② 送り先 ({destination['label'] or '受取先'})",
        value=f"```\n{destination['address']}\n```",
        inline=False,
    )
    detail = [
        f"申請額: **{utils.fmt_yen(amount)}**",
        f"チャージ率: **{utils.fmt_rate(quote['charge_rate'])}**"
        + (f" (<@&{int(quote['role_id'])}>)" if quote.get("role_id") else ""),
        f"もらえる残高: **{utils.fmt_int(int(quote['estimated_credit']))}**",
    ]
    if is_ltc and quote.get("asset_price") is not None:
        source = config.PRICE_SOURCE_LABELS.get(
            str(quote.get("price_source")), str(quote.get("price_source"))
        )
        detail.append(
            f"適用レート: **1 LTC = {utils.fmt_yen(int(quote['asset_price']))}**"
            + ("  ⚠️ 取得できなかったため代替値" if quote.get("price_stale") else "")
        )
        detail.append(f"レート取得元: {source}")
    embed.add_field(name="この申請の内容", value="\n".join(detail), inline=False)
    embed.add_field(
        name="③ 送金したら",
        value=(
            "下の `✅ 送金しました` を押して、"
            f"**{config.PROVIDER_PROOF_LABELS.get(provider, '証拠')}** を入力してください。\n"
            "管理者が確認して承認すると残高に反映されます。"
        ),
        inline=False,
    )
    warn = [
        f"・受付期限は {utils.discord_ts(quote.get('quote_expires_at'), 'R')} までです",
        "・**金額が違うと承認されません**。表示された額をそのまま送ってください",
        "・送金してから申請してください (申請だけでは反映されません)",
    ]
    if is_ltc:
        warn.append("・**LTC の返金はできません**。宛先と数量をよく確認してください")
        if quote.get("asset_price") is not None:
            warn.append(
                "・レートは**この画面を開いた時点で確定**しています "
                f"(1 LTC = {utils.fmt_yen(int(quote['asset_price']))})。"
                "その後に相場が動いても、この数量を送れば申請できます"
            )
    if destination["note"]:
        warn.append(f"・{utils.truncate(str(destination['note']), 300)}")
    embed.add_field(name="⚠️ 注意", value="\n".join(warn), inline=False)
    embed.set_footer(text=f"申請ID: #{quote['request_id']}")
    return embed


def request_submitted_embed(result: dict[str, Any]) -> discord.Embed:
    """申請完了 (ステップ 4/4)。"""
    provider = str(result["provider"])
    embed = discord.Embed(
        title="📨 申請を受け付けました",
        description=(
            f"{step_line(4, MANUAL_CHARGE_STEPS)}\n{SEPARATOR}\n"
            "**管理者の承認をお待ちください。** これ以上の操作は不要です。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="申請ID", value=f"`#{result['request_id']}`", inline=True)
    embed.add_field(
        name="方式",
        value=config.PROVIDER_LABELS.get(provider, provider),
        inline=True,
    )
    embed.add_field(
        name="付与予定",
        value=f"**{utils.fmt_int(int(result['estimated_credit']))}**",
        inline=True,
    )
    embed.add_field(
        name="この後の流れ",
        value=(
            "**1.** 管理者が入金を確認します\n"
            "**2.** 承認されると残高に反映され、**DM** でお知らせします\n"
            "**3.** `📜 履歴` で申請の状態を確認できます\n\n"
            f"承認されない場合の期限: {utils.discord_ts(result.get('expires_at'), 'R')}"
        ),
        inline=False,
    )
    waiting = int(result.get("pending_total") or 0)
    if waiting > 1:
        embed.set_footer(text=f"現在 {waiting} 件の申請が承認待ちです")
    return embed


def review_card_embed(
    *, request: Any, guild_name: str, pending_total: int = 0
) -> discord.Embed:
    """審査チャンネルへ出すカード。管理者が照合に必要な情報だけを載せる。"""
    provider = str(request["provider"])
    status = str(request["status"])
    color = {
        config.RequestStatus.PENDING: config.Color.WARNING,
        config.RequestStatus.APPROVED: config.Color.SUCCESS,
        config.RequestStatus.REJECTED: config.Color.DANGER,
        config.RequestStatus.EXPIRED: config.Color.NEUTRAL,
        config.RequestStatus.CANCELLED: config.Color.NEUTRAL,
        config.RequestStatus.QUOTED: config.Color.INFO,
    }.get(status, config.Color.INFO)
    embed = discord.Embed(
        title=f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
              f"{config.PROVIDER_LABELS.get(provider, provider)} チャージ申請 "
              f"#{int(request['id'])}",
        description=(
            f"{SEPARATOR}\n"
            f"**{config.REQUEST_STATUS_LABELS.get(status, status)}**\n"
            f"{SEPARATOR}"
        ),
        color=color,
    )
    embed.add_field(name="サーバー", value=f"{guild_name}\n`{int(request['guild_id'])}`", inline=True)
    embed.add_field(name="利用者", value=f"<@{int(request['user_id'])}>", inline=True)
    embed.add_field(
        name="申請額", value=f"**{utils.fmt_yen(int(request['requested_amount']))}**", inline=True
    )
    if request["asset_amount"] is not None:
        expected = utils.to_decimal(request["asset_amount"])
        embed.add_field(
            name="送金数量 (申請)", value=f"**{utils.fmt_asset(expected)}**", inline=True
        )
    if request["asset_price"] is not None:
        embed.add_field(
            name="確定レート",
            value=f"1 LTC = {utils.fmt_yen(int(utils.to_decimal(request['asset_price']) or 0))}",
            inline=True,
        )
    embed.add_field(
        name="チャージ率",
        value=utils.fmt_rate(request["charge_rate"])
        + (f"\n<@&{int(request['role_id'])}>" if request["role_id"] else ""),
        inline=True,
    )
    embed.add_field(
        name="付与予定",
        value=f"**{utils.fmt_int(int(request['estimated_credit']))}**",
        inline=True,
    )
    if request["proof_ref"]:
        label = config.PROVIDER_PROOF_LABELS.get(provider, "証拠")
        value = f"```\n{utils.truncate(str(request['proof_ref']), 200)}\n```"
        if provider == config.ChargeProvider.LTC:
            value += (
                "エクスプローラで着金・数量・承認数を確認してください。\n"
                f"`{utils.truncate(str(request['proof_ref']), 64)}`"
            )
        embed.add_field(name=f"📋 照合用: {label}", value=value, inline=False)
    if request["destination"]:
        embed.add_field(
            name="送り先", value=f"`{utils.truncate(str(request['destination']), 120)}`",
            inline=False,
        )
    times = [f"申請: {utils.discord_ts(request['submitted_at'] or request['created_at'])}"]
    if status == config.RequestStatus.PENDING and request["expires_at"]:
        times.append(f"期限: {utils.discord_ts(request['expires_at'], 'R')}")
    if request["reviewed_at"]:
        times.append(
            f"処理: {utils.discord_ts(request['reviewed_at'])}"
            + (f" (<@{int(request['reviewed_by'])}>)" if request["reviewed_by"] else "")
        )
    embed.add_field(name="日時", value="\n".join(times), inline=False)
    if status == config.RequestStatus.APPROVED:
        embed.add_field(
            name="結果",
            value=(
                f"付与 **{utils.fmt_int(int(request['credited_amount'] or 0))}**\n"
                f"取引ID `{request['transaction_id']}`"
            ),
            inline=False,
        )
    elif status == config.RequestStatus.REJECTED and request["reject_reason"]:
        embed.add_field(
            name="却下理由", value=utils.truncate(str(request["reject_reason"]), 900), inline=False
        )
    if status == config.RequestStatus.PENDING:
        embed.add_field(
            name="▶ 確認してから押してください",
            value=(
                "**入金が実際に届いているか**を自分の目で確認してから承認してください。\n"
                "`🟢 承認` … 表示どおりの額を付与\n"
                "`✏️ 金額を直して承認` … 実際の入金額と違うとき\n"
                "`🔴 却下` … 理由を入力して却下 (残高は動きません)"
            ),
            inline=False,
        )
        if pending_total > 1:
            embed.set_footer(text=f"未処理の申請: {pending_total} 件")
    return embed


def review_detail_embed(
    *, request: Any, history: Sequence[Any], balance: int
) -> discord.Embed:
    """審査の「詳細」表示。承認前に見ておきたい情報をまとめる。"""
    provider = str(request["provider"])
    embed = discord.Embed(
        title=f"🔍 申請 #{int(request['id'])} の詳細",
        description=f"{SEPARATOR}\n照合に使う値はコードブロックからコピーできます。\n{SEPARATOR}",
        color=config.Color.INFO,
    )
    if request["proof_ref"]:
        embed.add_field(
            name=config.PROVIDER_PROOF_LABELS.get(provider, "証拠"),
            value=f"```\n{utils.truncate(str(request['proof_ref']), 300)}\n```",
            inline=False,
        )
    if request["destination"]:
        embed.add_field(
            name="入金先",
            value=f"```\n{utils.truncate(str(request['destination']), 200)}\n```",
            inline=False,
        )
    amounts = [
        f"申請額: **{utils.fmt_yen(int(request['requested_amount']))}**",
        f"チャージ率: **{utils.fmt_rate(request['charge_rate'])}**",
        f"付与予定: **{utils.fmt_int(int(request['estimated_credit']))}**",
    ]
    if request["asset_amount"] is not None:
        amounts.append(f"送金数量 (申請): **{utils.fmt_asset(request['asset_amount'])}**")
    if request["asset_price"] is not None:
        price = utils.to_decimal(request["asset_price"]) or Decimal(0)
        amounts.append(f"確定レート: 1 LTC = **{utils.fmt_yen(int(price))}**")
        source = config.PRICE_SOURCE_LABELS.get(
            str(request["price_source"]), str(request["price_source"] or "-")
        )
        amounts.append(f"レート取得元: {source}")
        amounts.append(f"レート取得時刻: {utils.discord_ts(request['price_fetched_at'])}")
    embed.add_field(name="金額とレート", value="\n".join(amounts), inline=False)
    embed.add_field(
        name="利用者",
        value=(
            f"<@{int(request['user_id'])}> (`{int(request['user_id'])}`)\n"
            f"現在の残高: **{utils.fmt_int(balance)}**"
        ),
        inline=False,
    )
    if history:
        lines = [
            f"`#{int(r['id'])}` {config.REQUEST_STATUS_LABELS.get(str(r['status']), r['status'])} "
            f"{config.PROVIDER_LABELS.get(str(r['provider']), r['provider'])} "
            f"{utils.fmt_yen(int(r['requested_amount']))} "
            f"{utils.discord_ts(r['submitted_at'] or r['created_at'], 'R')}"
            for r in list(history)[:5]
        ]
        embed.add_field(
            name="この利用者の直近の申請", value="\n".join(lines), inline=False
        )
    embed.add_field(
        name="⚠️ 承認の前に",
        value=(
            "**入金が実際に届いていることを自分で確認してください。**\n"
            + ("Litecoin はエクスプローラで txid・宛先・数量・承認数を確認できます。\n"
               "**承認後の返金はブロックチェーン上では行えません。**"
               if provider == config.ChargeProvider.LTC else
               "PayPay アプリの取引履歴で、取引IDと金額が一致することを確認してください。")
        ),
        inline=False,
    )
    return embed


def request_result_dm_embed(*, guild_name: str, request: Any) -> discord.Embed:
    """却下・期限切れを利用者へ知らせる DM。"""
    provider = str(request["provider"])
    status = str(request["status"])
    if status == config.RequestStatus.REJECTED:
        title = "🔴 チャージ申請が却下されました"
        color = config.Color.DANGER
        guidance = (
            "残高は変わっていません。内容を確認し、必要なら最初からやり直してください。\n"
            "身に覚えがない場合はサーバーの管理者へお問い合わせください。"
        )
    else:
        title = "⚫ チャージ申請が期限切れになりました"
        color = config.Color.NEUTRAL
        guidance = (
            "承認されないまま期限を過ぎました。残高は変わっていません。\n"
            "送金済みの場合は、申請IDを添えてサーバーの管理者へお問い合わせください。"
        )
    embed = discord.Embed(
        title=title,
        description=f"{SEPARATOR}\n**{guild_name}**\n{SEPARATOR}",
        color=color,
    )
    embed.add_field(name="申請ID", value=f"`#{int(request['id'])}`", inline=True)
    embed.add_field(
        name="方式", value=config.PROVIDER_LABELS.get(provider, provider), inline=True
    )
    embed.add_field(
        name="申請額", value=utils.fmt_yen(int(request["requested_amount"])), inline=True
    )
    if request["reject_reason"]:
        embed.add_field(
            name="理由", value=utils.truncate(str(request["reject_reason"]), 900), inline=False
        )
    embed.add_field(name="▶ 次にどうすればいいですか？", value=guidance, inline=False)
    return embed


def request_list_embed(
    rows: Sequence[Any], *, page: int, total_pages: int, total: int, title: str
) -> discord.Embed:
    """申請の一覧 (管理者・利用者共通)。"""
    embed = discord.Embed(
        title=title,
        description=(
            f"{SEPARATOR}\n全 **{utils.fmt_int(total)}** 件のうち {len(rows)} 件を表示\n{SEPARATOR}"
            if total else f"{SEPARATOR}\n該当する申請はありません。\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    for row in list(rows)[:10]:
        provider = str(row["provider"])
        status = str(row["status"])
        lines = [
            f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
            f"{config.PROVIDER_LABELS.get(provider, provider)} / "
            f"<@{int(row['user_id'])}>",
            f"申請 **{utils.fmt_yen(int(row['requested_amount']))}** → "
            f"付与予定 **{utils.fmt_int(int(row['estimated_credit']))}**",
        ]
        if row["asset_amount"] is not None:
            lines.append(f"数量 {utils.fmt_asset(row['asset_amount'])}")
        if row["proof_ref"]:
            lines.append(f"証拠 `{utils.truncate(str(row['proof_ref']), 40)}`")
        lines.append(utils.discord_ts(row["submitted_at"] or row["created_at"], "R"))
        if status == config.RequestStatus.APPROVED and row["credited_amount"] is not None:
            lines.append(f"付与 **{utils.fmt_int(int(row['credited_amount']))}**")
        if status == config.RequestStatus.REJECTED and row["reject_reason"]:
            lines.append(f"理由 {utils.truncate(str(row['reject_reason']), 100)}")
        embed.add_field(
            name=f"#{int(row['id'])} {config.REQUEST_STATUS_LABELS.get(status, status)}",
            value="\n".join(lines),
            inline=False,
        )
    embed.set_footer(text=f"ページ {page}/{max(1, total_pages)}")
    return embed


def provider_status_embed(
    entries: Sequence[dict[str, Any]], *, guild_name: str,
    review_channel_id: int | None, price: dict[str, Any] | None,
    delegated: bool,
) -> discord.Embed:
    """管理者向けの方式一覧。"""
    embed = discord.Embed(
        title="💠 チャージ方式の状態",
        description=f"{SEPARATOR}\n**{guild_name}**\n{SEPARATOR}",
        color=config.Color.INFO,
    )
    for entry in entries:
        provider = str(entry["provider"])
        mark = "🟢 利用可" if entry["available"] else f"🔴 {entry['reason']}"
        lines = [
            mark,
            f"チャージ率: **{utils.fmt_rate(entry['rate'])}**",
            f"金額: {utils.fmt_yen(int(entry['minimum']))} 〜 {utils.fmt_yen(int(entry['maximum']))}",
        ]
        destination = entry.get("destination")
        if destination is not None:
            lines.append(
                f"入金先: `{utils.truncate(str(destination['address']), 60)}`"
            )
        embed.add_field(
            name=f"{config.PROVIDER_EMOJI.get(provider, '💠')} "
                 f"{config.PROVIDER_LABELS.get(provider, provider)}",
            value="\n".join(lines),
            inline=False,
        )
    review = f"<#{review_channel_id}>" if review_channel_id else "未設定"
    embed.add_field(
        name="審査",
        value=(
            f"審査チャンネル: {review}\n"
            f"このサーバーの管理者による承認: {'許可' if delegated else '不可 (Bot Owner のみ)'}"
        ),
        inline=False,
    )
    if price is not None:
        source = config.PRICE_SOURCE_LABELS.get(
            str(price.get("source")), str(price.get("source"))
        )
        value = [f"取得元: {source}"]
        if price.get("cached_price"):
            value.append(
                f"直近の価格: **1 LTC = {utils.fmt_yen(int(float(price['cached_price'])))}**"
                f" ({price.get('cached_age')}秒前"
                + ("・代替値" if price.get("cached_stale") else "") + ")"
            )
        elif price.get("last_good_price"):
            value.append(
                f"最後に取得できた価格: 1 LTC = "
                f"{utils.fmt_yen(int(float(price['last_good_price'])))}"
                f" ({utils.discord_ts(price.get('last_good_at'), 'R')})"
            )
        if price.get("manual_price"):
            value.append(
                f"手動設定: 1 LTC = {utils.fmt_yen(int(float(price['manual_price'])))}"
                f" ({utils.discord_ts(price.get('manual_updated_at'), 'R')})"
            )
        if price.get("consecutive_failures"):
            value.append(f"⚠️ 連続取得失敗: {price['consecutive_failures']} 回")
        if price.get("last_error"):
            value.append(f"直近のエラー: {utils.truncate(str(price['last_error']), 200)}")
        embed.add_field(name="Ł LTC 価格", value="\n".join(value), inline=False)
    return embed


def invite_panel_embed(settings: "GuildSettings", campaign: Any | None) -> discord.Embed:
    """招待キャンペーンのパネル。"""
    if campaign is None:
        return discord.Embed(
            title="🤝 招待キャンペーン",
            description=(
                f"{SEPARATOR}\n現在開催中のキャンペーンはありません。\n"
                "次の開催をお待ちください。\n"
                f"{SEPARATOR}"
            ),
            color=config.Color.NEUTRAL,
        )
    conditions: list[str] = []
    if campaign["require_charge"]:
        conditions.append("招待した人が**チャージを1回完了**したとき")
    if int(campaign["require_days"] or 0) > 0:
        conditions.append(f"参加から**{campaign['require_days']}日**サーバーに残ったとき")
    if not conditions:
        conditions.append("参加が確認できたとき")

    embed = discord.Embed(
        title=f"🤝 {campaign['name']}",
        description=(
            f"{SEPARATOR}\n"
            "あなた専用の招待リンクで友達を招待すると、**内部残高がもらえます**。\n"
            f"{SEPARATOR}"
        ),
        color=settings.accent_color if settings.accent_color is not None else config.Color.ACCENT,
    )
    embed.add_field(
        name="🎁 もらえる残高",
        value=(
            f"招待した人: **{utils.fmt_int(int(campaign['inviter_reward']))}**\n"
            f"招待された人: **{utils.fmt_int(int(campaign['invited_reward']))}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="✅ 報酬が確定する条件",
        value="\n".join(f"・{c}" for c in conditions),
        inline=True,
    )
    embed.add_field(
        name="▶ 参加のしかた",
        value=(
            "**1.** `🔗 招待リンクを取得` を押す (あなた専用のリンクが出ます)\n"
            "**2.** そのリンクを友達に送る\n"
            "**3.** 友達が参加し、条件を満たすと自動で残高が入ります\n"
            "**4.** `📊 自分の招待状況` で進行状況を確認できます"
        ),
        inline=False,
    )
    limits = [
        f"Discord アカウント作成から **{campaign['min_account_age_days']}日**以上",
        (f"1日 **{campaign['daily_limit']}人**まで"
         if int(campaign["daily_limit"] or 0) > 0 else "1日の上限なし"),
        (f"累計 **{campaign['total_limit']}人**まで"
         if int(campaign["total_limit"] or 0) > 0 else "累計上限なし"),
    ]
    embed.add_field(
        name="📋 条件・上限",
        value="\n".join(f"・{x}" for x in limits),
        inline=False,
    )
    embed.add_field(
        name="⚠️ 対象にならない招待",
        value=(
            "・自分自身の招待\n"
            "・**一度このサーバーに参加したことがある人** (退出して再参加した場合も対象外)\n"
            "・Bot アカウント\n"
            "・招待リンク以外 (サーバー検索など) からの参加\n"
            "※ 不自然な招待は保留され、管理者が確認します"
        ),
        inline=False,
    )
    if campaign["ends_at"]:
        embed.set_footer(
            text=f"終了予定 {utils.format_jst(int(campaign['ends_at']))}"
        )
    else:
        embed.set_footer(text="終了時期は未定です")
    return embed


def invite_link_embed(
    *, url: str, code: str, summary: dict[str, int], created: bool
) -> discord.Embed:
    embed = discord.Embed(
        title="🔗 あなた専用の招待リンク",
        description=(
            f"{SEPARATOR}\n"
            f"```\n{url}\n```\n"
            + ("新しく発行しました。" if created else "すでに発行済みのリンクです (同じものを使い続けてください)。")
            + "\n**このリンク経由の参加だけ**が報酬の対象になります。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.ACCENT,
    )
    embed.add_field(
        name="現在の招待状況",
        value=(
            f"🟢 確定: **{summary['confirmed']}人**\n"
            f"⌛ 保留中: {summary['pending']}人 (条件待ち)\n"
            f"🟠 要確認: {summary['hold']}人\n"
            f"🔴 無効: {summary['rejected']}人"
        ),
        inline=True,
    )
    embed.add_field(
        name="獲得した報酬",
        value=f"**{utils.fmt_int(summary['reward'])}**",
        inline=True,
    )
    embed.set_footer(text=f"招待コード: {code} / リンクは他の人に渡しても構いません")
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
    requests: dict[str, int] | None = None,
    price: dict[str, Any] | None = None,
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
    pending_requests = int((requests or {}).get(config.RequestStatus.PENDING, 0))
    if pending_requests:
        problems.append(f"🟠 承認待ちの申請 {pending_requests} 件")
    if price is not None and price.get("consecutive_failures"):
        problems.append(f"🟠 LTC 価格の取得失敗 {price['consecutive_failures']} 回")
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
    if requests is not None:
        embed.add_field(
            name="チャージ申請",
            value=(
                f"承認待ち: **{pending_requests}**\n"
                f"送金待ち: {requests.get(config.RequestStatus.QUOTED, 0)}\n"
                f"承認済み: {requests.get(config.RequestStatus.APPROVED, 0)} / "
                f"却下: {requests.get(config.RequestStatus.REJECTED, 0)}"
            ),
            inline=True,
        )
    if price is not None:
        current = price.get("cached_price") or price.get("last_good_price")
        embed.add_field(
            name="LTC 価格",
            value=(
                (f"1 LTC = **{utils.fmt_yen(int(float(current)))}**\n"
                 if current else "未取得\n")
                + f"取得元: {config.PRICE_SOURCE_LABELS.get(str(price.get('source')), '-')}"
                + ("\n⚠️ 代替値を使用中" if price.get("cached_stale") else "")
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
        kwargs["embed"] = clamp_embed(embed)
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

class AmountModal(discord.ui.Modal, title="ステップ1 / 3 ・ 金額の入力"):
    """チャージ金額を入力する Modal。"""

    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="Kyash で送る金額 (円・半角数字のみ)",
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
        await bot_of(interaction).handle_amount_submit(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("金額入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class ManualAmountModal(discord.ui.Modal):
    """PayPay / LTC の金額入力 (ステップ 2/4)。

    LTC でも入力は「円」。その時のレートで送る数量を Bot が計算して示す。
    利用者に暗号資産の計算をさせない。
    """

    def __init__(
        self, provider: str, settings: "GuildSettings", limits: tuple[int, int]
    ) -> None:
        name = config.PROVIDER_LABELS.get(provider, provider)
        super().__init__(title=f"ステップ2 / 4 ・ {utils.truncate(name, 20)} の金額", timeout=300)
        self.provider = provider
        low, high = limits
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label="チャージしたい金額 (円・半角数字のみ)",
            placeholder=f"{low}〜{high} の範囲で入力",
            required=True,
            min_length=1,
            max_length=config.AMOUNT_INPUT_MAX_LEN,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_manual_amount_submit(
            interaction, self.provider, str(self.amount.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("金額入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class ProviderSelect(discord.ui.Select["ProviderSelectView"]):
    """チャージ方式の選択メニュー (使える方式だけを並べる)。"""

    def __init__(self, entries: Sequence[dict[str, Any]]) -> None:
        options: list[discord.SelectOption] = []
        for entry in entries:
            if not entry["available"]:
                continue
            provider = str(entry["provider"])
            options.append(
                discord.SelectOption(
                    label=config.PROVIDER_LABELS.get(provider, provider),
                    value=provider,
                    description=utils.truncate(
                        f"{utils.fmt_rate(entry['rate'])} / "
                        f"{config.PROVIDER_DESCRIPTIONS.get(provider, '')}", 95,
                    ),
                    emoji=config.PROVIDER_EMOJI.get(provider),
                )
            )
        # 都度生成する一時 View なので custom_id は固定しない
        super().__init__(
            placeholder="チャージ方法を選んでください" if options else "利用できる方法がありません",
            min_values=1,
            max_values=1,
            options=options or [
                discord.SelectOption(label="利用できる方法がありません", value="none")
            ],
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        value = self.values[0]
        if value == "none":
            await safe_respond(
                interaction,
                embed=info_embed("利用できません", "現在チャージを受け付けていません。"),
            )
            return
        await bot_of(interaction).on_provider_selected(interaction, value)


class ProviderSelectView(discord.ui.View):
    """方式選択 (Ephemeral・一時 View)。"""

    def __init__(self, entries: Sequence[dict[str, Any]], *, timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.add_item(ProviderSelect(entries))


class DepositView(discord.ui.View):
    """入金先を案内した後の「送金しました」ボタン (Ephemeral・一時 View)。"""

    def __init__(self, request_id: int, provider: str, *, owner_id: int, timeout: float) -> None:
        super().__init__(timeout=max(30.0, timeout))
        self.request_id = int(request_id)
        self.provider = provider
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await safe_respond(
                interaction,
                embed=info_embed("操作できません", "この画面を開いた本人のみ操作できます。"),
            )
            return False
        return True

    @discord.ui.button(label="送金しました", emoji="✅", style=discord.ButtonStyle.success)
    async def submitted(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            RequestProofModal(self.request_id, self.provider)
        )

    @discord.ui.button(label="やめる", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_request_cancel(interaction, self.request_id)
        self.stop()


class RequestProofModal(discord.ui.Modal):
    """送金の証拠を入力する Modal (ステップ 3/4 → 4/4)。"""

    def __init__(self, request_id: int, provider: str) -> None:
        is_ltc = provider == config.ChargeProvider.LTC
        super().__init__(
            title="ステップ3 / 4 ・ 送金の申請",
            timeout=float(config.QUOTE_WAIT_SECONDS),
        )
        self.request_id = int(request_id)
        self.provider = provider
        self.proof: discord.ui.TextInput = discord.ui.TextInput(
            label=("トランザクションID (txid)" if is_ltc else "PayPay の取引ID"),
            placeholder=("64桁の英数字をそのまま貼り付け" if is_ltc else "アプリの取引詳細に表示されるID"),
            required=True,
            min_length=6,
            max_length=200,
        )
        self.add_item(self.proof)
        self.asset: discord.ui.TextInput | None = None
        if is_ltc:
            self.asset = discord.ui.TextInput(
                label="実際に送った数量 (LTC)",
                placeholder="例: 0.08333334",
                required=False,
                max_length=32,
            )
            self.add_item(self.asset)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_request_submit(
            interaction,
            self.request_id,
            str(self.proof.value),
            str(self.asset.value) if self.asset is not None else None,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("申請Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class ReviewCardView(discord.ui.View):
    """審査カードのボタン (Persistent View / 再起動後も動く)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="承認", emoji="🟢", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.REQUEST_APPROVE, row=0,
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_review_approve(interaction)

    @discord.ui.button(
        label="金額を直して承認", emoji="✏️", style=discord.ButtonStyle.primary,
        custom_id=config.CustomID.REQUEST_EDIT_APPROVE, row=0,
    )
    async def edit_approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await bot_of(interaction).on_review_edit_approve(interaction)

    @discord.ui.button(
        label="却下", emoji="🔴", style=discord.ButtonStyle.danger,
        custom_id=config.CustomID.REQUEST_REJECT, row=0,
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_review_reject(interaction)

    @discord.ui.button(
        label="詳細", emoji="🔍", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.REQUEST_DETAIL, row=1,
    )
    async def detail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_review_detail(interaction)


class ApproveAmountModal(discord.ui.Modal, title="付与する額を入力して承認"):
    """実際の入金額が申請と違うときに額を直して承認する。"""

    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="付与する内部残高 (半角数字)",
        placeholder="例: 1200",
        required=True,
        max_length=12,
    )
    note: discord.ui.TextInput = discord.ui.TextInput(
        label="修正の理由 (監査ログに残ります)",
        style=discord.TextStyle.paragraph,
        placeholder="例: 実際の入金が 950 円だったため",
        required=True,
        max_length=300,
    )

    def __init__(self, request_id: int, suggested: int) -> None:
        super().__init__(timeout=600)
        self.request_id = int(request_id)
        self.amount.default = str(int(suggested))

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_review_edit_approve(
            interaction, self.request_id, str(self.amount.value), str(self.note.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("承認Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class RejectReasonModal(discord.ui.Modal, title="却下の理由を入力"):
    """却下理由は必須。利用者へそのまま DM で伝わる。"""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="却下の理由 (利用者へ通知されます)",
        style=discord.TextStyle.paragraph,
        placeholder="例: 入金が確認できませんでした",
        required=True,
        max_length=400,
    )

    def __init__(self, request_id: int) -> None:
        super().__init__(timeout=600)
        self.request_id = int(request_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_review_reject(
            interaction, self.request_id, str(self.reason.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("却下Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class LinkModal(discord.ui.Modal, title="ステップ2 / 3 ・ リンクの送信"):
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
        await bot_of(interaction).handle_link_submit(interaction, self._tx_id, raw)

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
        await bot_of(interaction).handle_kyash_login(interaction, email, password)

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
        await bot_of(interaction).handle_kyash_otp(interaction, code)

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
        await bot_of(interaction).on_charge_button(interaction)

    @discord.ui.button(
        label="残高", emoji="💳", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_BALANCE, row=0,
    )
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_balance_button(interaction)

    @discord.ui.button(
        label="履歴", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HISTORY, row=1,
    )
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_history_button(interaction)

    @discord.ui.button(
        label="ヘルプ", emoji="❓", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_HELP, row=1,
    )
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_help_button(interaction)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.CHARGE_REFRESH, row=1,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_panel_refresh_button(interaction)


class RankingPanelView(discord.ui.View):
    """ランキングパネル (Persistent View)。チャージパネルとは完全に独立。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_REFRESH, row=0,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_ranking_refresh_button(interaction)

    @discord.ui.button(
        label="自分の順位", emoji="📜", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.RANKING_MYRANK, row=0,
    )
    async def my_rank(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_my_rank_button(interaction)


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
        await bot_of(interaction).handle_charge_cancel(interaction, self._tx_id)
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
        await bot_of(interaction).render_history(interaction, self, edit=True)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        await bot_of(interaction).render_history(interaction, self, edit=True)


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
        await bot_of(interaction).on_shop_open_button(interaction)

    @discord.ui.button(
        label="購入履歴", emoji="📦", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.SHOP_MYITEMS, row=0,
    )
    async def my_items(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_shop_myitems_button(interaction)


class ShopSelect(discord.ui.Select["ShopSelectView"]):
    """商品の選択メニュー (在庫や価格が変わるため都度生成する)。"""

    def __init__(self, items: Sequence[Any]) -> None:
        options: list[discord.SelectOption] = []
        for item in list(items)[:25]:
            duration = int(item["duration_days"])
            stock = int(item["stock"])
            details = [f"{utils.fmt_int(int(item['price']))}"]
            details.append(f"{duration}日間" if duration > 0 else "無期限")
            if stock >= 0:
                details.append(f"残り{stock}個")
            options.append(
                discord.SelectOption(
                    label=utils.truncate(str(item["name"]), 90),
                    value=str(int(item["id"])),
                    description=utils.truncate(" / ".join(details), 90),
                    emoji="🎫",
                )
            )
        # 都度生成する一時 View なので custom_id は固定しない (永続 View ではない)
        super().__init__(
            placeholder="購入する商品を選んでください" if options else "購入できる商品がありません",
            min_values=1,
            max_values=1,
            options=options or [
                discord.SelectOption(label="購入できる商品がありません", value="none")
            ],
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        value = self.values[0]
        if value == "none":
            await safe_respond(
                interaction, embed=info_embed("商品がありません", "現在購入できる商品はありません。")
            )
            return
        await bot_of(interaction).on_shop_select(interaction, int(value))


class ShopSelectView(discord.ui.View):
    """商品選択 (Ephemeral)。"""

    def __init__(self, items: Sequence[Any], *, owner_id: int) -> None:
        super().__init__(timeout=180)
        self._owner_id = owner_id
        self.add_item(ShopSelect(items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True


class InvitePanelView(discord.ui.View):
    """招待キャンペーンのパネル (Persistent View)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="招待リンクを取得", emoji="🔗", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.INVITE_GET, row=0,
    )
    async def get_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_invite_get_button(interaction)

    @discord.ui.button(
        label="自分の招待状況", emoji="📊", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.INVITE_STATUS, row=0,
    )
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_invite_status_button(interaction)

    @discord.ui.button(
        label="招待ランキング", emoji="🏆", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.INVITE_RANK, row=0,
    )
    async def ranking(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_invite_rank_button(interaction)


class AdminPanelView(discord.ui.View):
    """管理者ダッシュボード (Persistent View)。押下時に毎回権限を確認する。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="更新", emoji="🔄", style=discord.ButtonStyle.primary,
        custom_id=config.CustomID.ADMIN_REFRESH, row=0,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_admin_refresh_button(interaction)

    @discord.ui.button(
        label="メンテ切替", emoji="🛠", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.ADMIN_MAINTENANCE, row=0,
    )
    async def maintenance(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await bot_of(interaction).on_admin_maintenance_button(interaction)

    @discord.ui.button(
        label="キュー", emoji="🗃", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.ADMIN_QUEUE, row=1,
    )
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_admin_queue_button(interaction)

    @discord.ui.button(
        label="要確認", emoji="🟠", style=discord.ButtonStyle.danger,
        custom_id=config.CustomID.ADMIN_REVIEW, row=1,
    )
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_admin_review_button(interaction)
