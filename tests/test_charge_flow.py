"""チャージ処理の統合テスト (Kyash の HTTP 通信のみモック)。

添付モジュール (vendor/Kyasher) の実コードを通しつつ、Discord I/O を差し替えて
チャージの正常系・異常系・二重処理・再起動復旧・権限・ランキングを検証する。

実行:
    python3 tests/test_charge_flow.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"
config.RANKING_DEBOUNCE_SECONDS = 0.05
config.KYASH_RECEIPT_RECHECK_DELAY = 0.05

import requests  # noqa: E402

import kyash_service  # noqa: E402
import utils  # noqa: E402
from charge_service import ChargeError  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


# ---------------------------------------------------------------------------
# Kyash HTTP モック
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload=None, text: str = "", status: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


STATE: dict = {
    "wallet": 10_000,
    "links": {},          # link_id -> {amount, uuid, kind}
    "received": set(),    # 受取済み link_uuid
    "history": [],        # timeline (新しい順)
    "receive_mode": {},   # link_uuid -> 'ok' | 'reject' | 'timeout' | 'silent'
}


def add_link(link_id: str, amount: int, uuid: str, kind: str = "send") -> str:
    STATE["links"][link_id] = {"amount": amount, "uuid": uuid, "kind": kind}
    return f"https://kyash.me/payments/{link_id}"


def _link_page(link: dict) -> str:
    if link["kind"] == "send":
        return (
            f'<div class="amountText text_send">&yen;{link["amount"]:,}</div>'
            f'<a class="btn_send" data-href-app="kyash://claim/{link["uuid"]}">受け取る</a>'
        )
    return (
        f'<div class="amountText text_request">&yen;{link["amount"]:,}</div>'
        f'<a class="btn_request" data-href-app="kyash://request/u/{link["uuid"]}">送金</a>'
    )


def fake_get(url: str, **kwargs):
    if "kyash.me/payments/" in url:
        link_id = url.rsplit("/", 1)[-1]
        link = STATE["links"].get(link_id)
        if link is None or link["uuid"] in STATE["received"]:
            return FakeResponse(text="<html><body>処理済みのリンクです</body></html>")
        return FakeResponse(text=_link_page(link))
    if url.endswith("/primary_wallet"):
        return FakeResponse({
            "code": 200,
            "result": {"data": {
                "uuid": "WALLET-UUID",
                "balance": {"amount": STATE["wallet"],
                            "amountBreakdown": {"kyashMoney": STATE["wallet"], "kyashValue": 0}},
                "pointBalance": {"availableAmount": 0},
            }},
        })
    if "/timeline" in url:
        return FakeResponse({"code": 200, "result": {"data": {"timelines": STATE["history"]}}})
    if "/v1/links/" in url:
        return FakeResponse({"code": 200, "result": {"data": {
            "target": {"publicId": "pub", "userName": "sender"}}}})
    if url.endswith("/v1/me"):
        return FakeResponse({"code": 200, "result": {"data": {
            "userName": "bot_receiver", "imageUrl": "", "lastNameReal": "テスト",
            "firstNameReal": "アカウント", "phoneNumber": "000", "kyc": True}}})
    raise AssertionError(f"unexpected GET {url}")


def fake_put(url: str, **kwargs):
    assert url.endswith("/receive"), url
    link_uuid = url.split("/v1/links/")[1].rsplit("/receive", 1)[0]
    mode = STATE["receive_mode"].get(link_uuid, "ok")
    if mode == "timeout":
        raise requests.exceptions.ReadTimeout("read timeout")
    if mode == "reject":
        return FakeResponse({"code": 400, "error": {"message": "このリンクは既に受け取られています"}})
    amount = next(
        (v["amount"] for v in STATE["links"].values() if v["uuid"] == link_uuid), 0
    )
    if mode == "silent":
        # 受取APIは成功を返すが、実際には反映されない (痕跡なし) ケース
        return FakeResponse({"code": 200, "result": {"data": {"ok": True}}})
    STATE["received"].add(link_uuid)
    STATE["wallet"] += amount
    STATE["history"].insert(0, {"id": f"tl-{link_uuid}", "linkUuid": link_uuid, "amount": amount})
    return FakeResponse({"code": 200, "result": {"data": {"ok": True}}})


requests.get = fake_get
requests.put = fake_put


# ---------------------------------------------------------------------------
# Discord I/O のスタブ
# ---------------------------------------------------------------------------
class StubBotMixin:
    """Discord への送信を行わないスタブ。"""

    def install(self) -> None:
        self.sent_dms: list[tuple[int, str]] = []
        self.owner_alerts: list[str] = []

        async def fake_send_dm(user_id, embed, *, tx_id=None, queue_on_failure=True):
            self.sent_dms.append((user_id, embed.title or ""))
            return True

        async def fake_alert_owner(message):
            self.owner_alerts.append(message)

        self.charge._send_dm = fake_send_dm  # type: ignore[assignment]
        self.alert_owner = fake_alert_owner  # type: ignore[assignment]


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    class TestBot(main_module.ChargeBot, StubBotMixin):
        pass

    bot = TestBot()
    bot.install()
    await bot.db.connect()

    GUILD = 1000
    OTHER_GUILD = 2000
    await bot.db.set_guild_permission(GUILD, "ALLOWED", 1)
    await bot.db.set_guild_permission(OTHER_GUILD, "ALLOWED", 1)
    await bot.db.update_settings(GUILD, charge_rate="130", minimum_charge=100,
                                 maximum_charge=50_000, daily_limit=100_000)

    # Kyash セッションを有効化 (トークンのみでクライアント生成 → 通信なし)
    client = kyash_service.Kyash(access_token="test-token")
    bot.kyash._client = client
    bot.kyash._status = config.KyashAccountStatus.ACTIVE
    bot.kyash._username = "bot_receiver"

    print("\n=== 1. 金額検証 ===")
    for raw, label in (("50", "最低額未満"), ("60000", "最大額超過"), ("abc", "数値以外"),
                       ("-100", "負数"), ("0", "ゼロ"), ("9999999999", "桁数過大")):
        try:
            await bot.charge.start_charge(GUILD, 9001, raw)
            check(False, f"{label} が拒否されない")
        except ChargeError as exc:
            check(True, f"{label} を拒否 ({exc.code})")
    bot.charge._charge_rate_limiter.reset(f"charge:{GUILD}:9001")

    print("\n=== 2. 正常チャージ (130% / 1000円 → 1300) ===")
    tx1, amount, settings = await bot.charge.start_charge(GUILD, 101, "1000")
    check(amount == 1000, f"金額を受理 tx={tx1}")
    url1 = add_link("LINK1", 1000, "UUID-1")
    await bot.charge.submit_link(tx1, url1, 101)
    row = await bot.db.get_transaction(tx1)
    check(row["status"] == config.TxStatus.QUEUED, "リンク検証後に QUEUED")
    check(row["link_uuid"] == "UUID-1" and row["link_hash"], "link_uuid と link_hash を保存")
    check(row["received_amount"] == 1000, "受取予定額を保存")

    processed = await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx1)
    check(processed and row["status"] == config.TxStatus.COMPLETED, "キュー処理で COMPLETED")
    check(row["credited_amount"] == 1300, f"チャージ率適用 credited={row['credited_amount']}")
    check(await bot.db.get_balance(GUILD, 101) == 1300, "内部残高 1300")
    check(row["balance_before"] == 0 and row["balance_after"] == 1300, "残高推移を記録")
    check("UUID-1" in STATE["received"], "Kyash 側で受取済み")

    print("\n=== 3. 二重付与の防止 ===")
    before = await bot.db.get_balance(GUILD, 101)
    result = await bot.db.credit_transaction(tx1, 1300)
    check(result["already_credited"] and await bot.db.get_balance(GUILD, 101) == before,
          "同一Transactionの再付与を拒否 (残高不変)")

    print("\n=== 4. 同じリンクの再利用を拒否 ===")
    tx2, _, _ = await bot.charge.start_charge(GUILD, 102, "1000")
    try:
        await bot.charge.submit_link(tx2, url1, 102)
        check(False, "同一リンクの別ユーザー利用が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.LINK_ALREADY_USED, f"別ユーザーの再利用を拒否 ({exc.code})")
    check((await bot.db.get_transaction(tx2))["status"] == config.TxStatus.FAILED, "再利用は FAILED")

    print("\n=== 5. 金額不一致 ===")
    tx3, _, _ = await bot.charge.start_charge(GUILD, 103, "1000")
    url_bad = add_link("LINK2", 500, "UUID-2")
    try:
        await bot.charge.submit_link(tx3, url_bad, 103)
        check(False, "金額不一致が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.AMOUNT_MISMATCH, f"金額不一致を拒否 ({exc.code})")
    row = await bot.db.get_transaction(tx3)
    check(row["status"] == config.TxStatus.FAILED and row["error_code"] == "AMOUNT_MISMATCH",
          "金額不一致で FAILED")
    check("UUID-2" not in STATE["received"], "金額不一致のリンクは受け取らない")

    print("\n=== 6. 請求リンク / 無効リンク ===")
    tx4, _, _ = await bot.charge.start_charge(GUILD, 104, "1000")
    url_claim = add_link("LINK3", 1000, "UUID-3", kind="request")
    try:
        await bot.charge.submit_link(tx4, url_claim, 104)
        check(False, "請求リンクが通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.LINK_IS_CLAIM, f"請求リンクを拒否 ({exc.code})")
    tx5, _, _ = await bot.charge.start_charge(GUILD, 105, "1000")
    try:
        await bot.charge.submit_link(tx5, "https://kyash.me/payments/UNKNOWN", 105)
        check(False, "無効リンクが通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INVALID_LINK, f"無効リンクを拒否 ({exc.code})")

    print("\n=== 7. 同時チャージ / アクティブ制限 ===")
    tx6, _, _ = await bot.charge.start_charge(GUILD, 106, "1000")
    try:
        await bot.charge.start_charge(GUILD, 106, "2000")
        check(False, "同一ユーザーの複数アクティブが通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ACTIVE_TRANSACTION_EXISTS,
              f"同一ユーザーの二重チャージを拒否 ({exc.code})")
    await bot.charge.cancel_transaction(tx6, 106)
    check((await bot.db.get_transaction(tx6))["status"] == config.TxStatus.CANCELLED,
          "キャンセルできる")

    print("\n=== 8. 同一リンクの同時送信 ===")
    url_race = add_link("LINK4", 2000, "UUID-4")
    txa, _, _ = await bot.charge.start_charge(GUILD, 107, "2000")
    txb, _, _ = await bot.charge.start_charge(GUILD, 108, "2000")
    results = await asyncio.gather(
        bot.charge.submit_link(txa, url_race, 107),
        bot.charge.submit_link(txb, url_race, 108),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, ChargeError)]
    check(len(successes) == 1 and len(failures) == 1,
          f"同時送信は1件のみ成功 (成功{len(successes)}/失敗{len(failures)})")
    check(failures and failures[0].code == config.ErrorCode.LINK_ALREADY_USED,
          "もう一方は LINK_ALREADY_USED")
    await bot.charge.process_queue_once()
    check(STATE["received"] == {"UUID-1", "UUID-4"}, "受取は1回だけ実行された")

    print("\n=== 9. タイムアウト → 未受取確認 → 再試行 ===")
    url_to = add_link("LINK5", 3000, "UUID-5")
    STATE["receive_mode"]["UUID-5"] = "timeout"
    tx7, _, _ = await bot.charge.start_charge(GUILD, 109, "3000")
    await bot.charge.submit_link(tx7, url_to, 109)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx7)
    check(row["status"] == config.TxStatus.QUEUED and row["retry_count"] == 1,
          f"タイムアウト後に再試行待ち (status={row['status']}, retry={row['retry_count']})")
    check("UUID-5" not in STATE["received"], "タイムアウト時に二重受取しない")
    STATE["receive_mode"]["UUID-5"] = "ok"
    await bot.db.mark_queue_attempt(tx7, attempts=1, next_attempt_at=utils.now_ts(), last_error=None)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx7)
    check(row["status"] == config.TxStatus.COMPLETED and row["credited_amount"] == 3900,
          f"再試行で完了 (credited={row['credited_amount']})")

    print("\n=== 10. 受取APIが成功を返すが痕跡がない → MANUAL_REVIEW ===")
    url_silent = add_link("LINK6", 1500, "UUID-6")
    STATE["receive_mode"]["UUID-6"] = "silent"
    tx8, _, _ = await bot.charge.start_charge(GUILD, 110, "1500")
    await bot.charge.submit_link(tx8, url_silent, 110)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx8)
    check(row["status"] == config.TxStatus.MANUAL_REVIEW,
          f"確認できない場合は MANUAL_REVIEW (status={row['status']})")
    check(await bot.db.get_balance(GUILD, 110) == 0, "確認できない場合は残高を付与しない")

    print("\n=== 11. 受取拒否 (使用済み) ===")
    url_rej = add_link("LINK7", 1200, "UUID-7")
    STATE["receive_mode"]["UUID-7"] = "reject"
    tx9, _, _ = await bot.charge.start_charge(GUILD, 111, "1200")
    await bot.charge.submit_link(tx9, url_rej, 111)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx9)
    check(row["status"] == config.TxStatus.FAILED and row["error_code"] == "LINK_ALREADY_USED",
          f"受取拒否で FAILED (code={row['error_code']})")
    check(await bot.db.get_balance(GUILD, 111) == 0, "受取拒否では残高を付与しない")

    print("\n=== 12. 再起動復旧 ===")
    # (a) 受取成功後にクラッシュ (PROCESSING のまま) → 履歴で確認して付与
    url_crash = add_link("LINK8", 800, "UUID-8")
    tx10, _, _ = await bot.charge.start_charge(GUILD, 112, "800")
    await bot.charge.submit_link(tx10, url_crash, 112)
    await bot.db.transition_status(tx10, config.TxStatus.PROCESSING,
                                   expected=(config.TxStatus.QUEUED,),
                                   wallet_before=STATE["wallet"])
    STATE["received"].add("UUID-8")   # 実際には受取成功していた
    STATE["wallet"] += 800
    STATE["history"].insert(0, {"id": "tl-8", "linkUuid": "UUID-8", "amount": 800})
    # (b) 受取していないまま PROCESSING でクラッシュ → MANUAL_REVIEW
    url_crash2 = add_link("LINK9", 900, "UUID-9")
    tx11, _, _ = await bot.charge.start_charge(GUILD, 113, "900")
    await bot.charge.submit_link(tx11, url_crash2, 113)
    await bot.db.transition_status(tx11, config.TxStatus.PROCESSING,
                                   expected=(config.TxStatus.QUEUED,),
                                   wallet_before=STATE["wallet"])
    summary = await bot.charge.recover_pending_transactions()
    row10 = await bot.db.get_transaction(tx10)
    row11 = await bot.db.get_transaction(tx11)
    check(row10["status"] == config.TxStatus.COMPLETED and row10["credited_amount"] == 1040,
          f"受取済みだった取引を復旧して付与 (status={row10['status']})")
    check(row11["status"] == config.TxStatus.MANUAL_REVIEW,
          f"不明な取引は MANUAL_REVIEW (status={row11['status']})")
    check("UUID-9" not in STATE["received"], "復旧時に再受取しない")
    print(f"       復旧サマリ: {summary}")

    print("\n=== 13. MANUAL_REVIEW の管理者確定 ===")
    verification = await bot.charge.verify_transaction(tx11)
    check(verification.verdict == kyash_service.Verdict.NO_EVIDENCE,
          f"未受取と判定 ({verification.verdict})")
    await bot.charge.requeue_manual_review(tx11, operator_id=1)
    check((await bot.db.get_transaction(tx11))["status"] == config.TxStatus.QUEUED,
          "未受取確認済みなら再キュー可能")
    await bot.charge.process_queue_once()
    check((await bot.db.get_transaction(tx11))["status"] == config.TxStatus.COMPLETED,
          "再キュー後に完了")
    await bot.charge.resolve_manual_review(
        tx8, complete=False, operator_id=1, reason="Kyashアプリで未受取を確認")
    check((await bot.db.get_transaction(tx8))["status"] == config.TxStatus.FAILED,
          "管理者判断で失敗確定できる")

    print("\n=== 14. 日次上限 / 凍結 / メンテナンス / 緊急停止 ===")
    await bot.db.update_settings(GUILD, daily_limit=1000)
    try:
        await bot.charge.start_charge(GUILD, 101, "1000")  # 既に1000円利用済み
        check(False, "日次上限が効かない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.DAILY_LIMIT_EXCEEDED, f"日次上限を超過で拒否 ({exc.code})")
    await bot.db.update_settings(GUILD, daily_limit=100_000)

    await bot.db.set_frozen(GUILD, 120, True, 1, "テスト凍結")
    try:
        await bot.charge.start_charge(GUILD, 120, "1000")
        check(False, "凍結ユーザーがチャージできてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.USER_FROZEN, f"凍結ユーザーを拒否 ({exc.code})")

    await bot.db.update_settings(GUILD, maintenance=1)
    try:
        await bot.charge.start_charge(GUILD, 121, "1000")
        check(False, "メンテナンス中にチャージできてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.MAINTENANCE, f"メンテナンス中を拒否 ({exc.code})")
    await bot.db.update_settings(GUILD, maintenance=0, emergency_stop=1)
    try:
        await bot.charge.start_charge(GUILD, 122, "1000")
        check(False, "緊急停止中にチャージできてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.EMERGENCY_STOP, f"緊急停止中を拒否 ({exc.code})")

    # 緊急停止中はキュー処理も保留される
    url_es = add_link("LINK10", 1000, "UUID-10")
    await bot.db.update_settings(GUILD, emergency_stop=0)
    tx12, _, _ = await bot.charge.start_charge(GUILD, 123, "1000")
    await bot.charge.submit_link(tx12, url_es, 123)
    await bot.db.update_settings(GUILD, emergency_stop=1)
    await bot.charge.process_queue_once()
    check((await bot.db.get_transaction(tx12))["status"] == config.TxStatus.QUEUED,
          "緊急停止中は受取を保留 (QUEUED のまま)")
    check("UUID-10" not in STATE["received"], "緊急停止中は受取しない")
    await bot.db.update_settings(GUILD, emergency_stop=0)
    await bot.db.mark_queue_attempt(tx12, attempts=0, next_attempt_at=utils.now_ts(), last_error=None)
    await bot.charge.process_queue_once()
    check((await bot.db.get_transaction(tx12))["status"] == config.TxStatus.COMPLETED,
          "緊急停止解除後に処理される")

    print("\n=== 15. 未許可サーバー / サーバー分離 ===")
    await bot.db.set_guild_permission(3000, "DENIED", 1)
    try:
        await bot.charge.start_charge(3000, 130, "1000")
        check(False, "未許可サーバーで利用できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.GUILD_DISABLED, f"未許可サーバーを拒否 ({exc.code})")
    await bot.charge.admin_adjust_balance(
        guild_id=OTHER_GUILD, user_id=101, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=99_999, operator_id=1, reason="別サーバー")
    check(await bot.db.get_balance(GUILD, 101) == 1300, "別サーバーの残高が混ざらない")
    check(await bot.db.get_balance(OTHER_GUILD, 101) == 99_999, "別サーバーの残高は独立")

    print("\n=== 16. 期限切れ ===")
    tx13, _, _ = await bot.charge.start_charge(GUILD, 140, "1000")
    await bot.db.transition_status(tx13, config.TxStatus.WAITING_LINK,
                                   expires_at=utils.now_ts() - 10)
    await bot.charge.expire_transactions()
    check((await bot.db.get_transaction(tx13))["status"] == config.TxStatus.EXPIRED,
          "期限切れを EXPIRED にする")
    url_exp = add_link("LINK11", 1000, "UUID-11")
    try:
        await bot.charge.submit_link(tx13, url_exp, 140)
        check(False, "期限切れ取引にリンクを登録できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.TRANSACTION_EXPIRED,
              f"期限切れ取引のリンク登録を拒否 ({exc.code})")
    check("UUID-11" not in STATE["received"], "期限切れ取引では受取しない")

    print("\n=== 17. 管理者残高操作 / ランキング ===")
    await bot.charge.admin_adjust_balance(
        guild_id=GUILD, user_id=201, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=15_000, operator_id=1, reason="加算テスト")
    await bot.charge.admin_adjust_balance(
        guild_id=GUILD, user_id=202, change_type=config.BalanceChangeType.ADMIN_SET,
        amount=12_400, operator_id=1, reason="設定テスト")
    await bot.charge.admin_adjust_balance(
        guild_id=GUILD, user_id=203, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=15_000, operator_id=1, reason="同額テスト")
    await bot.charge.admin_adjust_balance(
        guild_id=GUILD, user_id=201, change_type=config.BalanceChangeType.ADMIN_REMOVE,
        amount=100_000, operator_id=1, reason="減算テスト (0未満にしない)")
    check(await bot.db.get_balance(GUILD, 201) == 0, "減算で負の残高にならない")
    settings = await bot.db.get_settings(GUILD)
    entries = await bot.charge.build_ranking_entries(GUILD, settings)
    check(entries[0][1] >= entries[-1][1], "ランキングは残高降順")
    ids = [e[0] for e in entries if e[1] == 15_000]
    check(ids == sorted(ids), f"同額はuser_id昇順 ({ids})")
    signature = bot.charge.ranking_signature(entries)
    check(signature == bot.charge.ranking_signature(
        await bot.charge.build_ranking_entries(GUILD, settings)),
        "内容が同じなら署名も同じ (無駄な編集をしない)")
    await bot.charge.admin_adjust_balance(
        guild_id=GUILD, user_id=120, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=50_000, operator_id=1, reason="凍結ユーザー")
    entries2 = await bot.charge.build_ranking_entries(GUILD, settings)
    check(not any(e[0] == 120 for e in entries2), "凍結ユーザーはランキング対象外")

    print("\n=== 18. 代理実績 ===")
    proxy = await bot.charge.create_proxy_achievement(
        guild_id=GUILD, user_id=301, amount=1000, charge_rate=Decimal("130"),
        credited_amount=None, operator_id=1, reason="手動確認済みの取引")
    check(proxy["credited_amount"] == 1300 and await bot.db.get_balance(GUILD, 301) == 1300,
          "代理実績で残高付与")
    proxy_row = await bot.db.get_transaction(proxy["transaction_id"])
    check(proxy_row["source"] == config.TxSource.ADMIN_PROXY and
          proxy_row["status"] == config.TxStatus.COMPLETED,
          "代理実績は source=ADMIN_PROXY で区別")
    logs, _ = await bot.db.list_audit_logs(guild_id=GUILD, action="ACHIEVEMENT_PROXY")
    check(bool(logs), "代理実績が監査ログに記録される")

    print("\n=== 19. 端数処理 / チャージ率のスナップショット ===")
    check(utils.calc_credited_amount(101, Decimal("130")) == 131, "101×130% = 131 (四捨五入)")
    check(utils.calc_credited_amount(1000, Decimal("130.5")) == 1305, "1000×130.5% = 1305")
    tx14, _, _ = await bot.charge.start_charge(GUILD, 401, "1000")
    await bot.db.update_settings(GUILD, charge_rate="200")   # 処理中に率を変更
    url_rate = add_link("LINK12", 1000, "UUID-12")
    await bot.charge.submit_link(tx14, url_rate, 401)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx14)
    check(row["credited_amount"] == 1300,
          f"開始時のチャージ率を適用 (credited={row['credited_amount']}, 現在率=200%)")
    await bot.db.update_settings(GUILD, charge_rate="130")

    print("\n=== 20. 整合性 / 統計 / バックアップ / 通知キュー ===")
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["completed_without_history"], "完了取引に必ず残高履歴がある")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高が存在しない")
    stats = await bot.db.get_statistics(GUILD)
    check(stats["success"] >= 6 and stats["credited"] > 0, f"統計を集計 {stats['success']}件成功")
    backup = await bot.tasks.run_backup()
    check(backup.exists() and backup.stat().st_size > 0, f"バックアップ作成 {backup.name}")
    await bot.db.enqueue_notification(kind="DM_RESULT", payload={"tx_id": tx1},
                                      transaction_id=tx1, user_id=101, delay=-10)
    processed_notifications = await bot.charge.process_notification_queue()
    check(processed_notifications >= 1, "通知キューを再送処理")

    print("\n=== 21. レート制限 ===")
    limited = False
    for i in range(config.RATE_LIMIT_CHARGE_COUNT + 2):
        try:
            tx, _, _ = await bot.charge.start_charge(GUILD, 500 + i, "1000")
        except ChargeError as exc:
            if exc.code == config.ErrorCode.RATE_LIMITED:
                limited = True
                break
    check(not limited, "ユーザー単位のレート制限は他ユーザーへ影響しない")
    bot.charge._charge_rate_limiter.reset(f"charge:{GUILD}:601")
    limited = False
    for _ in range(config.RATE_LIMIT_CHARGE_COUNT + 2):
        try:
            await bot.charge.start_charge(GUILD, 601, "1000")
        except ChargeError as exc:
            if exc.code == config.ErrorCode.RATE_LIMITED:
                limited = True
                break
    check(limited, "同一ユーザーの連続操作はレート制限される")

    print("\n=== 22. 秘密情報の取り扱い ===")
    masked = utils.sanitize_for_log(
        "link https://kyash.me/payments/LINK1 url: /payments/LINK1 "
        "password=hunter2 otp=123456 X-Auth: abcdefghijklmnopqrstuvwxyz0123456789ABCD"
    )
    check("LINK1" not in masked, "ログで送金リンクIDがマスクされる")
    check("hunter2" not in masked and "123456" not in masked, "パスワード/OTPがマスクされる")
    check("abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in masked, "トークン風文字列がマスクされる")
    # 正規化: 表記揺れでも同じハッシュになる
    variants = [
        "https://kyash.me/payments/LINK1",
        "http://kyash.me/payments/LINK1?utm=1",
        "kyash.me/payments/LINK1/",
        "  受け取って https://kyash.me/payments/LINK1#frag  ",
        "LINK1",
    ]
    hashes = {utils.link_hash(utils.normalize_kyash_link(v)[1]) for v in variants}
    check(len(hashes) == 1, f"URL表記揺れを正規化して同一ハッシュにする ({len(hashes)}種)")
    # DB に完全なURL(リンクID)が保存されていないこと
    dump = "\n".join(await bot.db.run(lambda c: list(c.iterdump())))
    check("payments/" not in dump, "DBに送金リンクURLが保存されていない")
    check("LINK1" not in dump, "DBに送金リンクIDが保存されていない")
    check("UUID-1" in dump, "受取・状態確認に必要なリンクUUIDのみ保存されている")
    account = await bot.db.get_kyash_account()
    check(account.access_token_enc is None or "test-token" not in str(account.access_token_enc),
          "アクセストークンが平文で保存されない")

    print("\n=== 23. コマンドツリー ===")
    from commands import setup_commands

    await setup_commands(bot)
    names = sorted(c.name for c in bot.tree.get_commands())
    expected = {
        "setup", "charge_panel", "charge_panels", "ranking_panel", "ranking_panels",
        "kyash", "server", "settings", "balance", "user", "history", "stats", "queue",
        "system", "logs", "maintenance", "emergency_stop", "achievement", "transaction",
        "config", "data", "backup",
    }
    missing = expected - set(names)
    check(not missing, f"必要なコマンドが揃っている (不足: {missing or 'なし'})")
    total_subcommands = sum(
        len(c.commands) if hasattr(c, "commands") else 1 for c in bot.tree.get_commands()
    )
    print(f"       登録コマンド: {len(names)} (サブコマンド含め {total_subcommands})")

    print("\n=== 24. 再起動後の永続化 ===")
    balance_before_restart = await bot.db.get_balance(GUILD, 101)
    await bot.charge.shutdown()      # 保留中のランキング更新を破棄してから閉じる
    await bot.db.close()
    import database

    db2 = database.Database(config.DB_PATH)
    await db2.connect()
    check(await db2.get_balance(GUILD, 101) == balance_before_restart, "残高が再起動後も保持される")
    check(await db2.is_guild_allowed(GUILD), "許可サーバーが再起動後も保持される")
    settings2 = await db2.get_settings(GUILD)
    check(str(settings2.charge_rate) == "130", "設定が再起動後も保持される")
    await db2.close()
    await bot.kyash.shutdown()

    print("\n" + "=" * 70)
    print(f"結果: {len(PASS)} 件成功 / {len(FAIL)} 件失敗")
    if FAIL:
        for label in FAIL:
            print(f"  ✗ {label}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if FAIL else 0)
