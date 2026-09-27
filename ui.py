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
    item_type: str = config.ShopItemType.ROLE,
    subscription: bool = False,
    renewal_count: int = 0,
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
    embed.add_field(
        name="商品",
        value=f"**{item_name}**" + ("  🔁" if subscription else ""),
        inline=True,
    )
    # ロール販売以外では role_id を使わないため、種類を表示する
    if item_type == config.ShopItemType.ROLE:
        embed.add_field(name="ロール", value=f"<@&{role_id}>", inline=True)
    else:
        embed.add_field(
            name="種類",
            value=f"{config.SHOP_ITEM_TYPE_EMOJI.get(item_type, '🎫')} "
                  f"{config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)}",
            inline=True,
        )
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
    if renewal_count:
        embed.add_field(name="自動更新", value=f"{renewal_count}回目", inline=True)
    embed.set_footer(text="内部残高システム")
    return embed


def tier_achievement_embed(
    *, user_mention: str, tier: Any, total_charged: int, timestamp: int
) -> discord.Embed:
    """段位到達の実績 (実績チャンネルへ投稿)。"""
    embed = discord.Embed(
        title="🎖 段位に到達しました",
        description=SEPARATOR,
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="利用者", value=user_mention, inline=False)
    embed.add_field(name="段位", value=f"**{tier['name']}**", inline=True)
    embed.add_field(name="ロール", value=f"<@&{int(tier['role_id'])}>", inline=True)
    embed.add_field(
        name="累計チャージ", value=f"**{utils.fmt_yen(total_charged)}**", inline=True
    )
    if tier["description"]:
        embed.add_field(
            name="特典", value=utils.truncate(str(tier["description"]), 900), inline=False
        )
    embed.add_field(name="日時", value=utils.discord_ts(timestamp), inline=False)
    embed.set_footer(text="累計チャージ額に応じて自動で昇格します")
    return embed


def tier_dm_embed(*, guild_name: str, tier: Any, total_charged: int) -> discord.Embed:
    """段位到達を本人へ知らせる DM。"""
    embed = discord.Embed(
        title=f"🎖 「{utils.truncate(str(tier['name']), 60)}」に昇格しました",
        description=f"{SEPARATOR}\n**{guild_name}**\n{SEPARATOR}",
        color=config.Color.SUCCESS,
    )
    embed.add_field(
        name="累計チャージ", value=f"**{utils.fmt_yen(total_charged)}**", inline=True
    )
    embed.add_field(name="ロール", value=f"<@&{int(tier['role_id'])}>", inline=True)
    if tier["description"]:
        embed.add_field(
            name="特典", value=utils.truncate(str(tier["description"]), 900), inline=False
        )
    embed.add_field(
        name="▶ これからどうなりますか？",
        value=(
            "ロールが自動で付きました。**降格はありません**。\n"
            "さらにチャージを続けると上位の段位に進めます。\n"
            "`❓ ヘルプ` から現在の段位と次の段位を確認できます。"
        ),
        inline=False,
    )
    return embed


