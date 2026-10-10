"""
マクドナルドアカウントの管理

  - トークンのキャッシュと永続化（毎回取り直さず、必要な分だけ更新する）
  - アカウントの健全性管理（連続失敗で自動隔離し、別アカウントへ切り替える）
  - 重み付けによるアカウント選択
"""

from __future__ import annotations

import asyncio

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from core import crypto
from core.crypto import get_cipher
from core import breaker
from db.models import as_utc, McdAccount, McdToken, utcnow
from db.session import session_scope
from services.mcd.client import Fingerprint, McdClient, McdError, TokenSet

log = logging.getLogger("bot.mcd.accounts")

STATUS_ACTIVE = "ACTIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_QUARANTINED = "QUARANTINED"
STATUS_BANNED = "BANNED"
USABLE = (STATUS_ACTIVE, STATUS_DEGRADED)

# 日本国内のおおよその範囲（端末位置のゆらぎ生成に使う）
_JP_BOUNDS = (31.0, 45.5, 130.0, 145.5)


def random_home_location() -> tuple[float, float]:
    lat = round(random.uniform(_JP_BOUNDS[0], _JP_BOUNDS[1]), 6)
    lng = round(random.uniform(_JP_BOUNDS[2], _JP_BOUNDS[3]), 6)
    return lat, lng


@dataclass
class AccountHandle:
    """選ばれたアカウントと、そのクライアント。使い終わったら close する。"""
    account_id: int
    label: str
    card_id: str
    client: McdClient

    async def aclose(self) -> None:
        # ⚠️ 必ず予約を外す。外し忘れると、そのアカウントは
        #    再起動するまで二度と選ばれなくなる。
        release(self.account_id)
        await self.client.aclose()


# ============================================================
#  使用中のアカウント（同時注文の取り合いを防ぐ）
# ============================================================
#
# ⚠️ `last_used_at` だけでは**同時に呼ばれたときに防げない**。
#    3件が同時に来ると、3件とも書き込み前の値を読むため、
#    同じアカウントを選んでしまう。実際に
#    「同時に3件 → [1, 2, 1]」（1番に集中、3番は未使用）になっていた。
#
#    これは2つの害がある。
#      ・同時注文が実質1〜2アカウントしか使わず、並列にならない
#      ・1つのアカウントに注文が集中し、機械的な使い方に見える
#
# ⚠️ プロセス内の印なので、BOTを複数立ち上げる場合は効かない。
#    そのときはDB側で押さえる必要がある（いまは1プロセス前提）。

# ⚠️ **閉じ忘れると、そのアカウントは二度と選ばれなくなる。**
#    これは直そうとしたバグより悪い。呼び出し側は全部 finally で
#    閉じているが、人の手に頼る作りにはしない。印に時刻を持たせ、
#    長すぎるものは自動で手放す。
_HOLD_LIMIT_SECONDS = 15 * 60

_in_use: dict[int, float] = {}
_pick_lock = asyncio.Lock()


def _sweep() -> None:
    """長く持ちすぎている印を外す。"""
    now = time.monotonic()
    stale = [k for k, t in _in_use.items() if now - t > _HOLD_LIMIT_SECONDS]
    for k in stale:
        del _in_use[k]
        log.warning(
            "アカウント %s の使用中の印が %d 分を超えたため外しました。"
            "どこかで閉じ忘れている可能性があります", k, _HOLD_LIMIT_SECONDS // 60,
        )


def in_use() -> set[int]:
    """いま注文に使われているアカウント。"""
    _sweep()
    return set(_in_use)


def release(account_id: int) -> None:
    """使い終わったので手放す。二重に呼んでも害は無い。"""
    _in_use.pop(int(account_id), None)


def release_all() -> None:
    """検証用。全部手放す。"""
    _in_use.clear()


# ============================================================
#  トークンの保存
# ============================================================

async def _load_tokens(session: AsyncSession, account_id: int) -> TokenSet:
    """保存してあるトークンを読み出す。

    ⚠️ 鍵が合わないと復号で落ちる。ここで落とすと**注文の途中**で
       例外になり、利用者には理由の分からない失敗になる。
       読めなかったものは空として扱い、「ログインし直しが要る
       アカウント」として普通の経路に乗せる。
    """
    acc = await session.get(McdAccount, account_id)
    row = await session.get(McdToken, account_id)
    if acc is not None and crypto.is_unreadable(acc.refresh_token_enc):
        log.error(
            "アカウント %s の登録内容を、いまの鍵では読み取れません。"
            "data/encryption_key.txt を確認してください", account_id,
        )
    tokens = TokenSet(
        refresh_token=(crypto.try_decrypt(acc.refresh_token_enc) or "") if acc else ""
    )
    if row:
        tokens.access_token = crypto.try_decrypt(row.access_token_enc) or ""
        tokens.root_paseto = crypto.try_decrypt(row.root_paseto_enc) or ""
        tokens.access_exp = row.access_exp.timestamp() if row.access_exp else 0.0
        tokens.root_exp = row.root_exp.timestamp() if row.root_exp else 0.0
    return tokens


