"""コマンドから変更できる設定。

main.py に直書きするのは Discord トークンとオーナーID、
それと DB を開く前に必要な起動パス（DB_PATH / LOG_DIR）だけ。
それ以外はすべてここで定義し、値は SQLite に保存する。

cog 側は今までどおり ``self.bot.cfg.PANEL_CHANNEL_ID`` の形で読める。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .store import Store

log = logging.getLogger("bot.settings")

KV_PREFIX = "cfg:"

DEFAULT_TERMS = """\
**ご利用にあたって**

1. チャージした残高は **返金・払い戻しできません**。使い切りです。
2. 注文は実際にマクドナルドへ発注されます。確定後の取り消しはできません。
3. 注文後は感想の投稿が必要です。投稿と承認が完了するまで次の注文はできません。
4. 他人の画像の転用、複数アカウントの使い分け、紹介制度の自作自演を禁止します。
5. 上記に違反した場合、残高を没収し利用を停止することがあります。

同意いただける場合のみ「同意して始める」を押してください。
"""


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str          # channel / role / guild / int / str / text / rate
    default: Any
    label: str
    group: str
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    hint: str = ""


def _spec(*args, **kwargs) -> Spec:
    return Spec(*args, **kwargs)


SPECS: dict[str, Spec] = {
    s.key: s
    for s in [
        # ---- チャンネル・ロール ----
        _spec("GUILD_ID", "guild", 0, "コマンドを即反映させるサーバー", "チャンネル",
              hint="0 ならグローバル同期（反映に最大1時間）"),
        _spec("ORDER_ROLE_ID", "role", 0, "注文できるロール", "チャンネル",
              hint="0 なら全員が注文できてしまうので必ず設定してください"),
        _spec("PANEL_CHANNEL_ID", "channel", 0, "パネルを置くチャンネル", "チャンネル"),
        _spec("ACHIEVEMENT_CHANNEL_ID", "channel", 0, "実績の投稿先", "チャンネル"),
        _spec("APPROVAL_CHANNEL_ID", "channel", 0, "承認待ちの投稿先", "チャンネル"),
        _spec("LOG_ORDERS_CHANNEL_ID", "channel", 0, "注文ログ", "チャンネル"),
        _spec("LOG_MONEY_CHANNEL_ID", "channel", 0, "入出金ログ", "チャンネル"),
        _spec("LOG_ERRORS_CHANNEL_ID", "channel", 0, "エラーログ", "チャンネル"),
        _spec("LOG_ADMIN_CHANNEL_ID", "channel", 0, "管理操作ログ", "チャンネル"),

        # ---- 料率 ----
        _spec("DEFAULT_USER_RATE", "rate", 60, "既定の利用者負担率(%)", "料率",
              0, 100, "利用者が定価の何%を払うか。小さいほど利用者が得をする"),

        # ---- 金額 ----
        _spec("ORDER_FACE_LIMIT", "int", 3000, "オーナー承認が必要になる定価(円)", "金額", 0, 1000000),
        _spec("APPROVAL_TIMEOUT_MINUTES", "int", 30, "承認依頼の有効期限(分)", "金額", 1, 1440),
        _spec("MIN_CHARGE", "int", 100, "1回のチャージ下限(円)", "金額", 1, 1000000),
        _spec("MAX_CHARGE", "int", 30000, "1回のチャージ上限(円)", "金額", 1, 1000000),
        _spec("PHOTO_BONUS", "int", 50, "実績に画像を添えた時のボーナス(円)", "金額", 0, 100000),
        _spec("REFERRAL_BONUS_INVITER", "int", 200, "紹介した人への報酬(円)", "金額", 0, 100000),
        _spec("REFERRAL_BONUS_INVITEE", "int", 200, "紹介された人への報酬(円)", "金額", 0, 100000),

        # ---- 規約 ----
        _spec("TERMS_VERSION", "str", "1.0", "規約の版", "規約",
              hint="変更すると全員に再同意を求めます"),
        _spec("TERMS_TEXT", "text", DEFAULT_TERMS, "規約の本文", "規約"),

        # ---- 不正検知 ----
        _spec("FRAUD_ACCOUNT_AGE_DAYS", "int", 30,
              "紹介: Discordアカウント作成からの必要日数", "不正検知", 0, 3650),
        _spec("FRAUD_GUILD_AGE_HOURS", "int", 24,
              "紹介: サーバー参加からの必要時間", "不正検知", 0, 8760),
        _spec("FRAUD_REFERRAL_MAX", "int", 20, "紹介: 1人が紹介できる上限", "不正検知", 1, 10000),
        _spec("FRAUD_REVIEW_SCORE", "int", 40, "オーナー承認へ回すスコア", "不正検知", 1, 100),
        _spec("FRAUD_SENDER_THRESHOLD", "int", 2,
              "同一Kyashを何人が使ったら警告するか", "不正検知", 1, 100),
        _spec("FRAUD_IMAGE_THRESHOLD", "int", 6,
              "画像の類似判定(小さいほど厳しい)", "不正検知", 0, 64),
        _spec("FRAUD_BURST_MINUTES", "int", 10, "連続注文を見る時間幅(分)", "不正検知", 1, 1440),
        _spec("FRAUD_BURST_MAX", "int", 3, "その時間幅で許す注文数", "不正検知", 1, 100),
        _spec("FRAUD_AMOUNT_ALERT", "int", 5000, "高額注文とみなす定価(円)", "不正検知", 0, 1000000),

        # ---- 動作 ----
        _spec("BRAND_NAME", "str", "Order Bot", "カードとEmbedに出る名前", "動作"),
        _spec("FONT_PATH", "str", "", "番号カードのフォントパス", "動作", hint="空なら自動検出"),
        _spec("BUZZER_POLL_SECONDS", "int", 30, "呼び出し番号を見に行く間隔(秒)", "動作", 5, 600),
        _spec("BUZZER_POLL_MAX", "int", 20, "呼び出し番号を見に行く回数", "動作", 0, 200),
        _spec("ORDER_TIMEOUT_SECONDS", "int", 120, "確認ボタンの有効時間(秒)", "動作", 10, 900),
        _spec("MONTHLY_REPORT_DAY", "int", 1, "月次レポートを送る日", "動作", 1, 28),
        _spec("MONTHLY_REPORT_HOUR", "int", 10, "月次レポートを送る時刻(JST)", "動作", 0, 23),
        _spec("HEALTH_CHECK_HOURS", "int", 6, "ヘルスチェックの間隔(時間)", "動作", 1, 168),
        _spec("KYASH_TOKEN_WARN_DAYS", "int", 7, "Kyashトークンの警告日数", "動作", 1, 30),
        _spec("CANCEL_GRACE_SECONDS", "int", 5, "決済を押した後の取り消し猶予(秒)", "動作", 0, 60,
              "0 で猶予なし。StoreOrder の前なので安全に取り消せる"),
        _spec("QUEUE_NOTIFY", "int", 1, "順番が近づいたらDMで知らせる(1で有効)", "動作", 0, 1),
        _spec("STORE_MISMATCH_WARN", "int", 1, "いつもと違う店舗を警告する(1で有効)", "動作", 0, 1),
        _spec("MAINTENANCE", "int", 0, "メンテナンスモード(1で注文停止)", "動作", 0, 1,
              "API障害時は自動で1になり、復旧すると自動で0に戻る"),
        _spec("MAINTENANCE_NOTE", "str", "", "メンテナンス中に表示する理由", "動作"),

        # ---- 保護・上限 ----
        _spec("ACCOUNT_DAILY_ORDERS", "int", 0,
              "1アカウントあたりの1日の注文件数上限", "保護", 0, 1000,
              "0 で無制限。超えたら次のアカウントへ回す"),
        _spec("ACCOUNT_DAILY_AMOUNT", "int", 0,
              "1アカウントあたりの1日の決済額上限(円)", "保護", 0, 10000000,
              "0 で無制限。カードの与信枠を超えないようにする"),
        _spec("CIRCUIT_FAIL_THRESHOLD", "int", 5,
              "連続失敗が何回でメンテナンスに入るか", "保護", 2, 50),
        _spec("CIRCUIT_COOLDOWN_MINUTES", "int", 15,
              "自動メンテナンスから復帰を試すまでの時間(分)", "保護", 1, 1440),
        _spec("BACKUP_KEEP", "int", 14, "残すバックアップの数", "保護", 1, 365),
        _spec("BACKUP_HOUR", "int", 4, "バックアップを取る時刻(JST)", "保護", 0, 23),
        _spec("DAILY_SUMMARY_HOUR", "int", 9, "日次サマリーを送る時刻(JST)", "保護", 0, 23),
        _spec("DORMANT_DAYS", "int", 90,
              "残高が動かない日数がこれを超えたら通知", "保護", 0, 3650, "0 で無効"),
        _spec("DORMANT_MIN_BALANCE", "int", 100, "通知の対象にする最低残高(円)", "保護", 1, 1000000),
        _spec("UNKNOWN_RECHECK_MINUTES", "int", 10,
              "決済成否不明の注文を照合しにいく間隔(分)", "保護", 1, 1440),
    ]
}

GROUPS = ["チャンネル", "料率", "金額", "規約", "不正検知", "動作", "保護"]

# 単独の値ではなく表で持つもの
TABLE_KEYS = ("ROLE_RATES", "USER_RATES", "CAMPAIGNS", "CHARGE_BONUS")

REQUIRED_KEYS = (
    "ORDER_ROLE_ID",
    "PANEL_CHANNEL_ID",
    "ACHIEVEMENT_CHANNEL_ID",
    "APPROVAL_CHANNEL_ID",
    "LOG_ORDERS_CHANNEL_ID",
    "LOG_MONEY_CHANNEL_ID",
    "LOG_ERRORS_CHANNEL_ID",
    "LOG_ADMIN_CHANNEL_ID",
)


class SettingError(ValueError):
    pass


class Settings:
    """DB に保存された設定を、属性アクセスで読めるようにする。

    ``cfg.PANEL_CHANNEL_ID`` のように読む。書き込みは :meth:`set` のみ。
    """

    def __init__(self, store: Store, constants: Optional[dict[str, Any]] = None):
        self._store = store
        self._constants = dict(constants or {})
        self._cache: dict[str, Any] = {}
        self._lock = threading.RLock()
        self.reload()

    # ------------------------------------------------------------ 読み込み

    def reload(self) -> None:
        with self._lock:
            cache: dict[str, Any] = {}
            for key, spec in SPECS.items():
                stored = self._store.get_kv(KV_PREFIX + key, None)
                cache[key] = spec.default if stored is None else stored
            list_tables = ("CAMPAIGNS", "CHARGE_BONUS")
            for key in TABLE_KEYS:
                stored = self._store.get_kv(KV_PREFIX + key, None)
                if stored is None:
                    stored = [] if key in list_tables else {}
                cache[key] = stored
            self._cache = cache

    def __getattr__(self, name: str) -> Any:
        # __init__ より前に呼ばれても壊れないようにする
        if name.startswith("_"):
            raise AttributeError(name)
        constants = self.__dict__.get("_constants", {})
        if name in constants:
            return constants[name]
        cache = self.__dict__.get("_cache", {})
        if name in cache:
            return cache[name]
        raise AttributeError(f"未定義の設定です: {name}")

    def get(self, key: str) -> Any:
        return getattr(self, key)

    def all_of(self, group: str) -> list[tuple[Spec, Any]]:
        return [
            (spec, self._cache.get(key, spec.default))
            for key, spec in SPECS.items()
            if spec.group == group
        ]

    def is_default(self, key: str) -> bool:
        spec = SPECS.get(key)
        if spec is None:
            return False
        return self._cache.get(key) == spec.default

    def missing_required(self) -> list[Spec]:
        return [SPECS[k] for k in REQUIRED_KEYS if not self._cache.get(k)]

    # ------------------------------------------------------------ 書き込み

    def set(self, key: str, raw: Any) -> Any:
        spec = SPECS.get(key)
        if spec is None:
            raise SettingError(f"設定 `{key}` は存在しません")
        value = self._coerce(spec, raw)
        with self._lock:
            self._store.set_kv(KV_PREFIX + key, value)
            self._cache[key] = value
        log.info("設定を変更しました: %s", key)
        return value

    def reset(self, key: str) -> Any:
        spec = SPECS.get(key)
        if spec is None:
            raise SettingError(f"設定 `{key}` は存在しません")
        with self._lock:
            self._store.set_kv(KV_PREFIX + key, spec.default)
            self._cache[key] = spec.default
        return spec.default

    @staticmethod
    def _coerce(spec: Spec, raw: Any) -> Any:
        if spec.kind in ("channel", "role", "guild"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise SettingError(f"{spec.label}: 数値のIDを指定してください")
            if value < 0:
                raise SettingError(f"{spec.label}: 0 以上を指定してください")
            return value

        if spec.kind in ("int", "rate"):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise SettingError(f"{spec.label}: 数値を指定してください")
            if spec.minimum is not None and value < spec.minimum:
                raise SettingError(f"{spec.label}: {spec.minimum} 以上にしてください")
            if spec.maximum is not None and value > spec.maximum:
                raise SettingError(f"{spec.label}: {spec.maximum} 以下にしてください")
            return value

        text = str(raw)
        if spec.kind == "str" and len(text) > 200:
            raise SettingError(f"{spec.label}: 200 文字以内にしてください")
        if spec.kind == "text" and len(text) > 3500:
            raise SettingError(f"{spec.label}: 3500 文字以内にしてください")
        return text

    # -------------------------------------------------------- 表形式の設定

    def _set_table(self, key: str, value: Any) -> None:
        with self._lock:
            self._store.set_kv(KV_PREFIX + key, value)
            self._cache[key] = value

    def set_role_rate(self, role_id: int, rate: Optional[int]) -> dict[int, int]:
        table = dict(self.ROLE_RATES)
        if rate is None:
            table.pop(str(role_id), None)
            table.pop(role_id, None)
        else:
            if not 0 <= rate <= 100:
                raise SettingError("負担率は 0〜100 で指定してください")
            table[str(role_id)] = int(rate)
        self._set_table("ROLE_RATES", table)
        return self.role_rates()

    def set_user_rate(self, user_id: int, rate: Optional[int]) -> dict[int, int]:
        table = dict(self.USER_RATES)
        if rate is None:
            table.pop(str(user_id), None)
            table.pop(user_id, None)
        else:
            if not 0 <= rate <= 100:
                raise SettingError("負担率は 0〜100 で指定してください")
            table[str(user_id)] = int(rate)
        self._set_table("USER_RATES", table)
        return self.user_rates()

    def role_rates(self) -> dict[int, int]:
        """JSON のキーは文字列になるので int に戻して返す。"""
        return {int(k): int(v) for k, v in (self.ROLE_RATES or {}).items()}

    def user_rates(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in (self.USER_RATES or {}).items()}

    # ---- キャンペーン ----

    def campaigns(self) -> list:
        from .rates import Campaign

        out = []
        for item in self.CAMPAIGNS or []:
            try:
                out.append(
                    Campaign(
                        name=str(item["name"]),
                        rate=int(item["rate"]),
                        weekdays=tuple(int(d) for d in item.get("weekdays", range(7))),
                        start_hour=int(item.get("start_hour", 0)),
                        end_hour=int(item.get("end_hour", 24)),
                    )
                )
            except Exception:
                log.warning("壊れたキャンペーン設定を読み飛ばしました: %r", item)
        return out

    def add_campaign(
        self, name: str, rate: int, weekdays: tuple[int, ...], start_hour: int, end_hour: int
    ) -> list:
        if not name.strip():
            raise SettingError("キャンペーン名を入力してください")
        if not 0 <= rate <= 100:
            raise SettingError("負担率は 0〜100 で指定してください")
        if not 0 <= start_hour < end_hour <= 24:
            raise SettingError("時間帯は 0〜24 で、開始 < 終了 にしてください")
        if not weekdays or any(not 0 <= d <= 6 for d in weekdays):
            raise SettingError("曜日の指定が正しくありません")

        items = list(self.CAMPAIGNS or [])
        if any(str(i.get("name")) == name.strip() for i in items):
            raise SettingError(f"「{name.strip()}」は既に存在します")
        items.append(
            {
                "name": name.strip(),
                "rate": int(rate),
                "weekdays": sorted(set(int(d) for d in weekdays)),
                "start_hour": int(start_hour),
                "end_hour": int(end_hour),
            }
        )
        self._set_table("CAMPAIGNS", items)
        return self.campaigns()

    def remove_campaign(self, name: str) -> bool:
        items = list(self.CAMPAIGNS or [])
        remaining = [i for i in items if str(i.get("name")) != name]
        if len(remaining) == len(items):
            return False
        self._set_table("CAMPAIGNS", remaining)
        return True

    # ---- チャージボーナス ----

    def charge_bonus_tiers(self) -> list[tuple[int, int]]:
        """(チャージ額の下限, ボーナス率%) を額の大きい順で返す。"""
        rows = []
        for item in self.CHARGE_BONUS or []:
            try:
                rows.append((int(item["min"]), int(item["percent"])))
            except Exception:
                log.warning("壊れたチャージボーナス設定を読み飛ばしました: %r", item)
        return sorted(rows, key=lambda x: x[0], reverse=True)

    def charge_bonus_for(self, amount: int) -> tuple[int, int]:
        """(ボーナス額, 適用率%) を返す。該当なしなら (0, 0)。"""
        for minimum, percent in self.charge_bonus_tiers():
            if amount >= minimum:
                return (amount * percent) // 100, percent
        return 0, 0

    def add_charge_bonus(self, minimum: int, percent: int) -> list[tuple[int, int]]:
        if minimum < 1:
            raise SettingError("下限は 1 円以上にしてください")
        if not 1 <= percent <= 100:
            raise SettingError("ボーナス率は 1〜100% で指定してください")
        items = [
            i for i in (self.CHARGE_BONUS or []) if int(i.get("min", 0)) != minimum
        ]
        items.append({"min": int(minimum), "percent": int(percent)})
        self._set_table("CHARGE_BONUS", items)
        return self.charge_bonus_tiers()

    def remove_charge_bonus(self, minimum: int) -> bool:
        items = list(self.CHARGE_BONUS or [])
        remaining = [i for i in items if int(i.get("min", 0)) != minimum]
        if len(remaining) == len(items):
            return False
        self._set_table("CHARGE_BONUS", remaining)
        return True

    # ------------------------------------------------------------ 不正検知

    def fraud_config(self):
        from .fraud import FraudConfig

        return FraudConfig(
            referral_min_account_age_days=self.FRAUD_ACCOUNT_AGE_DAYS,
            referral_min_guild_age_hours=self.FRAUD_GUILD_AGE_HOURS,
            referral_max_per_user=self.FRAUD_REFERRAL_MAX,
            referral_review_score=self.FRAUD_REVIEW_SCORE,
            multi_account_sender_threshold=self.FRAUD_SENDER_THRESHOLD,
            image_hash_threshold=self.FRAUD_IMAGE_THRESHOLD,
            order_burst_window_minutes=self.FRAUD_BURST_MINUTES,
            order_burst_max=self.FRAUD_BURST_MAX,
            order_amount_alert=self.FRAUD_AMOUNT_ALERT,
        )
