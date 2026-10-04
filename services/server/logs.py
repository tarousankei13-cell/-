"""
出来事の記録（サーバーログ）

入退室・ロール変更・削除されたメッセージなどを、決めたチャンネルへ流す。

⚠️ **記録先のチャンネル自身の出来事は記録しない。**
   記録先で「メッセージが消された」を記録すると、それがまた記録を生み、
   無限に増え続ける。ここで必ず弾くこと。

⚠️ 権限が無いときは**1度だけ**警告を出して、以降は黙る。
   毎回出すと、15分ごとの同期ログに混ざって読めなくなる。
"""

from __future__ import annotations

import logging

import discord

from core import settings

log = logging.getLogger("bot.server.logs")

# 記録できる出来事 → 画面に出す呼び名
EVENTS: dict[str, str] = {
    "join": "入室",
    "leave": "退室",
    "ban": "BAN・BAN解除",
    "role": "ロールの変更",
    "nick": "表示名の変更",
    "timeout": "発言停止",
    "msgdelete": "メッセージの削除",
    "msgedit": "メッセージの編集",
    "channel": "チャンネルの作成・削除",
    "voice": "ボイスチャンネルの入退室",
}

# 内容を読む権限（MESSAGE CONTENT INTENT）が無いと中身が空になるもの
NEEDS_CONTENT = frozenset({"msgdelete", "msgedit"})

# 権限不足を知らせたチャンネル。2度目以降は黙る。
_warned: set[int] = set()


def log_channel_id(guild_id: int | None = None) -> int | None:
    """記録先のチャンネルID。未設定なら None。"""
    cid = settings.get("guard_log_channel")
    return int(cid) if cid else None


def mod_channel_id() -> int | None:
    """
    処分の記録先。

    未設定なら、監視のログと同じ場所へ送る（設定を増やさないため）。
    """
    cid = settings.get("mod_log_channel")
    if cid:
        return int(cid)
    return log_channel_id()


def enabled(event: str) -> bool:
    """その出来事を記録する設定になっているか。"""
    chosen = settings.get("guard_events") or []
    return event in chosen


def is_log_channel(channel_id: int | None) -> bool:
    """
    記録先そのものか。

    ⚠️ ここが True のときは記録してはいけない（記録が記録を呼ぶ）。
    """
    if channel_id is None:
        return False
    return channel_id in {log_channel_id(), mod_channel_id()}


async def send(
    bot: discord.Client,
    embed: discord.Embed,
    *,
    event: str | None = None,
    to_mod: bool = False,
    content: str | None = None,
) -> bool:
    """
    記録を送る。送れたら True。

    event を渡すと、その出来事が設定で選ばれているときだけ送る。
    to_mod=True なら処分の記録先へ送る。
    """
    if event is not None and not enabled(event):
        return False

    cid = mod_channel_id() if to_mod else log_channel_id()
    if not cid:
        return False

    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden):
            if cid not in _warned:
                _warned.add(cid)
                log.warning(
                    "記録先のチャンネル（%s）が見つかりません。"
                    "消されたか、BOTから見えない場所にあります。", cid,
                )
            return False
        except discord.HTTPException:
            return False

    try:
        await channel.send(content=content, embed=embed)
        return True
    except discord.Forbidden:
        if cid not in _warned:
            _warned.add(cid)
            log.warning(
                "記録先のチャンネル（%s）に書き込めません。"
                "BOTに「メッセージを送信」と「埋め込みリンク」の権限を"
                "与えてください。", cid,
            )
        return False
    except discord.HTTPException as e:
        log.warning("記録を送れませんでした: %s", e)
        return False


def reset_warnings() -> None:
    """設定を変えたときに呼ぶ。警告を出し直せるようにする。"""
    _warned.clear()


def who(user: discord.abc.User | discord.Member | None) -> str:
    """誰の出来事か。消えた利用者でも落ちないようにする。"""
    if user is None:
        return "（不明）"
    return f"{user.mention}　`{user}`（`{user.id}`）"


def trim(text: str | None, limit: int = 1000) -> str:
    """
    埋め込みに入る長さに切る。

    ⚠️ 1フィールド1024文字が上限。超えると送信そのものが失敗する。
    """
    if not text:
        return "（なし）"
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "…"
