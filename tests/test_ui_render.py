#!/usr/bin/env python3
"""UI (Embed) の描画テスト。

Discord は Embed の各要素に文字数上限を持っており、超えると 400 を返して
「Bot が無言になる」形の不具合になる。ここでは ui.py の全 Embed 関数を
現実的な値で描画し、上限違反・例外・秘密情報の混入がないことを確認する。

    python3 tests/test_ui_render.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database  # noqa: E402
import ui  # noqa: E402
import utils  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

# Discord の Embed 上限
LIMIT_TITLE = 256
LIMIT_DESC = 4096
LIMIT_FIELDS = 25
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FOOTER = 2048
LIMIT_TOTAL = 6000

#: ログ・Embed に出てはならない文字列 (テスト用のダミー秘密)
SECRETS = ("MTIzNDU2Nzg5.SECRET.TOKEN", "hunter2password", "123456otp")


def check(cond: bool, label: str) -> None:
    (PASS if cond else FAIL).append(label)
    print(f"  {'ok  ' if cond else 'NG  '} {label}")


def verify(label: str, embed: object) -> None:
    """Embed が Discord の上限に収まっているかを検査する。"""
    import discord

    if not isinstance(embed, discord.Embed):
        check(False, f"{label}: Embed が返らない ({type(embed).__name__})")
        return
    problems: list[str] = []
    if len(embed.title or "") > LIMIT_TITLE:
        problems.append(f"title {len(embed.title or '')}")
    if len(embed.description or "") > LIMIT_DESC:
        problems.append(f"description {len(embed.description or '')}")
    if len(embed.fields) > LIMIT_FIELDS:
        problems.append(f"fields {len(embed.fields)}")
    for i, f in enumerate(embed.fields):
        if len(f.name or "") > LIMIT_FIELD_NAME:
            problems.append(f"field[{i}].name {len(f.name or '')}")
        if len(f.value or "") > LIMIT_FIELD_VALUE:
            problems.append(f"field[{i}].value {len(f.value or '')}")
        if not (f.name or "").strip() or not (f.value or "").strip():
            problems.append(f"field[{i}] が空")
    if embed.footer and len(embed.footer.text or "") > LIMIT_FOOTER:
        problems.append(f"footer {len(embed.footer.text or '')}")
    if len(embed) > LIMIT_TOTAL:
        problems.append(f"total {len(embed)}")
    # 空の区切り線だけが並ぶなどの見た目の崩れ
    desc = embed.description or ""
    if f"{ui.SEPARATOR}\n{ui.SEPARATOR}" in desc:
        problems.append("区切り線の間が空")
    text = "\n".join(
        [embed.title or "", desc, (embed.footer.text if embed.footer else "") or ""]
        + [f"{f.name}\n{f.value}" for f in embed.fields]
    )
    for secret in SECRETS:
        if secret in text:
            problems.append(f"秘密情報が混入 ({secret[:6]}…)")
    check(not problems, f"{label}" + (f" → {', '.join(problems)}" if problems else ""))


class Row(dict):
    """sqlite3.Row の代わりに使う辞書 (添字アクセス互換)。"""

    def __getitem__(self, key: object) -> object:  # type: ignore[override]
        return dict.__getitem__(self, key)  # type: ignore[arg-type]


def main() -> None:
    now = utils.now_ts()
    settings = database.GuildSettings(guild_id=1)
    long_settings = database.GuildSettings(
        guild_id=1,
        panel_title="あ" * 240,
        panel_description="い" * 1500,
        charge_rate=Decimal("1.5"),
        accent_color=0x00FF00,
    )

    class U:
        display_name = "テスト利用者" * 5
        id = 12345
        mention = "<@12345>"
        display_avatar = None

        def __str__(self) -> str:
            return "tester#0001"

    print("=== 1. パネル / 一覧 ===")
    verify("charge_panel_embed (既定)", ui.charge_panel_embed(settings, kyash_ready=True))
    verify("charge_panel_embed (未ログイン)",
           ui.charge_panel_embed(settings, kyash_ready=False))
    verify("charge_panel_embed (長文設定)",
           ui.charge_panel_embed(long_settings, kyash_ready=True))
    verify("ranking_embed (空)",
           ui.ranking_embed(None, [], settings, updated_at=now))
    verify("ranking_embed (満杯)",
           ui.ranking_embed(None, [(i, 10_000 - i, "利用者" * 10) for i in range(50)],
                            settings, updated_at=now))
    for rtype in config.RANKING_TYPE_LABELS:
        verify(f"ranking_embed ({rtype})",
               ui.ranking_embed(None, [(1, 500, "利用者")], settings,
                                updated_at=now, ranking_type=rtype))
    verify("ranking_disabled_embed", ui.ranking_disabled_embed())

    print("\n=== 2. 利用者向け ===")
    summary = {"count": 12, "sent": 120_000, "credited": 156_000}
    verify("balance_embed (最小)", ui.balance_embed(U(), 0, {"count": 0, "sent": 0, "credited": 0}))
    verify("balance_embed (全部)",
           ui.balance_embed(U(), 1_234_567, summary, rank=3, rank_total=120,
                            shop_available=True, spent=45_000))
    verify("help_embed (最小)", ui.help_embed(settings))
    verify("help_embed (全部)",
           ui.help_embed(settings, shop_available=True, campaign_name="春の招待祭" * 10))
    verify("history_embed (空)", ui.history_embed([], page=1, total_pages=1, total=0))
    rows = [
        Row(id=f"tx{i:03d}", requested_amount=1000 + i, received_amount=1000 + i,
            charge_rate="1.3", user_id=1, guild_id=1,
            credited_amount=1300 + i, status=st, created_at=now - i * 60,
            error_code=None if st == config.TxStatus.COMPLETED else config.ErrorCode.INVALID_LINK,
            refunded_at=None)
        for i, st in enumerate([
            config.TxStatus.COMPLETED, config.TxStatus.FAILED, config.TxStatus.MANUAL_REVIEW,
            config.TxStatus.QUEUED, config.TxStatus.CANCELLED, config.TxStatus.EXPIRED,
            config.TxStatus.WAITING_LINK, config.TxStatus.PROCESSING,
            config.TxStatus.COMPLETED, config.TxStatus.COMPLETED,
        ])
    ]
    verify("history_embed (全ステータス)",
           ui.history_embed(rows, page=2, total_pages=5, total=48))

    print("\n=== 3. チャージ手順 ===")
    verify("link_wait_embed", ui.link_wait_embed(
        tx_id="tx-0001", amount=1000, rate=Decimal("1.3"), credited=1300,
        expires_at=now + 600))
    verify("link_wait_embed (再開)", ui.link_wait_embed(
        tx_id="tx-0001", amount=99_999_999, rate=Decimal("999.99"), credited=1,
        resumed=True))
    verify("link_accepted_embed (先頭)",
           ui.link_accepted_embed(tx_id="tx-0001", amount=1000, waiting=0))
    verify("link_accepted_embed (待ち)",
           ui.link_accepted_embed(tx_id="tx-0001", amount=1000, waiting=42))
    check(ui.step_line(1).count("●") == 1 and ui.step_line(3).count("●") == 3,
          "step_line が進行度を表す")

    print("\n=== 4. エラー表示 ===")
    missing_next: list[str] = []
    for code in sorted(config.USER_ERROR_MESSAGES):
        embed = ui.error_embed(code)
        verify(f"error_embed({code})", embed)
        if code not in config.USER_ERROR_NEXT_ACTIONS:
            missing_next.append(code)
    check(not missing_next,
          f"全エラーに次の行動が用意されている (不足: {', '.join(missing_next) or 'なし'})")
    verify("error_embed (管理者向け詳細)",
           ui.error_embed(config.ErrorCode.UNKNOWN_ERROR, admin_detail="x" * 2000))
    unknown = ui.error_embed("NOT_A_REAL_CODE")
    check(unknown.title is not None, "未知のエラーコードでも Embed を返す")

    print("\n=== 5. 実績 / DM / ログ ===")
    verify("achievement_embed", ui.achievement_embed(
        user_mention="<@1>", received_amount=1000, charge_rate=Decimal("1.3"),
        credited_amount=1300, status=config.TxStatus.COMPLETED, tx_id="tx", timestamp=now))
    verify("achievement_embed (代理)", ui.achievement_embed(
        user_mention="<@1>", received_amount=1000, charge_rate="1.3",
        credited_amount=None, status=config.TxStatus.MANUAL_REVIEW, tx_id="tx",
        timestamp=now, proxy=True))
    for status in (config.PurchaseStatus.ACTIVE, config.PurchaseStatus.EXPIRED,
                   config.PurchaseStatus.REFUNDED):
        verify(f"shop_achievement_embed ({status})", ui.shop_achievement_embed(
            user_mention="<@1>", item_name="VIP ロール" * 20, role_id=7, price=5000,
            balance_after=1000, expires_at=now + 86400, purchase_id=1,
            timestamp=now, status=status))
    verify("shop_achievement_embed (無期限)", ui.shop_achievement_embed(
        user_mention="<@1>", item_name="VIP", role_id=7, price=5000,
        balance_after=0, expires_at=None, purchase_id=1, timestamp=now))
    verify("invite_achievement_embed", ui.invite_achievement_embed(
        inviter_mention="<@1>", invited_mention="<@2>", inviter_reward=300,
        invited_reward=100, campaign_name="春の招待祭", record_id=1, timestamp=now))
    verify("invite_achievement_embed (招待者不明)", ui.invite_achievement_embed(
        inviter_mention=None, invited_mention="<@2>", inviter_reward=0,
        invited_reward=100, campaign_name="春の招待祭", record_id=1, timestamp=now))
    verify("dm_success_embed", ui.dm_success_embed(
        guild_name="サーバー" * 30, received_amount=1000, charge_rate=Decimal("1.3"),
        credited_amount=1300, balance_after=5000, tx_id="tx", timestamp=now))
    verify("dm_failure_embed", ui.dm_failure_embed(
        guild_name="サーバー", tx_id="tx", error_code=config.ErrorCode.LINK_EXPIRED,
        timestamp=now, requested_amount=1000))
    verify("dm_failure_embed (金額なし)", ui.dm_failure_embed(
        guild_name="サーバー", tx_id="tx", error_code="NOT_A_REAL_CODE", timestamp=now))
    verify("dm_review_embed",
           ui.dm_review_embed(guild_name="サーバー", tx_id="tx", timestamp=now))
    verify("log_embed", ui.log_embed("題名", "本文", fields=[("名", "値", True)] * 20))
    verify("refund_notice_embed", ui.refund_notice_embed(
        guild_name="サーバー", tx_id="tx", amount=1000, balance_after=0, reason="重複"))
    verify("invite_reward_embed", ui.invite_reward_embed(
        guild_name="サーバー", label="招待報酬", amount=300, balance_after=300,
        campaign_name="春の招待祭"))

    print("\n=== 6. ショップ ===")
    items = [
        Row(id=i, name=f"商品{i}" * 8, role_id=100 + i, price=1000 * (i + 1),
            duration_days=0 if i % 2 else 30, stock=-1 if i % 3 else 3,
            purchase_limit=0 if i % 2 else 1, description="説明" * 20,
            active=1, sort_order=i)
        for i in range(12)
    ]
    verify("shop_panel_embed (空)", ui.shop_panel_embed(settings, []))
    verify("shop_panel_embed (多数)", ui.shop_panel_embed(settings, items))
    # 期限つき (30日/上限1回) と無期限の2種類で、購入可否の判定を確かめる
    timed = Row(id=1, name="期限つきロール", role_id=101, price=1000, duration_days=30,
                stock=3, purchase_limit=1, description="説明", active=1, sort_order=0)
    forever = Row(**{**timed, "duration_days": 0, "purchase_limit": 0, "stock": -1})
    cases = (
        ("購入可能", timed, 999_999, 0, False, False),
        ("残高不足", timed, 500, 0, False, True),
        ("上限到達", timed, 999_999, 1, False, True),
        ("在庫切れ", Row(**{**timed, "stock": 0}), 999_999, 0, False, True),
        ("無期限を既に所持", forever, 999_999, 0, True, True),
        ("期限つきは再購入できる", timed, 999_999, 0, True, False),
        ("無期限を未所持", forever, 999_999, 3, False, False),
    )
    for label, target, bal, owned, has_role, expect_blocked in cases:
        embed, blocker = ui.purchase_confirm_embed(
            item=target, balance=bal, owned=owned, already_has_role=has_role)
        verify(f"purchase_confirm_embed ({label})", embed)
        check((blocker is not None) == expect_blocked,
              f"購入可否の判定: {label} → {'不可' if expect_blocked else '可'}")
    verify("purchase_success_embed", ui.purchase_success_embed(
        item_name="VIP" * 50, role_id=7, price=5000, balance_after=1000,
        expires_at=now + 86400, purchase_id=1))
    verify("purchase_success_embed (無期限)", ui.purchase_success_embed(
        item_name="VIP", role_id=7, price=5000, balance_after=0,
        expires_at=None, purchase_id=1))
    verify("my_items_embed (空)", ui.my_items_embed([]))
    verify("my_items_embed", ui.my_items_embed([
        Row(id=i, item_name=f"商品{i}" * 8, role_id=100 + i, price=1000,
            status=st, expires_at=None if i % 2 else now + 86400,
            created_at=now - i * 3600)
        for i, st in enumerate([config.PurchaseStatus.ACTIVE,
                                config.PurchaseStatus.EXPIRED,
                                config.PurchaseStatus.REFUNDED] * 4)
    ]))

    print("\n=== 7. 招待キャンペーン ===")
    verify("invite_panel_embed (未開催)", ui.invite_panel_embed(settings, None))
    campaign = Row(
        id=1, name="春の招待祭" * 10, status=config.CampaignStatus.ACTIVE,
        inviter_reward=300, invited_reward=100, require_charge=1, require_days=3,
        min_account_age_days=7, daily_limit=5, total_limit=50,
        starts_at=now - 86400, ends_at=now + 86400 * 7)
    verify("invite_panel_embed (開催中)", ui.invite_panel_embed(settings, campaign))
    verify("invite_panel_embed (終了未定・制限なし)", ui.invite_panel_embed(settings, Row(
        id=1, name="無制限", status=config.CampaignStatus.ACTIVE,
        inviter_reward=0, invited_reward=0, require_charge=0, require_days=0,
        min_account_age_days=0, daily_limit=0, total_limit=0,
        starts_at=now, ends_at=None)))
    inv_summary = {"confirmed": 5, "pending": 2, "hold": 1, "rejected": 3, "reward": 1500}
    verify("invite_link_embed (新規)", ui.invite_link_embed(
        url="https://discord.gg/abcdefg", code="abcdefg", summary=inv_summary, created=True))
    verify("invite_link_embed (既存)", ui.invite_link_embed(
        url="https://discord.gg/abcdefg", code="abcdefg", summary=inv_summary, created=False))
    verify("invite_status_embed (空)", ui.invite_status_embed(
        summary={"confirmed": 0, "pending": 0, "hold": 0, "rejected": 0, "reward": 0},
        rank=None, total=0, records=[]))
    verify("invite_status_embed", ui.invite_status_embed(
        summary=inv_summary, rank=2, total=30, records=[
            Row(id=i, invited_id=1000 + i, status=st, reward_inviter=300,
                reward_invited=100, reason=config.InviteRejectReason.SELF_INVITE,
                joined_at=now - i * 60, confirmed_at=now)
            for i, st in enumerate([config.InviteStatus.CONFIRMED,
                                    config.InviteStatus.PENDING,
                                    config.InviteStatus.HOLD,
                                    config.InviteStatus.REJECTED] * 4)
        ]))

    print("\n=== 8. チャージ方式 (PayPay / LTC) ===")
    entries = [
        {"provider": config.ChargeProvider.KYASH, "available": True, "reason": None,
         "error_code": None, "minimum": 100, "maximum": 50_000,
         "rate": Decimal("130"), "destination": None},
        {"provider": config.ChargeProvider.PAYPAY, "available": True, "reason": None,
         "error_code": None, "minimum": 500, "maximum": 30_000, "rate": Decimal("120"),
         "destination": Row(address="paypay-id-123", label="受取用" * 20,
                            note="メモ欄には何も書かないでください" * 20)},
        {"provider": config.ChargeProvider.LTC, "available": False,
         "reason": "入金先が未登録です",
         "error_code": config.ErrorCode.PROVIDER_NOT_CONFIGURED,
         "minimum": 1000, "maximum": 100_000, "rate": Decimal("150"), "destination": None},
    ]
    verify("provider_select_embed", ui.provider_select_embed(entries))
    verify("provider_select_embed (全部使えない)",
           ui.provider_select_embed([{**e, "available": False, "reason": "停止中"}
                                     for e in entries]))
    verify("charge_panel_embed (複数方式)",
           ui.charge_panel_embed(settings, kyash_ready=True, providers=entries))
    verify("charge_panel_embed (Kyashのみ)",
           ui.charge_panel_embed(settings, kyash_ready=True, providers=[entries[0]]))
    verify("help_embed (複数方式)",
           ui.help_embed(settings, shop_available=True, campaign_name="春の祭",
                         providers=entries))
    check(ui.step_line(1, ui.MANUAL_CHARGE_STEPS).count("○") == 3,
          "承認制は 4 ステップ表示になる")

    ltc_quote = {
        "request_id": 7, "provider": config.ChargeProvider.LTC, "amount": 1000,
        "charge_rate": Decimal("150"), "role_id": 101, "estimated_credit": 1500,
        "asset_amount": Decimal("0.08333334"), "asset_price": Decimal("12000"),
        "price_source": config.PRICE_SOURCE_COINGECKO, "price_stale": False,
        "destination": Row(address="ltc1q" + "x" * 40, label="受取用", note="長い注意" * 80),
        "quote_expires_at": now + 1800,
    }
    verify("deposit_embed (LTC)", ui.deposit_embed(ltc_quote))
    verify("deposit_embed (LTC・代替レート)",
           ui.deposit_embed({**ltc_quote, "price_stale": True}))
    paypay_quote = {
        "request_id": 8, "provider": config.ChargeProvider.PAYPAY, "amount": 1000,
        "charge_rate": Decimal("120"), "role_id": None, "estimated_credit": 1200,
        "asset_amount": None, "asset_price": None, "price_source": None,
        "price_stale": False,
        "destination": Row(address="paypay-id-123", label=None, note=None),
        "quote_expires_at": now + 1800,
    }
    verify("deposit_embed (PayPay)", ui.deposit_embed(paypay_quote))
    verify("request_submitted_embed", ui.request_submitted_embed({
        "request_id": 7, "provider": config.ChargeProvider.LTC,
        "estimated_credit": 1500, "pending_total": 4, "expires_at": now + 86400,
    }))
    verify("request_submitted_embed (待ちなし)", ui.request_submitted_embed({
        "request_id": 7, "provider": config.ChargeProvider.PAYPAY,
        "estimated_credit": 1200, "pending_total": 1, "expires_at": None,
    }))

    def make_request(**over: object) -> Row:
        base = dict(
            id=7, guild_id=1, user_id=100, provider=config.ChargeProvider.LTC,
            status=config.RequestStatus.PENDING, requested_amount=1000,
            charge_rate="150", role_id=101, estimated_credit=1500,
            asset_amount="0.08333334", asset_price="12000",
            price_source=config.PRICE_SOURCE_COINGECKO, price_fetched_at=now - 60,
            destination="ltc1q" + "x" * 40, proof_ref="ab" * 32, proof_hash="h" * 64,
            proof_note=None, review_channel_id=5, review_message_id=6,
            reviewed_by=None, reviewed_at=None, reject_reason=None,
            credited_amount=None, transaction_id=None, operation_id=None,
            quote_expires_at=now + 1800, expires_at=now + 86400,
            submitted_at=now - 30, created_at=now - 120, updated_at=now,
        )
        base.update(over)
        return Row(**base)

    for status, extra in (
        (config.RequestStatus.PENDING, {}),
        (config.RequestStatus.APPROVED,
         {"credited_amount": 1500, "transaction_id": "TX-ABC", "reviewed_by": 9,
          "reviewed_at": now}),
        (config.RequestStatus.REJECTED,
         {"reject_reason": "入金が確認できませんでした" * 40, "reviewed_by": 9,
          "reviewed_at": now}),
        (config.RequestStatus.EXPIRED, {}),
        (config.RequestStatus.CANCELLED, {}),
        (config.RequestStatus.QUOTED, {"proof_ref": None, "submitted_at": None}),
    ):
        verify(f"review_card_embed ({status})",
               ui.review_card_embed(request=make_request(status=status, **extra),
                                    guild_name="テストサーバー" * 20, pending_total=5))
    verify("review_card_embed (PayPay)",
           ui.review_card_embed(
               request=make_request(provider=config.ChargeProvider.PAYPAY,
                                    asset_amount=None, asset_price=None,
                                    proof_ref="ABC-123", destination="paypay-id"),
               guild_name="サーバー", pending_total=1))
    verify("review_detail_embed", ui.review_detail_embed(
        request=make_request(), history=[make_request(id=i) for i in range(8)],
        balance=12_345))
    verify("review_detail_embed (PayPay・履歴なし)", ui.review_detail_embed(
        request=make_request(provider=config.ChargeProvider.PAYPAY, asset_amount=None,
                             asset_price=None, proof_ref="ABC-123"),
        history=[], balance=0))
    verify("request_result_dm_embed (却下)", ui.request_result_dm_embed(
        guild_name="サーバー" * 30,
        request=make_request(status=config.RequestStatus.REJECTED,
                             reject_reason="入金が確認できませんでした" * 40)))
    verify("request_result_dm_embed (期限切れ)", ui.request_result_dm_embed(
        guild_name="サーバー", request=make_request(status=config.RequestStatus.EXPIRED)))
    verify("request_list_embed (空)",
           ui.request_list_embed([], page=1, total_pages=1, total=0, title="📨 申請"))
    verify("request_list_embed", ui.request_list_embed(
        [make_request(id=i, status=st) for i, st in enumerate(
            list(config.REQUEST_STATUS_LABELS) * 2)],
        page=2, total_pages=5, total=48, title="📨 申請"))
    verify("provider_status_embed", ui.provider_status_embed(
        entries, guild_name="サーバー" * 30, review_channel_id=12345,
        price={"source": config.PRICE_SOURCE_COINGECKO, "manual_price": "11000",
               "manual_updated_at": now - 3600, "last_good_price": "12000",
               "last_good_at": now - 60, "cached_price": "12000", "cached_age": 30,
               "cached_stale": False, "last_error": "x" * 500,
               "consecutive_failures": 2},
        delegated=True))
    verify("provider_status_embed (価格なし・未設定)", ui.provider_status_embed(
        entries, guild_name="サーバー", review_channel_id=None, price=None,
        delegated=False))
    verify("history_embed (進行中の申請あり)", ui.history_embed(
        rows, page=1, total_pages=3, total=20,
        open_requests=[make_request(id=1, status=config.RequestStatus.QUOTED),
                       make_request(id=2, status=config.RequestStatus.PENDING)]))

    print("\n=== 9. 管理者向け ===")
    verify("daily_summary_embed", ui.daily_summary_embed(
        guild_name="サーバー", start=now - 86400, end=now,
        summary={"total": 10, "success": 8, "failed": 1, "review": 1,
                 "received": 10_000, "credited": 13_000, "users": 5,
                 "errors": [(config.ErrorCode.INVALID_LINK, 3)] * 8,
                 "top": [(i, 1000 * i) for i in range(1, 6)],
                 "spend_total": 5000, "spend_count": 4, "invites_confirmed": 2},
        distribution={"count": 5, "total": 50_000, "zero": 1, "median": 8000,
                      "max": 20_000, "top10_share": 40.0, "issued": 60_000,
                      "spent": 10_000}))
    request_counts = {k: 3 for k in config.REQUEST_STATUS_LABELS}
    price_snapshot = {
        "source": config.PRICE_SOURCE_COINGECKO, "manual_price": "11000",
        "manual_updated_at": now - 3600, "last_good_price": "12000",
        "last_good_at": now - 60, "cached_price": "12000", "cached_age": 30,
        "cached_stale": True, "last_error": "timeout", "consecutive_failures": 3,
    }
    verify("admin_panel_embed (申請・価格つき)", ui.admin_panel_embed(
        guild_name="サーバー", settings=settings,
        kyash={"logged_in": True, "wallet_balance": 1, "token_days_left": 30.0,
               "token_expiring_soon": False, "last_error": None,
               "consecutive_failures": 0, "wallet_headroom": 1000},
        queue={}, stats={"today_count": 1, "today_sent": 1, "today_credited": 1,
                         "success": 1, "failed": 0, "credited": 1, "users": 1},
        metrics={"receive_avg_seconds": 1, "purchases": 0, "purchase_refunds": 0,
                 "invites_confirmed": 0, "invites_hold": 0},
        review_count=2, updated_at=now,
        requests=request_counts, price=price_snapshot))
    verify("admin_panel_embed", ui.admin_panel_embed(
        guild_name="サーバー" * 30, settings=long_settings,
        kyash={"logged_in": True, "wallet_balance": 100_000, "token_days_left": 3.5,
               "token_expiring_soon": True, "last_error": "x" * 500,
               "consecutive_failures": 2, "wallet_headroom": 50_000},
        queue={config.TxStatus.QUEUED: 3, "PROCESSING": 1},
        stats={"total": 100, "success": 90, "failed": 5, "review": 5,
               "received": 100_000, "credited": 130_000, "users": 20,
               "errors": [], "top": [], "spend_total": 0, "spend_count": 0,
               "invites_confirmed": 0},
        metrics={"success_rate": 90.0, "average_seconds": 12.3, "p95_seconds": 30.0},
        review_count=5, updated_at=now))

    print("\n=== 10. 汎用 ===")
    verify("info_embed", ui.info_embed("題名" * 60, "本文" * 1000))
    verify("info_embed (超過入力を切り詰める)",
           ui.info_embed("題" * 1000, "本" * 10_000))
    verify("success_embed", ui.success_embed("完了"))
    verify("success_embed (超過入力)", ui.success_embed("完" * 1000, "了" * 10_000))
    verify("log_embed (フィールド超過)",
           ui.log_embed("題名", "本文", fields=[("名" * 500, "値" * 3000, True)] * 40))

    print("\n=== 11. 上限の最後の砦 (clamp_embed) ===")
    import discord

    broken = discord.Embed(title="題" * 500, description="本" * 9000)
    broken.set_footer(text="脚" * 4000)
    for i in range(40):
        broken.add_field(name="名" * 500, value="値" * 3000, inline=False)
    fixed = ui.clamp_embed(broken)
    check(len(fixed.title or "") <= LIMIT_TITLE, "title を上限内に収める")
    check(len(fixed.description or "") <= LIMIT_DESC, "description を上限内に収める")
    check(len(fixed.fields) <= LIMIT_FIELDS, "フィールド数を上限内に収める")
    check(all(len(f.value or "") <= LIMIT_FIELD_VALUE for f in fixed.fields),
          "フィールド値を上限内に収める")
    check(len(fixed) <= LIMIT_TOTAL, f"合計を上限内に収める ({len(fixed)}字)")
    intact = ui.info_embed("そのまま", "短い本文")
    ui.clamp_embed(intact)
    check(intact.title == "そのまま" and intact.description == "短い本文",
          "上限内の Embed は書き換えない")

    print("\n" + "=" * 70)
    print(f"結果: {len(PASS)} 件成功 / {len(FAIL)} 件失敗")
    for label in FAIL:
        print(f"  ✗ {label}")
    print("=" * 70)


if __name__ == "__main__":
    main()
    sys.exit(1 if FAIL else 0)