def _make_saver(account_id: int):
    """
    トークンが更新されるたびに呼ばれる保存処理。

    ⚠️ refresh_token は更新のたびにローテーションする。ここで保存し損ねると
       そのアカウントは二度とログインできなくなる。
    """
    async def save(tokens: TokenSet) -> None:
        cipher = get_cipher()
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc and tokens.refresh_token:
                acc.refresh_token_enc = cipher.encrypt(tokens.refresh_token)
            row = await s.get(McdToken, account_id)
            if row is None:
                row = McdToken(mcd_account_id=account_id)
                s.add(row)
            row.access_token_enc = cipher.encrypt(tokens.access_token or None)
            row.root_paseto_enc = cipher.encrypt(tokens.root_paseto or None)
            row.access_exp = (
                datetime.fromtimestamp(tokens.access_exp, tz=timezone.utc)
                if tokens.access_exp else None
            )
            row.root_exp = (
                datetime.fromtimestamp(tokens.root_exp, tz=timezone.utc)
                if tokens.root_exp else None
            )
            row.updated_at = utcnow()
    return save


def build_client(acc: McdAccount, tokens: TokenSet | None = None) -> McdClient:
    fp = Fingerprint(
        device_uid=acc.device_uid,
        wmop_device_id=acc.wmop_device_id,
        fb_instance_id=acc.fb_instance_id,
        latitude=float(acc.home_lat),
        longitude=float(acc.home_lng),
    )
    return McdClient(
        fp, tokens or TokenSet(), proxy=acc.proxy_url,
        on_tokens_updated=_make_saver(acc.id),
    )


async def open_account(account_id: int) -> AccountHandle:
    async with session_scope() as s:
        acc = await s.get(McdAccount, account_id)
        if acc is None:
            raise McdError(f"アカウント {account_id} が見つかりません")
        tokens = await _load_tokens(s, account_id)
        label, card_id = acc.label, acc.card_id or ""
        client = build_client(acc, tokens)
    return AccountHandle(account_id=account_id, label=label, card_id=card_id, client=client)


# ============================================================
#  アカウント選択
# ============================================================

def _score(acc: McdAccount, now: datetime) -> float:
    """
    点数が高いアカウントを選ぶ。

      ・使えること
      ・しばらく使っていないこと（連続利用を避ける）
      ・その日の注文が少ないこと
      ・直近で失敗していないこと
    """
    score = 0.0
    if acc.status == STATUS_ACTIVE:
        score += 3.0
    elif acc.status == STATUS_DEGRADED:
        score += 0.5

    last_used = as_utc(acc.last_used_at)
    if last_used:
        elapsed = (now - last_used).total_seconds() / 60.0
        score += 1.5 * min(elapsed / 30.0, 1.0)
    else:
        score += 1.5

    # DBに保存する前のオブジェクトでは、列の既定値(0)がまだ入っておらず
    # None になる。ここで落ちるとアカウントを1つも選べなくなるため、
    # 数え上げ系は必ず 0 を補ってから計算する。
    score += 1.0 * max(0.0, 1.0 - (acc.orders_today or 0) / 20.0)
    score -= 5.0 * (acc.consecutive_failures or 0)
    return score


async def pick_account(exclude: set[int] | None = None) -> AccountHandle:
    """
    使えるアカウントを1つ選んで開く。

    決済カードが設定されていないアカウントは注文を完了できないため、
    候補から外す（/mcd card で設定できる）。
    """
    exclude = set(exclude or set())
    now = datetime.now(timezone.utc)

    # ⚠️ 選ぶところは**1件ずつ**通す。同時に通すと、同じアカウントを
    #    2件が同時に選んでしまう（読んでから印を付けるまでの隙間）。
    async with _pick_lock:
        return await _pick_locked(exclude, now)


