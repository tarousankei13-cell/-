"""
マクドナルドアカウントの管理

  - トークンのキャッシュと永続化（毎回取り直さず、必要な分だけ更新する）
  - アカウントの健全性管理（連続失敗で自動隔離し、別アカウントへ切り替える）
  - 重み付けによるアカウント選択
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
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
        await self.client.aclose()


# ============================================================
#  トークンの保存
# ============================================================

async def _load_tokens(session: AsyncSession, account_id: int) -> TokenSet:
    cipher = get_cipher()
    acc = await session.get(McdAccount, account_id)
    row = await session.get(McdToken, account_id)
    tokens = TokenSet(refresh_token=cipher.decrypt(acc.refresh_token_enc) or "" if acc else "")
    if row:
        tokens.access_token = cipher.decrypt(row.access_token_enc) or ""
        tokens.root_paseto = cipher.decrypt(row.root_paseto_enc) or ""
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
    exclude = exclude or set()
    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        rows = (
            await s.execute(select(McdAccount).where(McdAccount.status.in_(USABLE)))
        ).scalars().all()
        candidates = [a for a in rows if a.id not in exclude and a.card_id]
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
        acc.consecutive_failures += 1
        acc.last_error = error[:500]
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
        return acc.status


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


async def reset_daily_counters() -> None:
    async with session_scope() as s:
        for acc in (await s.execute(select(McdAccount))).scalars().all():
            acc.orders_today = 0
