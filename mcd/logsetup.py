"""構造化ログと秘匿情報マスク。

ログ出力の直前でトークン類を潰す。これを通さないと、例外の
スタックトレースに refresh_token や PASETO がそのまま残る。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

# 潰す対象。長いトークンほど先に判定されるよう順序に意味がある。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"v2\.local\.[A-Za-z0-9_\-\.]+"), "v2.local.<PASETO>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "<JWT>"),
    (re.compile(r"(?i)(refresh[_-]?token\"?\s*[:=]\s*\"?)[A-Za-z0-9_\-\.]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(access[_-]?token\"?\s*[:=]\s*\"?)[A-Za-z0-9_\-\.]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(x-auth\"?\s*[:=]\s*\"?)[A-Za-z0-9_\-\.]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?(?:bearer\s+)?)[A-Za-z0-9_\-\.]{12,}"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(security_code\"?\s*[:=]\s*\"?)\d{3,4}"), r"\1<REDACTED>"),
    (re.compile(r"\b(?:\d[ \-]?){13,19}\b"), "<CARD>"),
    (re.compile(r"(?i)(bot\s+)?[MNO][A-Za-z0-9_\-]{22,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"), "<DISCORD_TOKEN>"),
]


def mask(text: Any) -> str:
    s = text if isinstance(text, str) else str(text)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def new_trace(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: mask(v) for k, v in record.args.items()}
            else:
                record.args = tuple(mask(a) for a in record.args)
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, JST).isoformat(timespec="seconds"),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("trace", "evt", "user", "order", "account", "ms", "extra"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = mask(self.formatException(record.exc_info))
        return mask(json.dumps(payload, ensure_ascii=False, default=str))


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        return mask(base)


def setup(log_dir: str | Path, level: int = logging.INFO, keep_days: int = 14) -> logging.Logger:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    masking = MaskingFilter()

    console = logging.StreamHandler()
    console.setFormatter(
        PlainFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    console.addFilter(masking)
    root.addHandler(console)

    jsonl = logging.handlers.TimedRotatingFileHandler(
        directory / "bot.jsonl", when="midnight", backupCount=keep_days, encoding="utf-8"
    )
    jsonl.setFormatter(JsonFormatter())
    jsonl.addFilter(masking)
    root.addHandler(jsonl)

    errors = logging.handlers.TimedRotatingFileHandler(
        directory / "error.jsonl", when="midnight", backupCount=keep_days, encoding="utf-8"
    )
    errors.setLevel(logging.WARNING)
    errors.setFormatter(JsonFormatter())
    errors.addFilter(masking)
    root.addHandler(errors)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger("bot")


def event(logger: logging.Logger, evt: str, level: int = logging.INFO, **fields: Any) -> None:
    """構造化イベントを1行出す。

    ``event(log, "order.paid", trace=tid, user=uid, ms=1234)`` のように使う。
    """
    known = {k: fields.pop(k) for k in ("trace", "user", "order", "account", "ms") if k in fields}
    extra = dict(known)
    extra["evt"] = evt
    if fields:
        extra["extra"] = fields
    logger.log(level, evt, extra=extra)