async def _pick_locked(exclude: set[int], now: datetime) -> AccountHandle:
    async with session_scope() as s:
        rows = (
            await s.execute(select(McdAccount).where(McdAccount.status.in_(USABLE)))
        ).scalars().all()
        # ⚠️ いま使われているアカウントは避ける。`last_used_at` は
        #    注文が終わるまで更新されないので、それだけでは防げない。
        busy = in_use()
        free = [a for a in rows
                if a.id not in exclude and a.id not in busy and a.card_id]
        if free:
            candidates = free
        else:
            # ⚠️ 全部ふさがっていても止めない。待たせるより、
            #    いちばん余裕のあるものを使い回すほうがよい。
            #    （注文の同時数は core/queue.py 側で既に絞ってある）
            candidates = [a for a in rows if a.id not in exclude and a.card_id]
            if candidates:
                log.info("空いているアカウントがありません。使用中から選び直します")
        # 続けて失敗しているアカウントは、しばらく使わない。
        # 壊れた相手に送り続けると全員がタイムアウトを待たされるため。
        usable = [a for a in candidates if breaker.accounts.allows(f"mcd:{a.id}")]
        if usable:
            candidates = usable
        elif candidates:
            # 全部止まっている場合は、一番早く復帰するものを試す
            log.warning("使えるアカウントが一時的にありません。最も回復が近いものを試します")
            candidates.sort(key=lambda a: breaker.accounts.get(f"mcd:{a.id}").retry_after)
            candidates = candidates[:1]
        if not candidates:
            no_card = [a.label for a in rows if a.id not in exclude and not a.card_id]
            if no_card:
                raise McdError(
                    "決済カードが設定されていないため注文できません。"
                    f"（{', '.join(no_card[:3])}）"
                    " /mcd card <ID> で設定してください"
                )
            raise McdError(
                "使用できるマクドナルドアカウントがありません。"
                "/mcd list で状態を確認してください"
            )
        best = max(candidates, key=lambda a: _score(a, now))
        # ⚠️ **選んだ直後に印を付ける。** セッションを抜けてから付けると、
        #    その隙間に別の注文が同じものを選ぶ。
        _in_use[best.id] = time.monotonic()
        tokens = await _load_tokens(s, best.id)
        handle = AccountHandle(
            account_id=best.id, label=best.label, card_id=best.card_id or "",
            client=build_client(best, tokens),
        )
        best.last_used_at = now
    log.info("アカウントを選択しました: %s (ID %s)", handle.label, handle.account_id)
    return handle


# ============================================================
#  健全性
# ============================================================

# ------------------------------------------------------------
#  POSトークンの使い回し
# ------------------------------------------------------------
# 注文のたびに取り直していたが、短時間なら使い回せる。
# 1注文あたりの往復が1回減る。
#   キー: (アカウントID, グループ) → (トークン, 期限)
_pos_cache: dict[tuple[int, str], tuple[str, float]] = {}


async def pos_paseto(handle: "AccountHandle", group: str) -> str:
    """POSトークンを取る。まだ新しければ前のものを使う。"""
    import time

    key = (handle.account_id, group)
    hit = _pos_cache.get(key)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]

    token = await handle.client.get_pos_paseto(group)
    _pos_cache[key] = (token, now + float(config.POS_PASETO_TTL_SECONDS))
    return token


def forget_pos(account_id: int | None = None) -> None:
    """使えなくなったトークンを捨てる。"""
    if account_id is None:
        _pos_cache.clear()
        return
    for key in [k for k in _pos_cache if k[0] == account_id]:
        _pos_cache.pop(key, None)


async def warm_up(store_id: str, group: str = "") -> None:
    """
    注文の下ごしらえを先に済ませておく。

    利用者が商品を選んでいる間に、裏でトークンと接続を温めておくと、
    確定ボタンを押したときの待ちが短くなる。

    ⚠️ 失敗しても何もしない。あくまで前倒しなので、
       本番の注文はいつもどおり自分で取り直す。
    """
    handle = None
    try:
        handle = await pick_account()
        await handle.client.ensure_auth()
        if group:
            await pos_paseto(handle, group)
    except Exception as e:
        log.debug("下ごしらえに失敗しました（注文には影響しません）: %s", e)
    finally:
        if handle:
            await handle.aclose()


async def report_success(account_id: int) -> None:
    breaker.accounts.record_success(f"mcd:{account_id}")
    async with session_scope() as s:
        acc = await s.get(McdAccount, account_id)
        if acc is None:
            return
        acc.consecutive_failures = 0
        acc.last_error = None
        acc.orders_today += 1
        if acc.status == STATUS_DEGRADED:
            acc.status = STATUS_ACTIVE
            log.info("アカウント %s が復帰しました", acc.label)