def tier_list_embed(
    tiers: Sequence[Any], *, guild_name: str, rates: dict[int, str] | None = None
) -> discord.Embed:
    """段位の一覧 (管理者向け)。"""
    embed = discord.Embed(
        title="🎖 段位の設定",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + ("累計チャージ額がしきい値に達すると、ロールを自動で付与します。\n"
               "降格はありません。" if tiers else "まだ設定されていません。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    for tier in tiers:
        lines = [
            f"累計 **{utils.fmt_yen(int(tier['threshold']))}** 以上",
            f"ロール: <@&{int(tier['role_id'])}>",
        ]
        rate = (rates or {}).get(int(tier["role_id"]))
        lines.append(
            f"チャージ率: **{rate}**" if rate
            else "チャージ率: 未設定 (`/rate set` で設定できます)"
        )
        if tier["description"]:
            lines.append(utils.truncate(str(tier["description"]), 200))
        embed.add_field(name=f"#{int(tier['id'])} {tier['name']}",
                        value="\n".join(lines), inline=False)
    if tiers:
        embed.set_footer(
            text="段位のロールに /rate set でチャージ率を設定すると優遇できます"
        )
    return embed


def tier_progress_field(
    tiers: Sequence[Any], granted: set[int], total_charged: int
) -> tuple[str, str] | None:
    """利用者向けの段位表示 (残高・ヘルプに差し込む)。"""
    if not tiers:
        return None
    current = None
    next_tier = None
    for tier in tiers:
        if int(tier["id"]) in granted or total_charged >= int(tier["threshold"]):
            current = tier
        elif next_tier is None:
            next_tier = tier
    lines = [
        f"いまの段位: **{current['name'] if current is not None else 'なし'}**",
        f"累計チャージ: **{utils.fmt_yen(total_charged)}**",
    ]
    if next_tier is not None:
        need = int(next_tier["threshold"]) - total_charged
        lines.append(
            f"次は **{next_tier['name']}** まであと **{utils.fmt_yen(max(0, need))}**\n"
            f"{utils.progress_bar(total_charged, int(next_tier['threshold']))}"
        )
    else:
        lines.append("**最高段位に到達しています**")
    return "🎖 段位", "\n".join(lines)


def ranking_reward_embed(*, guild_name: str, result: dict[str, Any]) -> discord.Embed:
    """ランキング報酬の配布結果 (実績チャンネルへ投稿)。"""
    period = str(result.get("period", ""))
    label = config.RANKING_PERIOD_LABELS.get(period, period)
    embed = discord.Embed(
        title=f"🏆 {label}ランキング 報酬配布",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"対象期間: {result.get('period_key', '-')}\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for entry in list(result.get("entries", []))[:15]:
        rank = int(entry["rank"])
        parts = [f"{medals.get(rank, f'{rank}位')} <@{int(entry['user_id'])}>"]
        if int(entry.get("amount") or 0) > 0:
            parts.append(f"**{utils.fmt_int(int(entry['amount']))}**")
        if entry.get("role_id"):
            parts.append(f"<@&{int(entry['role_id'])}>")
        parts.append(f"(獲得 {utils.fmt_int(int(entry['score']))})")
        lines.append(" / ".join(parts))
    embed.add_field(
        name="受賞者", value="\n".join(lines) or "対象者がいませんでした", inline=False
    )
    if result.get("start") and result.get("end"):
        embed.add_field(
            name="集計範囲",
            value=f"{utils.format_jst(int(result['start']))} 〜 "
                  f"{utils.format_jst(int(result['end']))}",
            inline=False,
        )
    embed.set_footer(text="期間中にチャージで獲得した残高で集計しています")
    return embed


def ranking_reward_list_embed(
    rewards: Sequence[Any], *, guild_name: str, grants: Sequence[Any] = ()
) -> discord.Embed:
    """ランキング報酬の設定一覧 (管理者向け)。"""
    embed = discord.Embed(
        title="🏆 ランキング報酬の設定",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + ("締めた期間の上位へ自動で配布します。\n"
               "週間は毎週月曜 00:00 (JST)、月間は毎月1日 00:00 (JST) 区切りです。"
               if rewards else "まだ設定されていません。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    by_type: dict[str, list[Any]] = {}
    for reward in rewards:
        by_type.setdefault(str(reward["ranking_type"]), []).append(reward)
    for period, items in by_type.items():
        lines = []
        for reward in items:
            rank_from = int(reward["rank_from"])
            rank_to = int(reward["rank_to"])
            rank_text = f"{rank_from}位" if rank_from == rank_to else f"{rank_from}〜{rank_to}位"
            parts = [f"**{rank_text}**"]
            if int(reward["amount"]) > 0:
                parts.append(utils.fmt_int(int(reward["amount"])))
            if reward["role_id"]:
                parts.append(f"<@&{int(reward['role_id'])}>")
            lines.append(f"#{int(reward['id'])} " + " / ".join(parts))
        embed.add_field(
            name=config.RANKING_PERIOD_LABELS.get(period, period),
            value="\n".join(lines),
            inline=False,
        )
    if grants:
        lines = [
            f"{config.RANKING_PERIOD_LABELS.get(str(g['ranking_type']), g['ranking_type'])} "
            f"{g['period_key']} {int(g['rank'])}位 <@{int(g['user_id'])}> "
            f"{utils.fmt_int(int(g['amount']))}"
            for g in list(grants)[:10]
        ]
        embed.add_field(name="直近の配布", value="\n".join(lines), inline=False)
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


def copy_block(value: str, *, hint: str | None = None) -> str:
    """値をコピーしやすい形で返す。

    Discord のコードブロックは PC ではホバーでコピーボタンが出て、
    スマートフォンでは長押しで選択できる。ブロック内に値だけを置くことで、
    余分な文字が混ざらないようにする。
    """
    text = f"```\n{value}\n```"
    if hint:
        text += hint
    return text


#: コピー方法の案内 (端末によって操作が違うため両方書く)
COPY_HINT: Final[str] = "📋 PC はブロック右上のボタン、スマホは長押しでコピーできます。"


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
    has_subscription = False
    has_input = False
    for item in list(items)[:10]:
        item_type = str(item["item_type"] or config.ShopItemType.ROLE)
        stock = int(item["stock"])
        limit = int(item["purchase_limit"])
        subscription = bool(int(item["subscription"] or 0)) and int(item["duration_days"]) > 0
        has_subscription = has_subscription or subscription
        has_input = has_input or item_type in config.SHOP_TYPES_NEED_INPUT
        lines = [
            f"💰 **{utils.fmt_int(int(item['price']))}**",
            f"⏳ {shop_item_period_text(item)}",
        ]
        if stock >= 0:
            lines.append(f"📦 残り **{stock}** 個" if stock else "📦 **在庫切れ**")
        if limit > 0:
            lines.append(f"🔒 1人 {limit} 回まで")
        detail = "　".join(lines) + f"\n🎁 {shop_item_reward_text(item)}"
        if item["description"]:
            detail += f"\n{utils.truncate(str(item['description']), 180)}"
        embed.add_field(
            name=f"{config.SHOP_ITEM_TYPE_EMOJI.get(item_type, '🎫')} {item['name']}"
                 + ("  🔁 自動更新" if subscription else ""),
            value=detail,
            inline=False,
        )
    embed.add_field(
        name="▶ 購入のしかた",
        value=(
            "**1.** `🛒 ショップを開く` を押す\n"
            "**2.** 商品を選ぶ (残高も表示されます)\n"
            + ("**3.** 名前などを入力する (必要な商品のみ)\n**4.** " if has_input else "**3.** ")
            + "内容を確認して `購入する` を押す\n"
            "→ 残高が引かれ、特典がすぐに反映されます"
        ),
        inline=False,
    )
    if has_subscription:
        embed.add_field(
            name="🔁 自動更新つきの商品について",
            value=(
                "期限が来ると**残高から自動で同じ金額を引き落とし**、期間を延長します。\n"
                "更新の1日前に DM でお知らせします。\n"
                "止めたいときは `/shop cancel purchase_id:<購入ID>` を実行してください "
                "(期限までは使えます)。\n"
                "残高が足りない場合は自動で終了し、特典は取り消されます。"
            ),
            inline=False,
        )
    embed.set_footer(text="購入後のキャンセルは管理者へご相談ください")
    return embed


def purchase_success_embed(
    *, item_name: str, price: int, balance_after: int,
    expires_at: int | None, purchase_id: int,
    role_id: int | None = None, channel_id: int | None = None,
    item_type: str = config.ShopItemType.ROLE, detail: str | None = None,
    subscription: bool = False, next_charge_at: int | None = None,
) -> discord.Embed:
    """購入完了の案内 (商品タイプごとに「何が起きたか」を書き分ける)。"""
    if role_id:
        granted = f"<@&{role_id}> を付与しました。"
    elif channel_id:
        granted = f"専用チャンネル <#{channel_id}> を作成しました。"
    elif item_type == config.ShopItemType.NICKNAME:
        granted = f"ニックネームを **{detail}** に変更しました。"
    elif item_type == config.ShopItemType.RATE_BOOST:
        granted = f"チャージ率ブースト **{detail}** を適用しました。"
    else:
        granted = f"{detail or '特典'} を適用しました。"
    embed = discord.Embed(
        title="✅ 購入が完了しました",
        description=(
            f"{SEPARATOR}\n{granted}\n"
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
    if subscription and next_charge_at:
        embed.add_field(
            name="🔁 次回の自動更新",
            value=(
                f"{utils.discord_ts(next_charge_at)} に **{utils.fmt_int(price)}** を"
                "残高から引き落とします。\n"
                f"止めるときは `/shop cancel purchase_id:{purchase_id}`"
            ),
            inline=False,
        )
    elif expires_at:
        embed.add_field(
            name="ご注意",
            value="有効期限が切れると**特典は自動で取り消されます**。",
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
    item_type = str(item["item_type"] or config.ShopItemType.ROLE)
    subscription = bool(int(item["subscription"] or 0)) and duration > 0

    blocker: str | None = None
    if duration == 0 and already_has_role and item_type in config.SHOP_TYPES_UNIQUE:
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

    if blocker is None and item_type in config.SHOP_TYPES_NEED_INPUT:
        action_text = (
            f"下の `購入する` を押すと**{config.SHOP_INPUT_LABELS.get(item_type, '内容')}の"
            "入力欄**が開きます。入力を送ると残高から引き落とします。\n"
        )
    elif blocker is None:
        action_text = "下の `購入する` を押すと**すぐに残高から引き落とし**、特典が反映されます。\n"
    else:
        action_text = f"{blocker}\n"
    embed = discord.Embed(
        title="🛒 購入の確認" if blocker is None else "⚠️ いま購入できません",
        description=f"{SEPARATOR}\n{action_text}{SEPARATOR}",
        color=config.Color.ACCENT if blocker is None else config.Color.WARNING,
    )
    embed.add_field(name="商品", value=f"**{item['name']}**", inline=True)
    embed.add_field(
        name="種類",
        value=f"{config.SHOP_ITEM_TYPE_EMOJI.get(item_type, '🎫')} "
              f"{config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)}",
        inline=True,
    )
    embed.add_field(name="価格", value=f"**{utils.fmt_int(price)}**", inline=True)
    embed.add_field(name="もらえるもの", value=shop_item_reward_text(item), inline=False)
    embed.add_field(
        name="有効期間",
        value=(
            f"**{duration}日ごとに自動更新**\n"
            f"期限のたびに **{utils.fmt_int(price)}** を残高から引き落とします。"
            if subscription else
            f"**{duration}日間** (期限が来ると自動で取り消されます)" if duration > 0
            else "**無期限**"
        ),
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
    if subscription and blocker is None:
        embed.add_field(
            name="🔁 自動更新の注意",
            value=(
                "更新の1日前に DM でお知らせします。\n"
                "`/shop cancel purchase_id:<購入ID>` でいつでも停止できます (期限までは使えます)。\n"
                "更新時に残高が足りない場合は自動で終了し、特典は取り消されます。"
            ),
            inline=False,
        )
    embed.set_footer(
        text="購入後の返金は管理者の操作が必要です" if blocker is None
        else "条件を満たすと購入できるようになります"
    )
    return embed, blocker


def shop_item_reward_text(item: Any) -> str:
    """商品を買うと何が手に入るかを1行で説明する。

    商品タイプごとに表示を切り替える。ロール販売以外では ``role_id`` を
    使わないため、ロールのメンションを出さない。
    """
    item_type = str(item["item_type"] or config.ShopItemType.ROLE)
    payload = utils.load_json_dict(item["payload"])
    if item_type == config.ShopItemType.ROLE:
        return f"<@&{int(item['role_id'])}> を付与"
    if item_type == config.ShopItemType.CUSTOM_ROLE:
        color = payload.get("color")
        return "好きな名前のロールを作成" + (f" (色は #{color} に固定)" if color else " (色も指定可)")
    if item_type == config.ShopItemType.NICKNAME:
        return "ニックネームを変更"
    if item_type == config.ShopItemType.RATE_BOOST:
        bonus = payload.get("bonus_rate")
        hours = payload.get("hours")
        return f"チャージ率 **+{utils.fmt_rate(bonus)}** を **{hours}時間**"
    if item_type == config.ShopItemType.PRIVATE_CHANNEL:
        return "自分だけの専用チャンネルを作成"
    return config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)


def shop_item_period_text(item: Any) -> str:
    """有効期間の表示 (サブスクかどうかも含める)。"""
    duration = int(item["duration_days"])
    if duration <= 0:
        return "無期限 (買い切り)"
    if int(item["subscription"] or 0):
        return f"{duration}日ごとに**自動更新** (残高から自動で引き落とし)"
    return f"{duration}日間"


def my_items_embed(rows: Sequence[Any]) -> discord.Embed:
    """所持ロール (購入履歴) の表示。"""
    embed = discord.Embed(
        title="📦 購入履歴",
        description=SEPARATOR if rows else f"{SEPARATOR}\n購入履歴はありません。",
        color=config.Color.INFO,
    )
    for row in list(rows)[:10]:
        item_type = str(row["item_type"] or config.ShopItemType.ROLE)
        emoji = config.SHOP_ITEM_TYPE_EMOJI.get(item_type, "🎫")
        subscription = bool(row["subscription"])
        lines = [
            f"種類: {emoji} {config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)}",
        ]
        if item_type == config.ShopItemType.ROLE:
            lines.append(f"ロール: <@&{int(row['role_id'])}>")
        lines.append(f"価格: {utils.fmt_int(int(row['price']))}")
        lines.append(f"購入: {utils.discord_ts(int(row['created_at']))}")
        if row["expires_at"]:
            lines.append(
                f"期限: {utils.discord_ts(row['expires_at'])} "
                f"({utils.discord_ts(row['expires_at'], 'R')})"
            )
        else:
            lines.append("期限: 無期限")
        if subscription and row["next_charge_at"]:
            lines.append(
                f"🔁 次回更新: {utils.discord_ts(row['next_charge_at'])} — "
                f"**{utils.fmt_int(int(row['price']))}** を自動で引き落とし"
            )
        elif int(row["renewal_count"] or 0):
            lines.append(f"更新回数: {int(row['renewal_count'])}回 (自動更新は停止中)")
        lines.append(f"購入ID: `{row['id']}`")
        embed.add_field(
            name=f"{config.PURCHASE_STATUS_LABELS.get(str(row['status']), str(row['status']))} "
                 f"{row['item_name']}" + ("  🔁" if subscription else ""),
            value="\n".join(lines),
            inline=False,
        )
    if any(bool(r["subscription"]) for r in rows):
        embed.set_footer(text="自動更新を止めるには /shop cancel purchase_id:<購入ID>")
    return embed


def subscription_notice_embed(
    *, item_name: str, price: int, next_charge_at: int, balance: int, purchase_id: int
) -> discord.Embed:
    """自動更新の予告 DM (引き落としの前に必ず知らせる)。"""
    enough = balance >= price
    embed = discord.Embed(
        title="🔁 自動更新のお知らせ",
        description=(
            f"{SEPARATOR}\n**{item_name}** の自動更新が近づいています。\n"
            f"{utils.discord_ts(next_charge_at)} "
            f"({utils.discord_ts(next_charge_at, 'R')}) に "
            f"**{utils.fmt_int(price)}** を残高から引き落とします。\n{SEPARATOR}"
        ),
        color=config.Color.INFO if enough else config.Color.WARNING,
    )
    embed.add_field(name="いまの残高", value=f"**{utils.fmt_int(balance)}**", inline=True)
    embed.add_field(name="更新料", value=f"**{utils.fmt_int(price)}**", inline=True)
    embed.add_field(
        name="判定",
        value=("🟢 残高は足りています" if enough
               else f"🔴 **{utils.fmt_int(price - balance)}** 不足しています"),
        inline=True,
    )
    if not enough:
        embed.add_field(
            name="このままだと",
            value=(
                "更新できず、この商品の特典は**自動で取り消されます**。\n"
                "続けたい場合は更新日までにチャージしてください。"
            ),
            inline=False,
        )
    embed.add_field(
        name="自動更新を止めたいとき",
        value=f"`/shop cancel purchase_id:{purchase_id}` (期限までは使えます)",
        inline=False,
    )
    return embed


def subscription_renewed_embed(
    *, item_name: str, price: int, balance_after: int, expires_at: int,
    renewal_count: int, purchase_id: int,
) -> discord.Embed:
    """自動更新に成功したときの DM。"""
    embed = discord.Embed(
        title="🔁 自動更新しました",
        description=(
            f"{SEPARATOR}\n**{item_name}** を更新しました "
            f"({renewal_count}回目)。\n"
            f"残高が **-{utils.fmt_int(price)}** されました。\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="更新後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(
        name="次回の更新",
        value=f"{utils.discord_ts(expires_at)}\n({utils.discord_ts(expires_at, 'R')})",
        inline=True,
    )
    embed.add_field(name="購入ID", value=f"`{purchase_id}`", inline=True)
    embed.set_footer(text="止めるときは /shop cancel を実行してください")
    return embed


def subscription_stopped_embed(
    *, item_name: str, reason_code: str, price: int, balance: int, purchase_id: int
) -> discord.Embed:
    """自動更新できずに終了したときの DM。"""
    reason = config.SUBSCRIPTION_STOP_REASONS.get(reason_code, reason_code)
    embed = discord.Embed(
        title="⏹ 自動更新を終了しました",
        description=(
            f"{SEPARATOR}\n**{item_name}** の自動更新を終了し、特典を取り消しました。\n"
            f"理由: **{reason}**\n{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    if reason_code == "INSUFFICIENT_BALANCE":
        embed.add_field(name="必要だった額", value=f"**{utils.fmt_int(price)}**", inline=True)
        embed.add_field(name="そのときの残高", value=f"**{utils.fmt_int(balance)}**", inline=True)
        embed.add_field(
            name="また使いたいときは",
            value="チャージして残高を用意し、ショップからもう一度購入してください。",
            inline=False,
        )
    else:
        embed.add_field(
            name="また使いたいときは",
            value="ショップの状況を確認するか、管理者へご相談ください。",
            inline=False,
        )
    embed.add_field(name="購入ID", value=f"`{purchase_id}`", inline=True)
    return embed


def subscription_list_embed(rows: Sequence[Any], *, guild_name: str) -> discord.Embed:
    """自動更新中の購入一覧 (管理者用)。"""
    embed = discord.Embed(
        title="🔁 自動更新中の購入",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + (f"{len(rows)} 件が自動更新の対象です。" if rows else "自動更新中の購入はありません。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    total = 0
    for row in list(rows)[:15]:
        price = int(row["price"])
        total += price
        item_type = str(row["item_type"] or config.ShopItemType.ROLE)
        embed.add_field(
            name=f"#{int(row['id'])} {row['item_name']}",
            value=(
                f"利用者: <@{int(row['user_id'])}>\n"
                f"種類: {config.SHOP_ITEM_TYPE_EMOJI.get(item_type, '🎫')} "
                f"{config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type)}\n"
                f"更新料: {utils.fmt_int(price)}\n"
                + (f"次回: {utils.discord_ts(row['next_charge_at'])}\n"
                   if row["next_charge_at"] else "")
                + f"更新回数: {int(row['renewal_count'] or 0)}回"
            ),
            inline=True,
        )
    if rows:
        embed.add_field(
            name="1周期あたりの合計", value=f"**{utils.fmt_int(total)}**", inline=False
        )
    return embed


# ---------------------------------------------------------------------------
# 返金申請とレシート
# ---------------------------------------------------------------------------

#: 返金で「お金が戻る」わけではないことの説明 (誤解を生まないよう必ず添える)
REFUND_SCOPE_NOTE = (
    "この手続きで戻るのは**サーバー内の残高の取消**です。\n"
    "Kyash へ実際にお金を返す作業は、承認後に管理者が手作業で行います。"
)


def refund_request_embed(
    *, request_id: int, tx_id: str, amount: int, received: int,
    completed_at: int, reason: str,
) -> discord.Embed:
    """申請を受け付けたときに利用者へ返す画面。"""
    embed = discord.Embed(
        title="↩️ 返金の申請を受け付けました",
        description=(
            f"{SEPARATOR}\n審査が終わるまでお待ちください。\n"
            f"結果は DM でお知らせします。\n{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="申請ID", value=f"`{request_id}`", inline=True)
    embed.add_field(name="対象の取引", value=f"`{tx_id}`", inline=True)
    embed.add_field(
        name="送金額 / 取消される残高",
        value=f"{utils.fmt_yen(received)} / **{utils.fmt_int(amount)}**",
        inline=True,
    )
    embed.add_field(
        name="チャージ日時", value=utils.discord_ts(completed_at), inline=True
    )
    embed.add_field(name="理由", value=utils.truncate(reason, 500) or "-", inline=False)
    embed.add_field(name="ご注意", value=REFUND_SCOPE_NOTE, inline=False)
    embed.add_field(
        name="取り下げたいとき",
        value=f"`/refund cancel request_id:{request_id}` (審査前のみ)",
        inline=False,
    )
    return embed


def refund_card_embed(
    request: Any, transaction: Any = None, *, guild_name: str,
    past_requests: int = 0,
) -> discord.Embed:
    """返金申請の審査カード。

    承認すると残高が減るため、判断に必要な材料 (取引の内容・過去の申請数) を
    1枚にまとめる。
    """
    status = str(request["status"])
    color = {
        config.RefundRequestStatus.PENDING: config.Color.WARNING,
        config.RefundRequestStatus.APPROVED: config.Color.SUCCESS,
        config.RefundRequestStatus.REJECTED: config.Color.DANGER,
        config.RefundRequestStatus.CANCELLED: config.Color.NEUTRAL,
    }.get(status, config.Color.NEUTRAL)
    embed = discord.Embed(
        title="↩️ 返金申請の審査",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"{config.REFUND_STATUS_LABELS.get(status, status)}\n{SEPARATOR}"
        ),
        color=color,
    )
    embed.add_field(name="申請ID", value=f"`{int(request['id'])}`", inline=True)
    embed.add_field(name="利用者", value=f"<@{int(request['user_id'])}>", inline=True)
    embed.add_field(
        name="申請日時", value=utils.discord_ts(int(request["created_at"])), inline=True
    )
    embed.add_field(
        name="対象の取引", value=f"`{request['transaction_id']}`", inline=True
    )
    embed.add_field(
        name="取消される残高",
        value=f"**{utils.fmt_int(int(request['amount'] or 0))}**",
        inline=True,
    )
    if transaction is not None:
        embed.add_field(
            name="送金額 / 方式",
            value=(
                f"{utils.fmt_yen(int(transaction['received_amount'] or 0))} / "
                f"{config.PROVIDER_LABELS.get(str(transaction['provider']), str(transaction['provider']))}"
            ),
            inline=True,
        )
        embed.add_field(
            name="チャージ日時",
            value=utils.discord_ts(
                int(transaction["completed_at"] or transaction["created_at"] or 0)
            ),
            inline=True,
        )
    if past_requests > 1:
        embed.add_field(
            name="この利用者の申請",
            value=f"過去を含めて **{past_requests}** 件",
            inline=True,
        )
    embed.add_field(
        name="申請理由",
        value=utils.truncate(str(request["reason"] or "-"), 900),
        inline=False,
    )
    if status == config.RefundRequestStatus.PENDING:
        embed.add_field(
            name="▶ 承認すると何が起きますか？",
            value=(
                "・対象のチャージを**取消**し、付与した残高を回収します\n"
                "・残高が足りない場合は 0 までしか戻せません (マイナスにはしません)\n"
                f"・{REFUND_SCOPE_NOTE}"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="処理",
            value=(
                f"担当: <@{int(request['reviewed_by'])}>\n"
                f"日時: {utils.discord_ts(int(request['reviewed_at']))}"
                if request["reviewed_by"] and request["reviewed_at"] else "-"
            ),
            inline=True,
        )
        if request["reject_reason"]:
            embed.add_field(
                name="却下の理由",
                value=utils.truncate(str(request["reject_reason"]), 500),
                inline=False,
            )
    return embed


def refund_result_dm_embed(
    *, guild_name: str, request_id: int, tx_id: str, approved: bool,
    amount: int, balance_after: int, reason: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ 返金申請が承認されました" if approved else "🔴 返金申請は承認されませんでした",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + (
                f"取引 `{tx_id}` を取り消し、残高 **{utils.fmt_int(amount)}** を"
                "回収しました。\n"
                if approved else
                f"取引 `{tx_id}` の返金は承認されませんでした。\n"
            )
            + SEPARATOR
        ),
        color=config.Color.SUCCESS if approved else config.Color.DANGER,
    )
    embed.add_field(name="申請ID", value=f"`{request_id}`", inline=True)
    embed.add_field(name="いまの残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    if approved:
        embed.add_field(name="この先", value=REFUND_SCOPE_NOTE, inline=False)
    elif reason:
        embed.add_field(name="理由", value=utils.truncate(reason, 500), inline=False)
    embed.add_field(
        name="ご不明な点があれば",
        value="サーバーの管理者へお問い合わせください。",
        inline=False,
    )
    return embed


def refund_list_embed(
    rows: Sequence[Any], *, title: str, total: int, page: int, total_pages: int
) -> discord.Embed:
    embed = discord.Embed(
        title=f"↩️ {title}",
        description=(
            f"{SEPARATOR}\n該当 **{utils.fmt_int(total)}** 件"
            f"　ページ {page}/{total_pages}\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    if not rows:
        embed.add_field(name="該当なし", value="返金申請はありません。", inline=False)
        return embed
    for row in rows:
        status = str(row["status"])
        embed.add_field(
            name=f"`{int(row['id'])}` "
                 f"{config.REFUND_STATUS_LABELS.get(status, status)}",
            value=(
                f"利用者: <@{int(row['user_id'])}>\n"
                f"取引: `{row['transaction_id']}`\n"
                f"取消額: {utils.fmt_int(int(row['amount'] or 0))}\n"
                f"申請: {utils.discord_ts(int(row['created_at']))}\n"
                f"理由: {utils.truncate(str(row['reason'] or '-'), 120)}"
            ),
            inline=False,
        )
    return embed


def receipt_embed(
    *, code: str, tx_id: str, received: int, credited: int, completed_at: int,
    provider: str, refunded: bool, guild_name: str,
) -> discord.Embed:
    """チャージの控え (本人向け)。"""
    embed = discord.Embed(
        title="🧾 チャージの控え",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + ("⚠️ この取引は**取消済み**です。\n" if refunded else "")
            + "下のコードは改ざんできない署名つきです。\n"
            "`/receipt verify` で誰でも内容を確認できます。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.NEUTRAL if refunded else config.Color.SUCCESS,
    )
    embed.add_field(name="取引ID", value=f"`{tx_id}`", inline=True)
    embed.add_field(
        name="方式",
        value=config.PROVIDER_LABELS.get(provider, provider),
        inline=True,
    )
    embed.add_field(name="チャージ日時", value=utils.discord_ts(completed_at), inline=True)
    embed.add_field(name="送金額", value=f"**{utils.fmt_yen(received)}**", inline=True)
    embed.add_field(name="付与された残高", value=f"**{utils.fmt_int(credited)}**", inline=True)
    embed.add_field(
        name="控えのコード",
        value=copy_block(code, hint="長押し (PCは選択) でコピーできます"),
        inline=False,
    )
    embed.set_footer(text="控えのコードは他人に見せても残高を操作されることはありません")
    return embed


def receipt_verify_embed(result: dict[str, Any], *, guild_name: str) -> discord.Embed:
    """控えの検証結果。"""
    reason = str(result.get("reason") or "")
    payload = result.get("payload") or {}
    if result.get("valid"):
        embed = discord.Embed(
            title="🧾 控えは有効です",
            description=(
                f"{SEPARATOR}\n署名が一致し、いまの記録とも一致しました。\n{SEPARATOR}"
            ),
            color=config.Color.SUCCESS,
        )
    else:
        messages = {
            "SIGNATURE": "署名が一致しません。コードが壊れているか、このサーバーで発行されたものではありません。",
            "NOT_FOUND": "署名は正しいものの、記録が見つかりません。",
            "MISMATCH": "署名は正しいものの、いまの記録と内容が一致しません。",
            "REFUNDED": "この取引は**取消済み**です。控えとしては無効です。",
            "NOT_COMPLETED": "この取引はまだ完了していません。",
        }
        embed = discord.Embed(
            title="⚠️ 控えを確認できませんでした",
            description=(
                f"{SEPARATOR}\n"
                f"{messages.get(reason, '確認できませんでした。')}\n{SEPARATOR}"
            ),
            color=config.Color.DANGER,
        )
    if payload:
        embed.add_field(name="取引ID", value=f"`{payload.get('tx_id')}`", inline=True)
        embed.add_field(
            name="利用者", value=f"<@{int(payload.get('user_id', 0))}>", inline=True
        )
        embed.add_field(
            name="チャージ日時",
            value=utils.discord_ts(int(payload.get("completed_at", 0))),
            inline=True,
        )
        embed.add_field(
            name="送金額", value=utils.fmt_yen(int(payload.get("received", 0))), inline=True
        )
        embed.add_field(
            name="付与された残高",
            value=utils.fmt_int(int(payload.get("credited", 0))),
            inline=True,
        )
        embed.add_field(name="サーバー", value=guild_name, inline=True)
    embed.set_footer(text="控えの内容は発行時点の記録です")
    return embed


class RefundRejectModal(discord.ui.Modal):
    """返金申請を却下するときの理由入力。"""

    def __init__(self, request_id: int) -> None:
        super().__init__(title="返金申請を却下する", timeout=300)
        self.request_id = request_id
        self.reason: discord.ui.TextInput = discord.ui.TextInput(
            label="却下の理由 (利用者へ通知されます)",
            placeholder="例: 利用規約に基づき返金の対象外です",
            required=True,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_refund_reject(
            interaction, self.request_id, str(self.reason.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("返金却下Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class RefundCardView(discord.ui.View):
    """返金申請の審査カード (Persistent View)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="承認して取消", emoji="✅", style=discord.ButtonStyle.danger,
        custom_id=config.CustomID.REFUND_APPROVE,
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_refund_approve(interaction)

    @discord.ui.button(
        label="却下", emoji="🔴", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.REFUND_REJECT,
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_refund_reject(interaction)


# ---------------------------------------------------------------------------
# 不正検知
# ---------------------------------------------------------------------------

def fraud_card_embed(flag: Any, *, guild_name: str) -> discord.Embed:
    """検知カード (審査チャンネルへ投稿し、処理後は書き換える)。

    「疑わしい」を知らせるだけで、処分は管理者が決める。断定的な表現は
    使わず、根拠と次の確認先を必ず添える。
    """
    kind = str(flag["kind"])
    severity = str(flag["severity"])
    status = str(flag["status"])
    color = {
        config.FraudSeverity.HIGH: config.Color.DANGER,
        config.FraudSeverity.WARN: config.Color.WARNING,
        config.FraudSeverity.INFO: config.Color.INFO,
    }.get(severity, config.Color.INFO)
    if status != config.FraudStatus.OPEN:
        color = config.Color.NEUTRAL
    embed = discord.Embed(
        title=f"🛡 検知: {config.FRAUD_KIND_LABELS.get(kind, kind)}",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"{config.FRAUD_SEVERITY_LABELS.get(severity, severity)}　"
            f"{config.FRAUD_STATUS_LABELS.get(status, status)}\n"
            f"{SEPARATOR}"
        ),
        color=color,
    )
    embed.add_field(name="対象", value=f"<@{int(flag['user_id'])}>", inline=True)
    embed.add_field(name="検知ID", value=f"`{int(flag['id'])}`", inline=True)
    embed.add_field(
        name="検知時刻", value=utils.discord_ts(int(flag["created_at"])), inline=True
    )
    embed.add_field(
        name="内容", value=utils.truncate(str(flag["detail"] or "-"), 900), inline=False
    )
    evidence = utils.load_json_dict(flag["evidence"])
    if evidence:
        lines = [
            f"{key}: {utils.truncate(str(value), 120)}"
            for key, value in list(evidence.items())[:8]
        ]
        embed.add_field(
            name="根拠", value=utils.truncate("\n".join(lines), 900), inline=False
        )
    if status == config.FraudStatus.OPEN:
        embed.add_field(
            name="▶ 確認のしかた",
            value=(
                f"`/user inspect user:<@{int(flag['user_id'])}>` で詳細を確認\n"
                "問題があれば `/user freeze`、問題なければ `⚪ 問題なし` を押してください。\n"
                "**この検知だけで自動的な処分は行っていません。**"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="処理",
            value=(
                f"担当: <@{int(flag['reviewed_by'])}>\n"
                f"日時: {utils.discord_ts(int(flag['reviewed_at']))}"
                if flag["reviewed_by"] and flag["reviewed_at"] else "-"
            ),
            inline=True,
        )
        if flag["note"]:
            embed.add_field(
                name="メモ", value=utils.truncate(str(flag["note"]), 500), inline=False
            )
    embed.set_footer(text="不正検知は目安です。判断は必ず人が行ってください")
    return embed


def fraud_list_embed(
    rows: Sequence[Any], *, guild_name: str, total: int, page: int, total_pages: int,
    status: str | None,
) -> discord.Embed:
    label = config.FRAUD_STATUS_LABELS.get(status or "", "すべて") if status else "すべて"
    embed = discord.Embed(
        title="🛡 不正検知の一覧",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"絞り込み: {label}　該当 **{utils.fmt_int(total)}** 件"
            f"　ページ {page}/{total_pages}\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    if not rows:
        embed.add_field(
            name="該当なし",
            value="条件に合う検知はありません。",
            inline=False,
        )
        return embed
    for row in rows:
        kind = str(row["kind"])
        embed.add_field(
            name=f"`{int(row['id'])}` "
                 f"{config.FRAUD_SEVERITY_LABELS.get(str(row['severity']), '')} "
                 f"{config.FRAUD_KIND_LABELS.get(kind, kind)}",
            value=(
                f"対象: <@{int(row['user_id'])}>\n"
                f"状態: {config.FRAUD_STATUS_LABELS.get(str(row['status']), str(row['status']))}\n"
                f"検知: {utils.discord_ts(int(row['created_at']))}\n"
                f"{utils.truncate(str(row['detail'] or '-'), 200)}"
            ),
            inline=False,
        )
    embed.set_footer(text="処理は /fraud resolve か /fraud ignore で行えます")
    return embed


class FraudNoteModal(discord.ui.Modal):
    """検知を処理するときのメモ入力。"""

    def __init__(self, flag_id: int, *, status: str) -> None:
        resolved = status == config.FraudStatus.RESOLVED
        super().__init__(
            title="対処済みにする" if resolved else "問題なしにする", timeout=300
        )
        self.flag_id = flag_id
        self.status = status
        self.note: discord.ui.TextInput = discord.ui.TextInput(
            label="メモ (任意・後から見返せます)",
            placeholder="例: 本人確認済み / 凍結して対応済み",
            required=False,
            max_length=400,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_fraud_review(
            interaction, self.flag_id, status=self.status, note=str(self.note.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("検知メモModalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class FraudCardView(discord.ui.View):
    """検知カードのボタン (Persistent View)。

    押下時に毎回権限を確認する。対象はメッセージIDから引く。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="対処済みにする", emoji="✅", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.FRAUD_RESOLVE,
    )
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_fraud_button(
            interaction, status=config.FraudStatus.RESOLVED
        )

    @discord.ui.button(
        label="問題なし", emoji="⚪", style=discord.ButtonStyle.secondary,
        custom_id=config.CustomID.FRAUD_IGNORE,
    )
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_fraud_button(
            interaction, status=config.FraudStatus.IGNORED
        )

    @discord.ui.button(
        label="詳細", emoji="🔎", style=discord.ButtonStyle.primary,
        custom_id=config.CustomID.FRAUD_DETAIL,
    )
    async def detail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_fraud_detail(interaction)


# ---------------------------------------------------------------------------
# サーバー全体のチャージ目標
# ---------------------------------------------------------------------------

def goal_reward_text(goal: Any) -> str:
    """報酬の内容を1行で表す。"""
    amount = int(goal["reward_amount"] or 0)
    role_id = goal["reward_role_id"]
    parts: list[str] = []
    if amount:
        parts.append(f"残高 **{utils.fmt_int(amount)}**")
    if role_id:
        parts.append(f"<@&{int(role_id)}>")
    return " + ".join(parts) if parts else "なし"


def goal_panel_embed(goal: Any, progress: dict[str, int] | None) -> discord.Embed:
    """チャージ目標のパネル (進捗が動いたときに更新する)。"""
    if goal is None:
        return discord.Embed(
            title="🎯 チャージ目標",
            description=(
                f"{SEPARATOR}\n"
                "いま集計中の目標はありません。\n"
                "次の目標が始まるまでお待ちください。\n"
                f"{SEPARATOR}"
            ),
            color=config.Color.NEUTRAL,
        )
    target = int(goal["target_amount"])
    total = int((progress or {}).get("total", 0))
    users = int((progress or {}).get("users", 0))
    count = int((progress or {}).get("count", 0))
    status = str(goal["status"])
    achieved = status == config.GoalStatus.ACHIEVED
    if achieved:
        total = int(goal["achieved_total"] or total)
    remaining = max(0, target - total)
    percent = (total / target * 100) if target > 0 else 0.0
    embed = discord.Embed(
        title=f"🎯 {utils.truncate(str(goal['name']), 200)}",
        description=(
            f"{SEPARATOR}\n"
            f"{config.GOAL_STATUS_LABELS.get(status, status)}\n"
            "サーバー全体のチャージ額を合わせて目標を目指します。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.SUCCESS if achieved else config.Color.ACCENT,
    )
    embed.add_field(
        name="進捗",
        value=(
            f"`{utils.progress_bar(total, target)}` **{percent:.1f}%**\n"
            f"**{utils.fmt_yen(total)}** / {utils.fmt_yen(target)}"
            + ("" if achieved else f"\nあと **{utils.fmt_yen(remaining)}**")
        ),
        inline=False,
    )
    embed.add_field(name="参加人数", value=f"**{users}**人", inline=True)
    embed.add_field(name="チャージ回数", value=f"{count}回", inline=True)
    embed.add_field(name="達成報酬", value=goal_reward_text(goal), inline=True)
    if goal["ends_at"]:
        embed.add_field(
            name="締切",
            value=(
                f"{utils.discord_ts(int(goal['ends_at']))}\n"
                f"({utils.discord_ts(int(goal['ends_at']), 'R')})"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="締切", value="期限なし (達成するまで)", inline=True)
    embed.add_field(
        name="集計の開始",
        value=utils.discord_ts(int(goal["starts_at"])),
        inline=True,
    )
    if achieved:
        embed.add_field(
            name="🎉 達成しました",
            value=(
                f"{utils.discord_ts(int(goal['achieved_at']))} に達成しました。\n"
                "期間中にチャージした全員へ報酬を配布しました。"
            ),
            inline=False,
        )
    elif status == config.GoalStatus.OPEN:
        embed.add_field(
            name="▶ 参加のしかた",
            value=(
                "**期間中にチャージするだけ**で参加になります。\n"
                "達成すると、期間中にチャージした**全員**が報酬を受け取れます。\n"
                "`🔄 最新の進捗` を押すと今の数字を確認できます。"
            ),
            inline=False,
        )
    embed.set_footer(text=f"目標ID: {int(goal['id'])}")
    return embed


def goal_progress_embed(
    goal: Any, progress: dict[str, int], *, contribution: int = 0
) -> discord.Embed:
    """個人向けの進捗表示 (🔄 ボタンの応答)。"""
    embed = goal_panel_embed(goal, progress)
    embed.add_field(
        name="あなたの参加状況",
        value=(
            f"期間中のチャージ: **{utils.fmt_yen(contribution)}**\n"
            + ("🟢 報酬の対象です" if contribution > 0
               else "まだチャージがありません (チャージすると対象になります)")
        ),
        inline=False,
    )
    return embed


def goal_reward_dm_embed(
    *, goal_name: str, guild_name: str, reward_amount: int, role_id: int | None,
    total: int, target: int, contribution: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 チャージ目標の達成報酬",
        description=(
            f"{SEPARATOR}\n**{guild_name}** の目標 **{goal_name}** が達成されました。\n"
            f"期間中にチャージした方へ報酬をお渡しします。\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(
        name="達成額",
        value=f"**{utils.fmt_yen(total)}** / {utils.fmt_yen(target)}",
        inline=True,
    )
    embed.add_field(
        name="あなたの参加額", value=f"**{utils.fmt_yen(contribution)}**", inline=True
    )
    embed.add_field(
        name="受け取った報酬",
        value=(f"残高 **+{utils.fmt_int(reward_amount)}**" if reward_amount else "")
        + (f"\n<@&{role_id}> を付与" if role_id else "")
        or "なし",
        inline=False,
    )
    embed.set_footer(text="ご参加ありがとうございました")
    return embed


def goal_achieved_embed(goal: Any, *, granted: int, guild_name: str) -> discord.Embed:
    """目標達成の実績 (実績チャンネルへ投稿)。"""
    embed = discord.Embed(
        title="🎉 チャージ目標を達成しました",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            f"目標 **{goal['name']}** を達成しました！\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(
        name="到達額",
        value=f"**{utils.fmt_yen(int(goal['achieved_total'] or 0))}** / "
              f"{utils.fmt_yen(int(goal['target_amount']))}",
        inline=True,
    )
    embed.add_field(name="報酬を受け取った人", value=f"**{granted}**人", inline=True)
    embed.add_field(name="報酬", value=goal_reward_text(goal), inline=True)
    if goal["achieved_at"]:
        embed.add_field(
            name="達成", value=utils.discord_ts(int(goal["achieved_at"])), inline=True
        )
    embed.set_footer(text=f"目標ID: {int(goal['id'])}")
    return embed


def goal_list_embed(rows: Sequence[Any], *, guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="🎯 チャージ目標の一覧",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + (f"{len(rows)} 件" if rows else "目標はまだありません。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    for row in list(rows)[:10]:
        status = str(row["status"])
        total = int(row["achieved_total"] or 0)
        target = int(row["target_amount"])
        lines = [
            config.GOAL_STATUS_LABELS.get(status, status),
            f"目標額: {utils.fmt_yen(target)}",
            f"報酬: {goal_reward_text(row)}",
        ]
        if status != config.GoalStatus.OPEN:
            lines.append(
                f"到達: {utils.fmt_yen(total)} "
                f"({(total / target * 100) if target else 0:.1f}%)"
            )
        if row["ends_at"]:
            lines.append(f"締切: {utils.discord_ts(int(row['ends_at']))}")
        embed.add_field(
            name=f"`{int(row['id'])}` {utils.truncate(str(row['name']), 80)}",
            value="\n".join(lines),
            inline=False,
        )
    return embed


class GoalPanelView(discord.ui.View):
    """チャージ目標のパネル (Persistent View)。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="最新の進捗", emoji="🔄", style=discord.ButtonStyle.primary,
        custom_id=config.CustomID.GOAL_REFRESH,
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_goal_refresh_button(interaction)


# ---------------------------------------------------------------------------
# オークション
# ---------------------------------------------------------------------------

def auction_minimum_bid(auction: Any) -> int:
    """次に入札できる最低額。"""
    current = auction["current_bid"]
    if current is None:
        return int(auction["start_price"])
    return int(current) + int(auction["min_increment"])


def auction_panel_embed(
    auction: Any, bids: Sequence[Any] = (), *, counts: tuple[int, int] = (0, 0)
) -> discord.Embed:
    """オークションのパネル (入札ごとに更新する)。"""
    status = str(auction["status"])
    open_now = status == config.AuctionStatus.OPEN
    color = {
        config.AuctionStatus.OPEN: config.Color.ACCENT,
        config.AuctionStatus.CLOSED: config.Color.SUCCESS,
        config.AuctionStatus.CANCELLED: config.Color.WARNING,
        config.AuctionStatus.FAILED: config.Color.NEUTRAL,
    }.get(status, config.Color.NEUTRAL)
    current = auction["current_bid"]
    embed = discord.Embed(
        title=f"🔨 {utils.truncate(str(auction['name']), 200)}",
        description=(
            f"{SEPARATOR}\n"
            f"{config.AUCTION_STATUS_LABELS.get(status, status)}\n"
            + (f"{utils.truncate(str(auction['description']), 500)}\n"
               if auction["description"] else "")
            + SEPARATOR
        ),
        color=color,
    )
    embed.add_field(name="景品", value=f"<@&{int(auction['role_id'])}>", inline=True)
    duration = int(auction["duration_days"] or 0)
    embed.add_field(
        name="ロールの有効期間",
        value=f"**{duration}日間**" if duration else "**無期限**",
        inline=True,
    )
    embed.add_field(
        name="開始価格", value=f"{utils.fmt_int(int(auction['start_price']))}", inline=True
    )
    if open_now:
        embed.add_field(
            name="現在の最高額",
            value=(
                f"**{utils.fmt_int(int(current))}**\n<@{int(auction['current_bidder'])}>"
                if current is not None else "まだ入札はありません"
            ),
            inline=True,
        )
        embed.add_field(
            name="次の最低入札額",
            value=f"**{utils.fmt_int(auction_minimum_bid(auction))}**",
            inline=True,
        )
        embed.add_field(
            name="締切",
            value=(
                f"{utils.discord_ts(int(auction['ends_at']))}\n"
                f"({utils.discord_ts(int(auction['ends_at']), 'R')})"
            ),
            inline=True,
        )
    else:
        winner = auction["winner_id"]
        embed.add_field(
            name="結果",
            value=(
                f"落札者 <@{int(winner)}>\n**{utils.fmt_int(int(auction['winning_bid']))}**"
                if winner and auction["winning_bid"] else
                "入札がないまま終了しました" if status == config.AuctionStatus.FAILED
                else "中止しました (入札は全額返金済み)"
            ),
            inline=True,
        )
        if auction["closed_at"]:
            embed.add_field(
                name="終了", value=utils.discord_ts(int(auction["closed_at"])), inline=True
            )
    bid_count, bidder_count = counts
    if bid_count:
        embed.add_field(
            name="入札状況",
            value=f"{bid_count}件 / {bidder_count}人",
            inline=True,
        )
    if bids:
        lines = [
            f"{i}. <@{int(b['user_id'])}> **{utils.fmt_int(int(b['amount']))}**"
            + ("" if not int(b["refunded"] or 0) else " (返金済み)")
            for i, b in enumerate(list(bids)[:5], start=1)
        ]
        embed.add_field(name="入札の上位", value="\n".join(lines), inline=False)
    if open_now:
        embed.add_field(
            name="▶ 入札のしかた",
            value=(
                "**1.** `💸 入札する` を押す\n"
                "**2.** 入札額を入力する (現在の最低額以上)\n"
                "→ 入札した分は**その場で残高から預かります**\n"
                "→ 他の人に上回られたら**全額すぐに返します** (DMでお知らせ)\n"
                f"→ 締切 {config.AUCTION_ANTI_SNIPE_SECONDS // 60} 分前の入札では"
                "締切が延長されます"
            ),
            inline=False,
        )
        embed.set_footer(text=f"オークションID: {int(auction['id'])}")
    else:
        embed.set_footer(text=f"オークションID: {int(auction['id'])} / 終了しました")
    return embed


def auction_bid_success_embed(
    *, name: str, amount: int, balance_after: int, ends_at: int,
    extended: bool, auction_id: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ 入札しました",
        description=(
            f"{SEPARATOR}\n**{name}** に **{utils.fmt_int(amount)}** で入札しました。\n"
            f"この分は残高から預かっています。\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(name="入札後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(
        name="締切",
        value=utils.discord_ts(ends_at) + ("\n(締切が延長されました)" if extended else ""),
        inline=True,
    )
    embed.add_field(name="オークションID", value=f"`{auction_id}`", inline=True)
    embed.add_field(
        name="この先の流れ",
        value=(
            "・他の人に上回られたら**全額すぐに返金**します (DMでお知らせ)\n"
            "・そのまま締切を迎えたら落札となり、景品のロールが付きます"
        ),
        inline=False,
    )
    return embed


def auction_outbid_dm_embed(
    *, name: str, auction_id: int, your_bid: int, new_bid: int,
    balance_after: int, ends_at: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🔔 入札が上回られました",
        description=(
            f"{SEPARATOR}\n**{name}** であなたの入札 "
            f"({utils.fmt_int(your_bid)}) が上回られました。\n"
            f"預かっていた **{utils.fmt_int(your_bid)}** は全額返金しました。\n{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="現在の最高額", value=f"**{utils.fmt_int(new_bid)}**", inline=True)
    embed.add_field(name="返金後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(
        name="締切",
        value=f"{utils.discord_ts(ends_at)}\n({utils.discord_ts(ends_at, 'R')})",
        inline=True,
    )
    embed.add_field(
        name="続けて入札するには",
        value=f"オークションのパネルから、または `/auction bid auction_id:{auction_id}`",
        inline=False,
    )
    return embed


def auction_won_dm_embed(
    *, name: str, auction_id: int, winning_bid: int, role_id: int,
    role_expires_at: int | None, balance: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🏁 落札しました",
        description=(
            f"{SEPARATOR}\n**{name}** を **{utils.fmt_int(winning_bid)}** で落札しました。\n"
            f"景品の <@&{role_id}> を付与しました。\n{SEPARATOR}"
        ),
        color=config.Color.SUCCESS,
    )
    embed.add_field(
        name="お支払い",
        value=f"入札時に預かった **{utils.fmt_int(winning_bid)}** をそのまま充当しました\n"
              f"(追加の引き落としはありません)",
        inline=False,
    )
    embed.add_field(name="いまの残高", value=f"**{utils.fmt_int(balance)}**", inline=True)
    embed.add_field(
        name="ロールの有効期限",
        value=(
            f"{utils.discord_ts(role_expires_at)}\n"
            f"({utils.discord_ts(role_expires_at, 'R')})"
            if role_expires_at else "無期限"
        ),
        inline=True,
    )
    embed.add_field(name="オークションID", value=f"`{auction_id}`", inline=True)
    return embed


def auction_cancelled_dm_embed(
    *, name: str, auction_id: int, refunded: int, balance_after: int, reason: str
) -> discord.Embed:
    embed = discord.Embed(
        title="⚫ オークションが中止されました",
        description=(
            f"{SEPARATOR}\n**{name}** は中止されました。\n"
            f"預かっていた **{utils.fmt_int(refunded)}** は全額返金しました。\n{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="返金後の残高", value=f"**{utils.fmt_int(balance_after)}**", inline=True)
    embed.add_field(name="オークションID", value=f"`{auction_id}`", inline=True)
    embed.add_field(name="理由", value=utils.truncate(reason, 500) or "-", inline=False)
    return embed


def auction_result_embed(auction: Any, *, counts: tuple[int, int] = (0, 0)) -> discord.Embed:
    """落札結果の実績 (実績チャンネルへ投稿)。"""
    status = str(auction["status"])
    winner = auction["winner_id"]
    embed = discord.Embed(
        title="🔨 オークション結果",
        description=SEPARATOR,
        color=config.Color.SUCCESS if winner else config.Color.NEUTRAL,
    )
    embed.add_field(name="オークション", value=f"**{auction['name']}**", inline=False)
    embed.add_field(name="景品", value=f"<@&{int(auction['role_id'])}>", inline=True)
    embed.add_field(
        name="状態",
        value=config.AUCTION_STATUS_LABELS.get(status, status),
        inline=True,
    )
    if winner and auction["winning_bid"]:
        embed.add_field(name="落札者", value=f"<@{int(winner)}>", inline=True)
        embed.add_field(
            name="落札額", value=f"**{utils.fmt_int(int(auction['winning_bid']))}**", inline=True
        )
    bid_count, bidder_count = counts
    embed.add_field(name="入札", value=f"{bid_count}件 / {bidder_count}人", inline=True)
    if auction["closed_at"]:
        embed.add_field(
            name="終了", value=utils.discord_ts(int(auction["closed_at"])), inline=True
        )
    embed.set_footer(text=f"オークションID: {int(auction['id'])}")
    return embed


def auction_list_embed(rows: Sequence[Any], *, guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="🔨 オークション一覧",
        description=(
            f"{SEPARATOR}\n**{guild_name}**\n"
            + (f"{len(rows)} 件" if rows else "オークションはありません。")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.INFO,
    )
    for row in list(rows)[:10]:
        status = str(row["status"])
        current = row["current_bid"]
        lines = [
            config.AUCTION_STATUS_LABELS.get(status, status),
            f"景品: <@&{int(row['role_id'])}>",
            f"開始価格: {utils.fmt_int(int(row['start_price']))}",
        ]
        if status == config.AuctionStatus.OPEN:
            lines.append(
                f"現在: {utils.fmt_int(int(current))} (<@{int(row['current_bidder'])}>)"
                if current is not None else "現在: 入札なし"
            )
            lines.append(f"締切: {utils.discord_ts(int(row['ends_at']))}")
        elif row["winner_id"] and row["winning_bid"]:
            lines.append(
                f"落札: <@{int(row['winner_id'])}> "
                f"{utils.fmt_int(int(row['winning_bid']))}"
            )
        embed.add_field(
            name=f"`{int(row['id'])}` {utils.truncate(str(row['name']), 80)}",
            value="\n".join(lines),
            inline=False,
        )
    return embed


class AuctionBidModal(discord.ui.Modal):
    """入札額の入力。"""

    def __init__(self, auction: Any) -> None:
        super().__init__(
            title=utils.truncate(f"入札: {auction['name']}", 45), timeout=300
        )
        self.auction_id = int(auction["id"])
        minimum = auction_minimum_bid(auction)
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label=f"入札額 (最低 {minimum})",
            placeholder=f"{minimum} 以上の半角数字で入力",
            required=True,
            min_length=1,
            max_length=config.AMOUNT_INPUT_MAX_LEN,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await bot_of(interaction).handle_auction_bid(
            interaction, self.auction_id, str(self.amount.value)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("入札Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class AuctionView(discord.ui.View):
    """オークションのパネル (Persistent View)。

    どのオークションかはメッセージIDから引く。custom_id は固定値なので
    Bot を再起動してもボタンが動き続ける。
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="入札する", emoji="💸", style=discord.ButtonStyle.success,
        custom_id=config.CustomID.AUCTION_BID,
    )
    async def bid(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_auction_bid_button(interaction)


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
            value=copy_block(utils.fmt_asset(asset_amount, unit="")),
            inline=False,
        )
    else:
        embed.add_field(
            name="① 送る金額",
            value=copy_block(str(amount)),
            inline=False,
        )
    embed.add_field(
        name=f"② 送り先 ({destination['label'] or '受取先'})",
        value=copy_block(str(destination["address"]), hint=COPY_HINT),
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


def claim_link_embed(quote: dict[str, Any], *, resumed: bool = False) -> discord.Embed:
    """請求リンクの支払い案内 (ステップ 2/2)。

    Bot が金額を指定して発行するため、送金リンク方式と違い
    金額の打ち間違いが起きない。
    """
    embed = discord.Embed(
        title=("⌛ 支払いをお待ちしています" if resumed
               else "🧾 このリンクを支払ってください"),
        description=(
            f"{step_line(2, 2)}\n{SEPARATOR}\n"
            + ("前回発行したリンクがまだ有効です。\n" if resumed else "")
            + "**リンクを開いて支払うだけ**で、自動で残高に反映されます。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.WARNING if resumed else config.Color.ACCENT,
    )
    embed.add_field(
        name="🔗 タップして支払う",
        value=f"{quote['url']}",
        inline=False,
    )
    embed.add_field(
        name="📋 コピー用",
        value=copy_block(str(quote["url"]), hint=COPY_HINT),
        inline=False,
    )
    embed.add_field(
        name="この取引の内容",
        value=(
            f"支払う金額: **{utils.fmt_yen(int(quote['amount']))}**\n"
            f"チャージ率: **{utils.fmt_rate(quote['charge_rate'])}**"
            + (f" (<@&{int(quote['role_id'])}>)" if quote.get("role_id") else "") + "\n"
            f"もらえる残高: **{utils.fmt_int(int(quote['credited']))}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="期限",
        value=utils.discord_ts(quote.get("expires_at"), "R"),
        inline=True,
    )
    embed.add_field(
        name="▶ やること",
        value=(
            "**1.** 上のリンクを開く (Kyash アプリが開きます)\n"
            "**2.** 表示された金額をそのまま支払う\n"
            "**3.** 自動で確認して残高に反映します (最大1分ほど)\n"
            "　　急ぐときは `🔄 支払いを確認` を押してください"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚠️ 注意",
        value=(
            "・**支払いは1回だけ**にしてください (2回払っても残高は1回分です)\n"
            "・このリンクは**あなた専用**です。他の人に渡さないでください\n"
            "・金額は Bot が指定しているので、変更する必要はありません\n"
            "・期限を過ぎるとリンクは無効になります"
        ),
        inline=False,
    )
    embed.set_footer(text=f"取引ID: {quote['tx_id']}")
    return embed


def claim_pending_embed(*, tx_id: str, amount: int) -> discord.Embed:
    """支払いをまだ確認できないときの案内。"""
    embed = discord.Embed(
        title="⌛ まだ支払いを確認できていません",
        description=(
            f"{SEPARATOR}\n"
            "Kyash 側の反映に少し時間がかかることがあります。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.WARNING,
    )
    embed.add_field(name="金額", value=f"**{utils.fmt_yen(amount)}**", inline=True)
    embed.add_field(
        name="▶ 次にどうすればいいですか？",
        value=(
            "**1.** Kyash アプリで支払いが完了しているか確認する\n"
            "**2.** 30秒ほど待ってから `🔄 支払いを確認` をもう一度押す\n"
            "**3.** Bot も自動で確認しているので、そのまま待っても反映されます\n\n"
            "まだ支払っていない場合は、上のリンクから支払ってください。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"取引ID: {tx_id}")
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
        value = copy_block(utils.truncate(str(request["proof_ref"]), 200))
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
            value=copy_block(utils.truncate(str(request["proof_ref"]), 300)),
            inline=False,
        )
    if request["destination"]:
        embed.add_field(
            name="入金先",
            value=copy_block(utils.truncate(str(request["destination"]), 200)),
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


def kyash_accounts_embed(
    snapshot: dict[str, Any], *, show_balance: bool = False
) -> discord.Embed:
    """受取用アカウントの一覧 (複数登録に対応)。

    残高の金額は Bot Owner にのみ見せる (``show_balance``)。
    """
    accounts = list(snapshot.get("accounts") or [])
    usable = int(snapshot.get("usable_count") or 0)
    problems: list[str] = []
    if not accounts:
        problems.append("🔴 アカウントが登録されていません (`/kyash add`)")
    elif usable == 0:
        problems.append("🔴 利用できるアカウントがありません (`/kyash login`)")
    for account in accounts:
        if account.get("enabled") and account.get("limit_reached"):
            problems.append(f"🟠 `{account['label']}` が残高しきい値に達しています")
        if account.get("enabled") and account.get("token_expiring_soon"):
            days = account.get("token_days_left")
            problems.append(
                f"🟠 `{account['label']}` のトークン残り "
                + (f"{days:.1f}日" if isinstance(days, (int, float)) else "わずか")
            )
    embed = discord.Embed(
        title="🔐 受取用 Kyash アカウント",
        description=(
            f"{SEPARATOR}\n"
            f"登録 **{len(accounts)}** 件 / 利用可 **{usable}** 件\n"
            + ("\n".join(problems) if problems else "🟢 異常はありません")
            + f"\n{SEPARATOR}"
        ),
        color=config.Color.DANGER if problems else config.Color.SUCCESS,
    )
    for account in accounts[:10]:
        state = config.KYASH_STATUS_LABELS.get(
            str(account["status"]), str(account["status"])
        )
        lines = [
            f"状態: **{state}**" + ("" if account.get("enabled") else "  ⏸ 無効化中"),
            f"ユーザー名: `{account.get('username') or '-'}`",
            f"優先度: {account.get('priority')}  (小さいほど先に使う)",
        ]
        threshold = int(account.get("threshold") or 0)
        if threshold:
            headroom = account.get("headroom")
            lines.append(
                f"しきい値: {utils.fmt_yen(threshold)}"
                + (f" / 残り {utils.fmt_yen(int(headroom))}" if headroom is not None else "")
            )
        else:
            lines.append("しきい値: 未設定 (無制限)")
        if show_balance and account.get("wallet_balance") is not None:
            lines.append(f"残高: **{utils.fmt_yen(int(account['wallet_balance']))}**")
        days = account.get("token_days_left")
        if isinstance(days, (int, float)):
            lines.append(f"トークン残り: {days:.1f}日")
        if account.get("last_error"):
            lines.append(
                "直近のエラー: "
                + utils.truncate(utils.sanitize_for_log(str(account["last_error"])), 150)
            )
        embed.add_field(
            name=f"#{account['id']} {account['label']}",
            value="\n".join(lines),
            inline=False,
        )
    embed.add_field(
        name="▶ 使い方",
        value=(
            "`/kyash add` で枠を追加 → `/kyash login` でログイン\n"
            "`/kyash threshold` で受取上限、`/kyash priority` で使う順番を設定\n"
            "余裕のあるアカウントが**自動で選ばれます**。"
            "全部が上限に達したときだけチャージが止まります。"
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Kyasher {snapshot.get('module_version')} / "
             "パスワード・トークン等の秘密情報は表示されません"
    )
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
            + ("新しく発行しました。" if created
               else "すでに発行済みのリンクです (同じものを使い続けてください)。")
            + "\n**このリンク経由の参加だけ**が報酬の対象になります。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.ACCENT,
    )
    # コードブロック = 正確にコピーできる / 素のURL = タップして共有できる
    # (コードブロック内の URL はリンク化されないため、両方を出す)
    embed.add_field(
        name="📋 コピー用",
        value=copy_block(url),
        inline=False,
    )
    embed.add_field(
        name="🔗 タップして共有",
        value=f"{url}\n{COPY_HINT}",
        inline=False,
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
    accounts = list(kyash.get("accounts") or [])
    account_count = int(kyash.get("account_count") or 0)
    usable_count = int(kyash.get("usable_count") or 0)
    if account_count > 1:
        # 複数アカウント運用では「何台が使えるか」が重要
        if usable_count == 0:
            problems.append(f"🔴 Kyash: 使える受取アカウントが 0 / {account_count} 台")
        elif usable_count < account_count:
            down = [
                str(a.get("label"))
                for a in accounts
                if a.get("status") != config.KyashAccountStatus.ACTIVE
            ]
            problems.append(
                f"🟠 Kyash: {usable_count} / {account_count} 台のみ使用可"
                + (f" (異常: {', '.join(down[:3])})" if down else "")
            )
    elif kyash.get("status") != config.KyashAccountStatus.ACTIVE:
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
    limited = [
        str(a.get("label")) for a in accounts
        if a.get("limit_reached") and a.get("status") == config.KyashAccountStatus.ACTIVE
    ]
    if usable_count and len(limited) >= usable_count:
        problems.append("🔴 受取残高しきい値に到達 (全アカウント)")
    elif limited:
        problems.append(f"🟠 残高しきい値に到達: {', '.join(limited[:3])}")
    elif headroom is not None and headroom <= 0:
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
        name="Kyash 受取" + (f" ({usable_count}/{account_count}台)" if account_count > 1 else ""),
        value=(
            (
                # 複数台のときは代表状態ではなく各台の状態を出す
                "\n".join(
                    f"{'🟢' if a.get('status') == config.KyashAccountStatus.ACTIVE else '🔴'} "
                    f"{a.get('label')}"
                    + ("（上限）" if a.get("limit_reached") else "")
                    for a in accounts[:4]
                )
                + (f"\n…他 {account_count - 4} 台" if account_count > 4 else "")
                + "\n"
                if account_count > 1
                else f"{config.KYASH_STATUS_LABELS.get(str(kyash.get('status')), str(kyash.get('status')))}\n"
            )
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


def amount_prompt_embed(
    provider: str, limits: tuple[int, int] | None, entry: Any = None
) -> discord.Embed:
    """金額入力へ進むための案内 (混雑時の代替経路)。

    Discord では「3秒以内に応答」と「入力欄 (Modal) を開く」を両立できない
    場面がある。応答を優先して先に受け付けを返した場合に、この画面から
    改めて入力欄を開いてもらう。
    """
    name = config.PROVIDER_LABELS.get(provider, provider)
    emoji = config.PROVIDER_EMOJI.get(provider, "💠")
    embed = discord.Embed(
        title=f"{emoji} {name} でチャージ",
        description=(
            f"{SEPARATOR}\n"
            "下の `金額を入力する` を押すと入力欄が開きます。\n"
            f"{SEPARATOR}"
        ),
        color=config.Color.ACCENT,
    )
    if limits:
        low, high = limits
        embed.add_field(
            name="受付範囲",
            value=f"**{utils.fmt_yen(low)}** 〜 **{utils.fmt_yen(high)}**",
            inline=True,
        )
    if entry is not None and entry.get("rate") is not None:
        embed.add_field(
            name="チャージ率", value=f"**{utils.fmt_rate(entry['rate'])}**", inline=True
        )
    embed.set_footer(text="混み合っていたため、先に受付画面をお返ししました")
    return embed


class AmountEntryView(discord.ui.View):
    """「金額を入力する」ボタン (混雑時の代替経路)。

    先に応答を返した後は Modal を直接出せないため、この View から開く。
    """

    def __init__(
        self, provider: str, settings: "GuildSettings",
        limits: tuple[int, int] | None, *, owner_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self._provider = provider
        self._settings = settings
        self._limits = limits
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(
        label="金額を入力する", emoji="✏️", style=discord.ButtonStyle.success
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._provider == config.ChargeProvider.KYASH:
            await interaction.response.send_modal(AmountModal(self._settings))
            return
        limits = self._limits or (
            self._settings.minimum_charge, self._settings.maximum_charge
        )
        await interaction.response.send_modal(
            ManualAmountModal(self._provider, self._settings, limits)
        )


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


class ClaimPaymentView(discord.ui.View):
    """請求リンクの支払い確認 (Ephemeral・一時 View)。"""

    def __init__(self, tx_id: str, *, owner_id: int, timeout: float) -> None:
        super().__init__(timeout=max(30.0, timeout))
        self.tx_id = tx_id
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await safe_respond(
                interaction,
                embed=info_embed("操作できません", "この画面を開いた本人のみ操作できます。"),
            )
            return False
        return True

    @discord.ui.button(label="支払いを確認", emoji="🔄", style=discord.ButtonStyle.success)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_claim_check(interaction, self.tx_id)

    @discord.ui.button(label="やめる", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await bot_of(interaction).on_claim_cancel(interaction, self.tx_id)
        self.stop()


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

    def __init__(self, *, account_id: int | None = None) -> None:
        super().__init__(timeout=300)
        #: ログイン先の受取用アカウント (None なら代表アカウント)
        self.account_id = account_id

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        email = str(self.email.value).strip()
        password = str(self.password.value)
        self.password.default = None
        await bot_of(interaction).handle_kyash_login(
            interaction, email, password, account_id=self.account_id
        )

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


class ItemInputModal(discord.ui.Modal):
    """購入時に名前などを入力してもらう Modal。

    カスタムロール・ニックネーム・専用チャンネルは利用者の入力が要るため、
    「購入する」を押した後にこの画面を出す。入力を送った時点で購入を確定する。
    """

    def __init__(self, item: Any) -> None:
        item_type = str(item["item_type"] or config.ShopItemType.ROLE)
        label = config.SHOP_INPUT_LABELS.get(item_type, "内容")
        super().__init__(
            title=utils.truncate(f"{item['name']} の{label}", 45), timeout=300
        )
        self.item_id = int(item["id"])
        self.item_type = item_type
        limit = (
            config.NICKNAME_MAX_LEN if item_type == config.ShopItemType.NICKNAME
            else config.CUSTOM_NAME_MAX_LEN
        )
        if item_type == config.ShopItemType.PRIVATE_CHANNEL:
            placeholder = "例: わたしの部屋 (記号は - に置き換わります)"
        elif item_type == config.ShopItemType.NICKNAME:
            placeholder = "例: たろう"
        else:
            placeholder = "例: 常連さん"
        self.value_input: discord.ui.TextInput = discord.ui.TextInput(
            label=utils.truncate(label, 45),
            placeholder=placeholder,
            required=True,
            min_length=1,
            max_length=limit,
        )
        self.add_item(self.value_input)
        # カスタムロールのみ、色も選べるようにする (管理者が固定していれば無視される)
        self.color_input: discord.ui.TextInput | None = None
        if item_type == config.ShopItemType.CUSTOM_ROLE and not (
            utils.load_json_dict(item["payload"]).get("color")
        ):
            self.color_input = discord.ui.TextInput(
                label="色 (任意・#RRGGBB 形式)",
                placeholder="例: #FF66AA / 空欄なら色なし",
                required=False,
                min_length=0,
                max_length=7,
            )
            self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        color = str(self.color_input.value) if self.color_input is not None else None
        await bot_of(interaction).finish_shop_purchase(
            interaction, self.item_id,
            item_input=str(self.value_input.value), item_color=color, defer=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # type: ignore[override]
        logger.error("購入入力Modalでエラー: %s", utils.safe_error_text(error))
        await safe_respond(interaction, embed=error_embed(config.ErrorCode.UNKNOWN_ERROR))


class ShopSelect(discord.ui.Select["ShopSelectView"]):
    """商品の選択メニュー (在庫や価格が変わるため都度生成する)。"""

    def __init__(self, items: Sequence[Any]) -> None:
        options: list[discord.SelectOption] = []
        for item in list(items)[:25]:
            duration = int(item["duration_days"])
            stock = int(item["stock"])
            item_type = str(item["item_type"] or config.ShopItemType.ROLE)
            subscription = bool(int(item["subscription"] or 0)) and duration > 0
            details = [f"{utils.fmt_int(int(item['price']))}"]
            if subscription:
                details.append(f"{duration}日ごと自動更新")
            else:
                details.append(f"{duration}日間" if duration > 0 else "無期限")
            details.append(config.SHOP_ITEM_TYPE_LABELS.get(item_type, item_type))
            if stock >= 0:
                details.append(f"残り{stock}個")
            options.append(
                discord.SelectOption(
                    label=utils.truncate(
                        ("🔁 " if subscription else "") + str(item["name"]), 90),
                    value=str(int(item["id"])),
                    description=utils.truncate(" / ".join(details), 90),
                    emoji=config.SHOP_ITEM_TYPE_EMOJI.get(item_type, "🎫"),
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


class ShopConfirmView(discord.ui.View):
    """購入の最終確認 (Ephemeral)。

    入力が必要な商品では、この確認ボタンから直接 Modal を開く。
    一度応答した interaction では Modal を出せないため、``ConfirmView`` の
    ように先に画面を書き換えてから待つ作り方はできない。
    """

    def __init__(self, item: Any, *, owner_id: int) -> None:
        super().__init__(timeout=120)
        self._owner_id = owner_id
        self._item = item
        self._item_id = int(item["id"])
        self._needs_input = (
            str(item["item_type"] or config.ShopItemType.ROLE)
            in config.SHOP_TYPES_NEED_INPUT
        )
        self.confirm.label = "入力して購入する" if self._needs_input else "購入する"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._owner_id:
            await safe_respond(interaction, embed=error_embed(config.ErrorCode.NOT_ALLOWED))
            return False
        return True

    @discord.ui.button(label="購入する", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self._needs_input:
            # Modal を先に出す (ここで応答してしまうと Modal を出せない)
            await interaction.response.send_modal(ItemInputModal(self._item))
            self.stop()
            return
        await interaction.response.edit_message(
            embed=info_embed("⏳ 購入中", "処理を実行しています…", color=config.Color.WARNING),
            view=self,
        )
        self.stop()
        await bot_of(interaction).finish_shop_purchase(interaction, self._item_id)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=info_embed("キャンセルしました", "購入は行われていません。",
                             color=config.Color.NEUTRAL),
            view=self,
        )
        self.stop()


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
