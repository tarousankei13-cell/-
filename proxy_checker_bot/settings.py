"""コマンドから変更できる設定の保存・検証。

トークン以外の設定はすべてここで扱い、settings.json に永続化する。
設定はサーバー (ギルド) ごと、DM では実行者ごとに分かれる。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from checker import (
    DEFAULT_HTTPS_JUDGE_URL,
    DEFAULT_JUDGE_URL,
    parse_https_judge,
    parse_judge,
)

logger = logging.getLogger("proxybot.settings")

SETTINGS_PATH = Path(__file__).with_name("settings.json")


@dataclass(frozen=True)
class Option:
    """設定1項目の定義。検証にも表示にも同じ定義を使う。"""

    key: str
    label: str
    kind: str  # "float" | "int" | "bool" | "url"
    default: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""
    description: str = ""

    def format(self, value: Any) -> str:
        if self.kind == "bool":
            return "オン" if value else "オフ"
        if self.kind == "float":
            return f"{float(value):.1f}{self.unit}"
        if self.kind == "int":
            return f"{int(value)}{self.unit}"
        return str(value)

    @property
    def range_text(self) -> str:
        if self.kind == "bool":
            return "オン / オフ"
        if self.minimum is None or self.maximum is None:
            return "自由入力"
        if self.kind == "float":
            return f"{self.minimum:.1f}〜{self.maximum:.1f}{self.unit}"
        return f"{int(self.minimum)}〜{int(self.maximum)}{self.unit}"


OPTIONS: tuple[Option, ...] = (
    Option(
        key="timeout",
        label="タイムアウト",
        kind="float",
        default=8.0,
        minimum=1.0,
        maximum=30.0,
        unit="秒",
        description="プロキシ1台に待つ最大時間",
    ),
    Option(
        key="concurrency",
        label="同時実行数",
        kind="int",
        default=50,
        minimum=1,
        maximum=200,
        unit="台",
        description="並行して検査する台数。大きいほど速いが負荷も増える",
    ),
    Option(
        key="retries",
        label="再試行回数",
        kind="int",
        default=0,
        minimum=0,
        maximum=3,
        unit="回",
        description="失敗したときに追加で試す回数",
    ),
    Option(
        key="https_check",
        label="HTTPS判定",
        kind="bool",
        default=True,
        description="CONNECT メソッドで HTTPS 中継に対応しているかも調べる",
    ),
    Option(
        key="max_proxies",
        label="最大受付件数",
        kind="int",
        default=200,
        minimum=1,
        maximum=1000,
        unit="件",
        description="1回のコマンドで受け付ける上限。超えた分は切り捨て",
    ),
    Option(
        key="show_dead",
        label="失敗も表示",
        kind="bool",
        default=True,
        description="結果一覧に失敗したプロキシと理由も載せる",
    ),
    Option(
        key="attach_file",
        label="ファイル添付",
        kind="bool",
        default=True,
        description="生存プロキシの .txt を結果と一緒に自動添付する",
    ),
    Option(
        key="ephemeral",
        label="結果を非公開",
        kind="bool",
        default=False,
        description="結果を実行者だけに見えるメッセージで返す",
    ),
    Option(
        key="judge_url",
        label="判定用URL",
        kind="url",
        default=DEFAULT_JUDGE_URL,
        description="出口IPと国名の取得先 (http:// のみ)",
    ),
    Option(
        key="https_judge_url",
        label="判定用URL(CONNECT時)",
        kind="url_https",
        default=DEFAULT_HTTPS_JUDGE_URL,
        description="HTTP中継を拒否するプロキシをCONNECTで判定するときの取得先 (https:// のみ)",
    ),
)

OPTION_MAP: dict[str, Option] = {option.key: option for option in OPTIONS}
DEFAULTS: dict[str, Any] = {option.key: option.default for option in OPTIONS}


def _coerce(option: Option, value: Any) -> tuple[Any, Optional[str]]:
    """値を型に合わせ、範囲外なら丸める。戻り値は (値, 注意文)。"""
    if option.kind == "bool":
        return bool(value), None

    if option.kind in ("url", "url_https"):
        text = str(value).strip()
        # 不正なら ValueError が飛ぶ
        (parse_judge if option.kind == "url" else parse_https_judge)(text)
        return text, None

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{option.label} には数値を指定してください") from exc

    note = None
    if option.minimum is not None and number < option.minimum:
        number = option.minimum
        note = f"{option.label}は{option.range_text}が指定できる範囲なので、{option.format(number)}に丸めました"
    elif option.maximum is not None and number > option.maximum:
        number = option.maximum
        note = f"{option.label}は{option.range_text}が指定できる範囲なので、{option.format(number)}に丸めました"

    return (float(number) if option.kind == "float" else int(number)), note


class SettingsStore:
    """settings.json を読み書きする小さなストア。"""

    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # 永続化
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("設定の読み込みに失敗したので既定値で動かします: %s", exc)
            return
        if isinstance(raw, dict):
            self._data = {
                str(scope): dict(values)
                for scope, values in raw.items()
                if isinstance(values, dict)
            }

    def _save(self) -> None:
        temp = self._path.with_suffix(".json.tmp")
        try:
            temp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp.replace(self._path)
        except OSError as exc:
            logger.error("設定の保存に失敗しました: %s", exc)

    # ------------------------------------------------------------------ #
    # 参照 / 更新
    # ------------------------------------------------------------------ #

    def get(self, scope: str) -> dict[str, Any]:
        """既定値に保存済みの値を重ねたものを返す。"""
        with self._lock:
            stored = self._data.get(scope, {})
            merged = dict(DEFAULTS)
            for key, value in stored.items():
                if key in OPTION_MAP:
                    merged[key] = value
            return merged

    def customized_keys(self, scope: str) -> set[str]:
        with self._lock:
            stored = self._data.get(scope, {})
            return {key for key, value in stored.items() if key in OPTION_MAP and value != DEFAULTS[key]}

    def apply(self, scope: str, changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """変更を検証して保存する。戻り値は (反映した値, 注意文一覧)。"""
        applied: dict[str, Any] = {}
        notes: list[str] = []

        for key, value in changes.items():
            if value is None:
                continue
            option = OPTION_MAP.get(key)
            if option is None:
                notes.append(f"`{key}` という設定はありません")
                continue
            try:
                coerced, note = _coerce(option, value)
            except ValueError as exc:
                notes.append(f"{option.label}: {exc}")
                continue
            applied[key] = coerced
            if note:
                notes.append(note)

        if applied:
            with self._lock:
                self._data.setdefault(scope, {}).update(applied)
                self._save()

        return applied, notes

    def reset(self, scope: str) -> bool:
        """設定を既定値に戻す。変更があったかを返す。"""
        with self._lock:
            existed = bool(self._data.pop(scope, None))
            if existed:
                self._save()
            return existed