async def report_failure(account_id: int, error: str, *, fatal: bool = False) -> str:
    """
    失敗を記録し、必要なら隔離する。

    fatal=True は「このアカウントを使い続けても直らない」場合。
    カードの残高不足・期限切れ・認証切れなどがこれにあたる。
    回数を待たずにすぐ隔離する。待っていると、その間の注文が
    全部同じ理由で失敗してしまうため。

    返り値は新しい状態。QUARANTINED になったら管理者へ通知すること。
    """
    breaker.accounts.record_failure(f"mcd:{account_id}", error)
    forget_pos(account_id)   # 使えなくなっているかもしれないので捨てる
    async with session_scope() as s:
        acc = await s.get(McdAccount, account_id)
        if acc is None:
            return STATUS_BANNED
        before = acc.status
        acc.consecutive_failures += 1
        acc.last_error = error[:500]
        action = "failure"
        if fatal:
            acc.status = STATUS_QUARANTINED
            log.error("アカウント %s を隔離しました（回復が見込めない失敗）", acc.label)
        elif acc.consecutive_failures >= config.MCD_FAILURES_TO_QUARANTINE:
            acc.status = STATUS_QUARANTINED
            log.error("アカウント %s を隔離しました（%d回連続失敗）",
                      acc.label, acc.consecutive_failures)
        elif acc.consecutive_failures >= config.MCD_FAILURES_TO_DEGRADE:
            acc.status = STATUS_DEGRADED
            log.warning("アカウント %s の状態を下げました（%d回連続失敗）",
                        acc.label, acc.consecutive_failures)
        if acc.status != before:
            action = ("quarantine" if acc.status == STATUS_QUARANTINED
                      else "degrade")

        # ⚠️ last_error は最後の1件しか残らない。止まった理由は後から
        #    調べるものなので、そのときには上書きされている。経緯を残す。
        _add_event(
            s, account_id, action, error,
            status_after=acc.status, failures=acc.consecutive_failures,
        )
        return acc.status


def _add_event(
    s, account_id: int, action: str, error: str = "", *,
    status_after: str = "", failures: int = 0, info=None,
) -> None:
    """アカウントに起きたことを1件残す。

    ⚠️ **記録のために処理を止めない。** ここで落ちると注文そのものが
       失敗する。分類できなくても、生の文面だけは必ず残す。

    ⚠️ 生の応答には認証情報が混ざりうる。必ず伏せてから入れる。
    """
    from db.models import McdAccountEvent

    try:
        from services.mcd import errors as mcd_errors

        if info is None and error:
            try:
                # ⚠️ parse(status, body) の順。取り違えると例外になり、
                #    ここは握りつぶすので**無言で分類なし**になる。
                #    HTTPの状態が分からない場面なので 0 を渡す。
                info = mcd_errors.parse(0, error)
            except Exception:
                log.debug("エラーの分類に失敗しました", exc_info=True)
                info = None
        s.add(McdAccountEvent(
            mcd_account_id=int(account_id),
            action=action[:24],
            kind=(getattr(info, "kind", "") or "")[:24],
            http_status=int(getattr(info, "status", 0) or 0),
            message=(getattr(info, "message", "") or error or "")[:500],
            raw=mcd_errors.scrub(error)[:4000],
            status_after=(status_after or "")[:16],
            failures=int(failures or 0),
        ))
    except Exception:
        log.debug("アカウントの記録に失敗しました（処理は続けます）", exc_info=True)


async def account_events(account_id: int, limit: int = 20) -> list:
    """そのアカウントに起きたことを新しい順に。"""
    from db.models import McdAccountEvent

    async with session_scope() as s:
        rows = (await s.execute(
            select(McdAccountEvent)
            .where(McdAccountEvent.mcd_account_id == int(account_id))
            .order_by(McdAccountEvent.created_at.desc())
            .limit(limit)
        )).scalars().all()
        for r in rows:
            _ = (r.action, r.kind, r.message, r.raw, r.http_status,
                 r.status_after, r.failures, r.created_at)
        s.expunge_all()
        return list(rows)


async def healthcheck_all() -> list[tuple[int, str, bool]]:
    """全アカウントの生存確認。(id, label, 結果) を返す。"""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(McdAccount).where(McdAccount.status != STATUS_BANNED)
            )
        ).scalars().all()
        targets = [(a.id, a.label) for a in rows]

    results = []
    for account_id, label in targets:
        handle = None
        try:
            handle = await open_account(account_id)
            alive = await handle.client.healthcheck()
        except McdError as e:
            alive = False
            await report_failure(account_id, str(e))
        finally:
            if handle:
                await handle.aclose()
        if alive:
            await report_success_healthcheck(account_id)
        results.append((account_id, label, alive))
    return results


async def report_success_healthcheck(account_id: int) -> None:
    """ヘルスチェック成功（注文数は増やさない）。"""
    async with session_scope() as s:
        acc = await s.get(McdAccount, account_id)
        if acc is None:
            return
        acc.consecutive_failures = 0
        acc.last_error = None
        if acc.status in (STATUS_DEGRADED, STATUS_QUARANTINED):
            acc.status = STATUS_ACTIVE
            log.info("アカウント %s が復帰しました", acc.label)
            # ⚠️ 復帰も残す。止まった記録だけでは「何回止まって何回
            #    戻ったか」が分からず、たまたま一度止まったのか、
            #    止まり続けているのかを見分けられない。
            _add_event(s, account_id, "recover", status_after=acc.status)


async def reset_daily_counters() -> None:
    async with session_scope() as s:
        for acc in (await s.execute(select(McdAccount))).scalars().all():
            acc.orders_today = 0
