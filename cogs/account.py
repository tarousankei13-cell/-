"""
アカウント管理（管理者用）

マクドナルド・Kyash とも、メールとパスワードを入力したあと SMS 等で届く
6桁の認証コードが必要になる。

Discord のモーダルは「送信から3秒以内に応答」が必要で、その間に
ログイン通信を挟むと間に合わないことがある。そのため
  ① 認証情報のモーダル → すぐ「認証コードを入力」ボタンを表示
  ② 裏でログイン処理
  ③ ボタンを押して認証コードのモーダル
という二段構えにしている。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import discord

from config import JST
from discord import app_commands
from discord.ext import commands

import emoji as E
from core import audit
from core import crypto
from core import proxy as core_proxy
from core.crypto import get_cipher
from db.models import KyashAccount, McdAccount, McdToken, as_utc, utcnow
from db.session import session_scope
from cogs._checks import admin_only, handle_check_failure
from services.kyash.client import KyashClient, KyashError
from services.mcd import accounts as mcd_accounts
from services.mcd.client import Fingerprint, McdClient, McdError
from ui import embeds

log = logging.getLogger("bot.cogs.account")


# ============================================================
#  マクドナルド
# ============================================================

async def _save_mcd(client, fp, email: str, label: str) -> int:
    """
    ログイン済みのマクドナルドアカウントを保存して、IDを返す。

    ⚠️ 認証コードを求められた場合と求められなかった場合の**両方**が
       ここを通る。別々に書くと、片方だけ列が増えて食い違う。
    """
    refresh = client.tokens.refresh_token
    if not refresh:
        raise RuntimeError("リフレッシュトークンがありません")

    cipher = get_cipher()
    async with session_scope() as s:
        acc = McdAccount(
            label=label,
            email_enc=cipher.encrypt(email),
            refresh_token_enc=cipher.encrypt(refresh),
            device_uid=fp.device_uid,
            wmop_device_id=fp.wmop_device_id,
            fb_instance_id=fp.fb_instance_id,
            home_lat=fp.latitude, home_lng=fp.longitude,
        )
        s.add(acc)
        await s.flush()
        return acc.id


# 認証コードの入力を待つ時間（秒）。
#
# ⚠️ まとめ登録では**何件分ものコードを集めてから**入れる。メールを
#    順に開いて書き写す時間が要るので、1件ぶんの感覚で短くすると
#    「書き写している間に画面が死ぬ」→ 最初からやり直しになる。
OTP_WAIT_SECONDS = 15 * 60


async def _save_and_autocard(
    client, fp, email: str, label: str,
) -> tuple[int, str, int]:
    """保存して、カードが1枚だけならそれを設定する。

    ⚠️ 認証コードが要らなかった分と、あとからコードを入れた分の
       **両方**がここを通る。別々に書くと、片方だけカードが設定されず
       「登録したのに注文に選ばれない」食い違いが起きる。

    戻り値は (アカウントID, カードの表示名, 使えるカードの枚数)。
    クライアントは閉じない（呼び出し側で閉じる）。
    """
    account_id = await _save_mcd(client, fp, email, label)
    cards: list[dict] = []
    try:
        cards = await client.get_cards()
    except McdError:
        pass
    usable = [c for c in cards if c.get("card_id")]
    card_name = ""
    if len(usable) == 1:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc:
                acc.card_id = usable[0]["card_id"]
                acc.card_expiry = (usable[0].get("expiry") or "")[:8] or None
        card_name = (usable[0].get("masked") or "")[:32]
    return account_id, card_name, len(usable)


class McdCredModal(discord.ui.Modal, title="マクドナルドアカウントを追加"):
    label = discord.ui.TextInput(label="表示名（任意）", required=False, max_length=32,
                                 placeholder="例: メイン")
    email = discord.ui.TextInput(label="メールアドレス", required=True, max_length=128)
    password = discord.ui.TextInput(
        label="パスワード", required=True, max_length=128,
        placeholder="この入力はあなたにしか見えません",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        email = str(self.email.value).strip()
        password = str(self.password.value)
        label = str(self.label.value).strip() or email.split("@")[0]

        await interaction.response.send_message(
            embed=embeds.info(f"{E.LOADING} ログインしています…"), ephemeral=True
        )

        fp = Fingerprint.generate(*mcd_accounts.random_home_location())
        client = McdClient(fp)
        try:
            result = await client.login(email, password)
        except McdError as e:
            await client.aclose()
            await interaction.edit_original_response(embed=embeds.error(str(e)))
            return

        # 認証コードを求められなかったときは、ここで終わり。
        # 届かないコードの入力画面を出しても進めない。
        if not result.needs_otp:
            try:
                account_id = await _save_mcd(client, fp, email, label)
            except Exception:
                log.exception("マクドナルドアカウントの保存に失敗しました")
                await client.aclose()
                await interaction.edit_original_response(
                    embed=embeds.error("アカウントの保存に失敗しました。")
                )
                return
            # ⚠️ ここでもカードを見る。以前は「`/mcd card` を実行して
            #    ください」と案内するだけで、やり忘れるとそのアカウントは
            #    注文に選ばれないまま放置されていた。
            cards = []
            try:
                cards = await client.get_cards()
            except McdError as e:
                log.info("カード一覧を取得できませんでした: %s", e)
            await client.aclose()
            await interaction.edit_original_response(
                embed=embeds.info(f"{E.OK} 登録しました。"), view=None)
            await _after_mcd_added(
                interaction, account_id, label, cards,
                note="認証コードは求められませんでした。",
            )
            return

        await interaction.edit_original_response(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。\n"
                "SMS またはメールに届いた **6桁** を、下のボタンから入力してください。"
            ),
            view=McdOtpView(
                interaction.user.id, client, fp, result.mfa_token, email, label
            ),
        )


class McdOtpView(discord.ui.View):
    def __init__(self, owner_id, client, fp, mfa_token, email, label) -> None:
        super().__init__(timeout=OTP_WAIT_SECONDS)
        self.owner_id = owner_id
        self.client = client
        self.fp = fp
        self.mfa_token = mfa_token
        self.email = email
        self.label = label
        self.attempts = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        await self.client.aclose()

    @discord.ui.button(label="認証コードを入力", emoji=E.KEY, style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(McdOtpModal(self))


class McdOtpModal(discord.ui.Modal, title="認証コードの入力"):
    otp = discord.ui.TextInput(label="6桁の認証コード", required=True, min_length=4, max_length=8)

    def __init__(self, parent: McdOtpView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.parent
        try:
            await p.client.login_with_mfa(p.mfa_token, str(self.otp.value).strip())
        except McdError as e:
            p.attempts += 1
            if p.attempts >= 3:
                await p.client.aclose()
                await interaction.followup.send(
                    embed=embeds.error("認証に3回失敗しました。最初からやり直してください。"),
                    ephemeral=True,
                )
                p.stop()
                return
            await interaction.followup.send(
                embed=embeds.error(f"{e}\nもう一度お試しください（残り {3 - p.attempts} 回）。"),
                ephemeral=True,
            )
            return

        account_id = await _save_mcd(p.client, p.fp, p.email, p.label)

        # カードを取得して選ばせる
        cards = []
        try:
            cards = await p.client.get_cards()
        except McdError as e:
            log.info("カード一覧を取得できませんでした: %s", e)
        await p.client.aclose()
        p.stop()

        await _after_mcd_added(interaction, account_id, p.label, cards)


# ============================================================
#  認証コードを、あとからまとめて入れる
# ============================================================
#
# まとめ登録では、認証コードを求められた分を**捨てずに預かる**。
# メールとパスワードは預かった側が持っているので、利用者があとから
# 入れるのは **コードだけ** で済む。
#
# ⚠️ 以前はコードを求められた時点で諦めていた（クライアントを閉じて
#    「`/mcd add` で1件ずつ」と案内していた）。10件あれば10回、
#    メールとパスワードを打ち直すことになっていた。


@dataclass
class _PendingOtp:
    """認証コードを待っているアカウント1件。

    ⚠️ client と mfa_token は**ログインの続き**に必要なので捨てない。
       持ち続ける以上、**必ず閉じる**こと。完了・やめる・時間切れの
       すべての道で閉じる（閉じ忘れると通信が開いたまま残る）。
    """

    email: str
    label: str
    fp: object
    client: object
    mfa_token: str
    attempts: int = 0
    done: bool = False
    note: str = ""


_OTP_SEPS = (",", ":", "=", "\t", " ")


def _parse_otp_batch(
    raw: str, open_idx: list[int],
) -> tuple[dict[int, str], list[str]]:
    """貼られた文字列から {番号: コード} を取り出す。

    ⚠️ 入れ方を1つに強制しない。届いたメールを開く順番は人によって
       違うので、

         ・`1,123456` と番号を添える（並び順が違っても安全）
         ・`123456` だけを並べる（表示した順に当てる）

       の両方を受ける。番号付きを先に確定させ、番号なしのコードは
       **空いている番号へ上から詰める**。

    ⚠️ 番号は画面の表示と同じ 1 始まり。内部は 0 始まりなので変換する。

    戻り値は (割り当て, 読めなかった行)。
    """
    numbered: dict[int, str] = {}
    bare: list[str] = []
    bad: list[str] = []
    allowed = set(open_idx)

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        idx: int | None = None
        code = line
        for sep in _OTP_SEPS:
            if sep in line:
                head, _, tail = line.partition(sep)
                head, tail = head.strip(), tail.strip()
                if head.isdigit() and tail.isdigit():
                    idx, code = int(head) - 1, tail
                break
        if not code.isdigit() or not (4 <= len(code) <= 8):
            bad.append(line[:40])
            continue
        if idx is None:
            bare.append(code)
        elif idx in allowed:
            numbered[idx] = code
        else:
            # まだ待っていない番号。取り違えて別の口座に入れないよう断る。
            bad.append(line[:40])

    # 番号なしのコードを、空いている番号へ上から詰める
    free = [i for i in open_idx if i not in numbered]
    for code in bare:
        if not free:
            bad.append(code)
            continue
        numbered[free.pop(0)] = code
    return numbered, bad


class OtpBatchView(discord.ui.View):
    """預かった分の認証コードを、まとめて片付ける画面。

    ⚠️ 持っているクライアントは必ず閉じる。完了・やめる・時間切れの
       すべての道で閉じること。
    """

    def __init__(self, owner_id: int, pending: list[_PendingOtp]) -> None:
        super().__init__(timeout=OTP_WAIT_SECONDS)
        self.owner_id = owner_id
        self.pending = pending
        # 登録できた分（アカウントID, 表示名, カード名, 使えるカード枚数）
        self.added: list[tuple[int, str, str, int]] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    def open_idx(self) -> list[int]:
        """まだコードを待っている番号（0始まり）。"""
        return [i for i, x in enumerate(self.pending) if not x.done]

    def listing(self) -> str:
        rows = []
        for i, x in enumerate(self.pending):
            no = i + 1
            if x.done:
                rows.append(f"{E.OK} `{no}` {_mask_mail(x.email)}　{x.note}")
            else:
                tail = f"　{x.note}" if x.note else ""
                rows.append(f"{E.KEY} `{no}` {_mask_mail(x.email)}{tail}")
        return "\n".join(rows)

    async def close_open(self) -> None:
        """まだ終わっていない分のクライアントを閉じる。"""
        for x in self.pending:
            if not x.done:
                try:
                    await x.client.aclose()
                except Exception:
                    pass

    async def on_timeout(self) -> None:
        # ⚠️ 時間切れでも必ず閉じる。
        await self.close_open()

    @discord.ui.button(
        label="認証コードをまとめて入力", emoji=E.KEY,
        style=discord.ButtonStyle.primary,
    )
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.open_idx():
            await interaction.response.send_message(
                embed=embeds.info("待っている分はもうありません。"), ephemeral=True
            )
            return
        await interaction.response.send_modal(OtpBatchModal(self))

    @discord.ui.button(label="やめる", emoji=E.NG, style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        left = len(self.open_idx())
        await self.close_open()
        for x in self.pending:
            x.done = True
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info(
                f"{E.INFO} 残り {left} 件の登録をやめました。\n"
                "あとで入れたい場合は、`/mcd bulk` からもう一度お試しください。"
            ),
            view=None,
        )


class OtpBatchModal(discord.ui.Modal, title="認証コードの入力（まとめて）"):
    codes = discord.ui.TextInput(
        label="番号とコード（1行に1件）",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1000,
        placeholder=("1,123456\n2,654321\n"
                     "（番号を省いてコードだけ並べても大丈夫です）"),
    )

    def __init__(self, parent: OtpBatchView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.parent
        assign, bad = _parse_otp_batch(str(self.codes.value), p.open_idx())

        if not assign:
            await interaction.followup.send(
                embed=embeds.error(
                    "コードを読み取れませんでした。\n"
                    "`番号,コード` の形で1行に1件入れてください。"
                    + (f"\n読めなかった行: {len(bad)}" if bad else "")
                ),
                ephemeral=True,
            )
            return

        ok, ng = 0, 0
        for idx, code in sorted(assign.items()):
            x = p.pending[idx]
            if x.done:
                continue
            try:
                await x.client.login_with_mfa(x.mfa_token, code)
            except McdError as e:
                x.attempts += 1
                ng += 1
                if x.attempts >= 3:
                    # ⚠️ 3回間違えたら、この分は閉じて終わりにする。
                    #    開いたまま残すと通信が残る。
                    try:
                        await x.client.aclose()
                    except Exception:
                        pass
                    x.done = True
                    x.note = f"{E.NG} 3回失敗したのでやめました"
                else:
                    x.note = f"{E.NG} {str(e)[:40]}（残り {3 - x.attempts} 回）"
                continue
            except Exception as e:
                log.exception("認証コードの確認に失敗しました: %s", x.email)
                ng += 1
                x.note = f"{E.NG} {type(e).__name__}"
                continue

            try:
                account_id, card_name, n_usable = await _save_and_autocard(
                    x.client, x.fp, x.email, x.label
                )
            except Exception as e:
                log.exception("アカウントの保存に失敗しました: %s", x.email)
                ng += 1
                x.note = f"{E.NG} 保存に失敗 {type(e).__name__}"
                continue
            finally:
                try:
                    await x.client.aclose()
                except Exception:
                    pass

            x.done = True
            ok += 1
            p.added.append((account_id, x.label, card_name, n_usable))
            mark = (f"カード {card_name or '設定済み'}" if n_usable == 1
                    else (f"{E.WARN} カード{n_usable}枚 → `/mcd card {account_id}`"
                          if n_usable > 1 else f"{E.NG} カード無し"))
            x.note = f"`#{account_id}` **{x.label}**　{mark}"

            await audit.record(
                actor_id=interaction.user.id, actor_name=str(interaction.user),
                action="account.add", target=f"mcd:{account_id}",
                detail="まとめ登録（認証コード）",
            )

        left = p.open_idx()
        parts = [
            f"{E.OK} **{ok} 件を登録しました**" if ok else "",
            f"{E.NG} **{ng} 件は進めませんでした**" if ng else "",
            f"{E.WARN} 読めなかった行: {len(bad)} 件" if bad else "",
            p.listing(),
        ]
        desc = "\n\n".join([x for x in parts if x])[:4000]

        if left:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"{E.KEY} 認証コードの入力（残り {len(left)} 件）",
                    description=desc + (
                        "\n\n残っている分は、もう一度 **認証コードをまとめて入力** "
                        "から入れてください。"
                    ),
                    color=embeds.GREEN if ok else embeds.RED,
                ),
                view=p,
                ephemeral=True,
            )
            return

        p.stop()
        no_card = [a for a in p.added if a[3] == 0]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.PEOPLE} 認証コードの入力が終わりました",
                description=desc,
                color=embeds.GREEN if ok else embeds.RED,
            ),
            view=(CardScanView(interaction.user.id, [a[0] for a in no_card])
                  if no_card else None),
            ephemeral=True,
        )


class McdBulkModal(discord.ui.Modal, title="アカウントをまとめて登録"):
    """自分で作ったアカウントを、順に取り込む。

    ⚠️ **新しくアカウントを作るものではない。** すでにマクドナルドに
       登録してあるアカウントを、このBOTへ取り込むだけ。

    ⚠️ 認証コードを求められたものは、ここでは進められない。
       コードは本人が受け取るものなので、まとめ処理の中で待てない。
       その分は `/mcd add` で1件ずつ入れてもらう。
    """

    lines = discord.ui.TextInput(
        label="メールアドレスとパスワード（1行に1件）",
        style=discord.TextStyle.paragraph,
        required=True, max_length=2000,
        placeholder=("a@example.com,ぱすわーど\n"
                     "b@example.com,ぱすわーど\n"
                     "（カンマ・コロン・スペース・タブ区切り）"),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # ⚠️ ここは**3秒以内に応答**しなければならない。読み取りは
        #    文字列を見るだけにして、DBや通信は応答後（_run_bulk）に回す。
        #    既に登録済みかどうかの照合も、あちらで行う。
        raw = str(self.lines.value)
        r = _parse_bulk_report(raw)
        if not r.entries:
            await interaction.response.send_message(
                embed=embeds.error(
                    "読み取れる行がありませんでした。\n"
                    "`メールアドレス,パスワード` の形で1行に1件、"
                    "入れてください。\n\n" + _skip_report(r, [])
                ),
                ephemeral=True,
            )
            return
        if len(r.entries) > BULK_MAX_PASTE:
            await interaction.response.send_message(
                embed=embeds.error(
                    f"一度に登録できるのは{BULK_MAX_PASTE}件までです"
                    f"（{len(r.entries)}件ありました）。\n"
                    f"{E.INFO} ファイルを添付すれば、"
                    f"{BULK_MAX_FILE}件までまとめて入れられます"
                    "（`/mcd bulk file:〜`）。"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=embeds.info(
                f"{E.LOADING} {len(r.entries)} 件を順に登録しています…\n"
                f"{E.INFO} 1件ずつログインするので少し時間がかかります。"
                + _skip_summary(r)
            ),
            ephemeral=True,
        )
        await _run_bulk(interaction, r.entries, parse=r)


async def _run_bulk(
    interaction: discord.Interaction, entries: list[tuple[str, str, str]],
    *, parse: "BulkParse | None" = None,
) -> None:
    """読み取り済みの行を順に登録して、結果を出す。

    ⚠️ 貼り付けから来た分とファイルから来た分の**両方**がここを通る。
       別々に書くと、片方だけ認証コードを預からない等の食い違いが起きる。

    ⚠️ すでに応答済み（defer か send_message 済み）で呼ぶこと。
       ここからは followup で返す。

    ⚠️ すでに登録済みのメールは**ログインを試さずに外す**。ここは
       応答済みなので、復号の時間がかかってもモーダルの3秒制限に
       かからない。二重に登録すると、どちらが使われるか分からなくなる。
    """
    have = await _existing_emails()
    already = [e for e, _, _ in entries if e.lower() in have]
    entries = [x for x in entries if x[0].lower() not in have]
    # ⚠️ 呼び出し側がすでに外している分（ファイル経路）も合わせて報告する。
    #    ここで数え直すだけだと、外してある分は空になり、
    #    「登録済みだから飛ばした」ことが画面から消えてしまう。
    if parse is not None and parse.already:
        already = list(dict.fromkeys(list(parse.already) + already))

    added, failed = [], []
    # ⚠️ 失敗した行は**そのまま控える**。やり直しにはパスワードが要るので、
    #    理由だけでなく行そのものを持つ（打ち直させない）。
    failed_entries: list[tuple[str, str, str]] = []
    # ⚠️ 認証コードが必要な分は**捨てずに預かる**。クライアントと
    #    mfa_token を持ち続けるので、利用者はあとから**コードだけ**
    #    入れれば済む（メールとパスワードを打ち直さない）。
    #    預かったものは OtpBatchView が必ず閉じる。
    pending_otp: list[_PendingOtp] = []
    for email, password, label in entries:
        fp = Fingerprint.generate(*mcd_accounts.random_home_location())
        client = McdClient(fp)
        try:
            result = await client.login(email, password)
            if result.needs_otp:
                pending_otp.append(_PendingOtp(
                    email=email, label=label, fp=fp,
                    client=client, mfa_token=result.mfa_token,
                ))
                continue          # ★閉じない。続きがある
            account_id, card_name, n_usable = await _save_and_autocard(
                client, fp, email, label
            )
            await client.aclose()
            added.append((account_id, label, card_name, n_usable))
        except McdError as e:
            failed.append((email, str(e)[:80]))
            failed_entries.append((email, password, label))
            await client.aclose()
        except Exception as e:
            log.exception("まとめ登録に失敗しました: %s", email)
            failed.append((email, f"{type(e).__name__}: {e}"[:80]))
            failed_entries.append((email, password, label))
            try:
                await client.aclose()
            except Exception:
                pass

    parts = []
    # ⚠️ 飛ばした分を黙って消さない。打ち間違えた1行が
    #    「登録されていない」ことに気づけなくなる。
    skip = _skip_report(parse, already)
    if skip:
        parts.append(skip)
    if added:
        rows = []
        for aid, label, card, n in added:
            mark = (f"カード {card or '設定済み'}" if n == 1
                    else (f"{E.WARN} カード{n}枚 → `/mcd card {aid}`" if n > 1
                          else f"{E.NG} カード無し"))
            rows.append(f"・`#{aid}` **{label}**　{mark}")
        parts.append(f"{E.OK} **登録できました（{len(added)}件）**\n"
                     + _few(rows))
    if pending_otp:
        parts.append(
            f"{E.KEY} **認証コードが必要です（{len(pending_otp)}件）**\n"
            "→ このあとの画面で、**コードだけ**まとめて入れれば登録できます。\n"
            "（メールとパスワードを打ち直す必要はありません）"
        )
    if failed:
        parts.append(
            f"{E.NG} **登録できませんでした（{len(failed)}件）**\n"
            + _few(f"・{_mask_mail(m)}　{why}" for m, why in failed)
        )

    no_card = [a for a in added if a[3] == 0]
    desc = "\n\n".join(parts)
    # ⚠️ 画面の一覧は _few で頭だけしか出していない。省略が起きたときや
    #    埋め込みの字数に収まらないときは、**全文をファイルで渡す**。
    #    でないと「どれが入ってどれが入らなかったか」が画面から
    #    永久に失われる。
    rows_total = (
        len(added) + len(failed) + len(already)
        + ((len(parse.dup) + len(parse.bad)) if parse else 0)
    )
    report = None
    if rows_total > 10 or len(desc) > 3800:
        report = discord.File(
            io.BytesIO(_bulk_report_text(
                added, failed, already, parse).encode("utf-8")),
            filename="まとめ登録の結果.txt",
        )
        desc = desc[:3800] + "\n\n…全文は添付のファイルをご覧ください。"
    await interaction.followup.send(
        embed=discord.Embed(
            title=f"{E.PEOPLE} まとめ登録の結果",
            description=desc,
            color=embeds.GREEN if added else embeds.RED,
        ),
        view=(CardScanView(interaction.user.id, [a[0] for a in no_card])
              if no_card else None),
        file=report,
        ephemeral=True,
    )

    # 登録できなかった分は、やり直しの画面を別のメッセージで出す。
    #
    # ⚠️ 1つのメッセージに付けられる画面は1つだけなので、カードや
    #    認証コードの画面とは分ける。
    if failed_entries:
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.NG} 登録できなかった分（{len(failed_entries)}件）",
                description=(
                    "原因が一時的なもの（通信など）なら、やり直すだけで"
                    "入ることがあります。\n"
                    "**メールとパスワードは控えてあるので、打ち直す必要は"
                    "ありません。**\n"
                    f"（{OTP_WAIT_SECONDS // 60}分以内）"
                ),
                color=embeds.ORANGE,
            ),
            view=BulkRetryView(interaction.user.id, failed_entries),
            ephemeral=True,
        )

    # 認証コードを待っている分は、別のメッセージで預かり画面を出す。
    #
    # ⚠️ 1つのメッセージに付けられる画面は1つだけなので、カードの
    #    画面とは分ける。こちらは時間切れがあるので後に出す。
    if pending_otp:
        view = OtpBatchView(interaction.user.id, pending_otp)
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.KEY} 認証コードの入力（{len(pending_otp)}件）",
                description=(
                    "届いた **6桁** を、下のボタンから**まとめて**入れて"
                    f"ください（{OTP_WAIT_SECONDS // 60}分以内）。\n"
                    "番号を添えても、コードだけ順に並べても読み取れます。\n\n"
                    + view.listing()
                )[:4000],
                color=embeds.BLUE,
            ),
            view=view,
            ephemeral=True,
        )


def _mask_mail(email: str) -> str:
    """記録や画面にメールをそのまま出さない。"""
    name, _, domain = email.partition("@")
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}" if domain else f"{head}***"


# 一度に登録できる件数。
#
# ⚠️ 1件ずつログインするので、多すぎると Discord の応答期限
#    （操作を受け付けてから15分）を超えて結果を返せなくなる。
#    貼り付けは入力窓が2000文字までなので、そもそも多くは入らない。
BULK_MAX_PASTE = 20
BULK_MAX_FILE = 100


@dataclass
class BulkParse:
    """読み取りの結果。**何を採って何を捨てたか**を全部持つ。

    ⚠️ 黙って捨てないこと。以前は読めない行をそのまま飛ばしていたので、
       打ち間違えた1行が「登録されていない」ことに気づけなかった。
    """

    entries: list[tuple[str, str, str]] = field(default_factory=list)
    bad: list[tuple[int, str]] = field(default_factory=list)      # 読めない行
    dup: list[tuple[int, str]] = field(default_factory=list)      # 同じファイル内の重複
    already: list[str] = field(default_factory=list)              # すでに登録済み

    @property
    def total_skipped(self) -> int:
        return len(self.bad) + len(self.dup) + len(self.already)


def _parse_bulk_report(raw: str) -> BulkParse:
    """貼られた文字列から (メール, パスワード, 表示名) を取り出す。

    ⚠️ 区切り文字を決め打ちしない。カンマで貼る人もコロンで貼る人も
       タブで貼る人もいる。パスワードに区切り文字が入ることもあるので、
       **最初の1つだけ**で切る。

    ⚠️ 捨てた行は**行番号つきで**返す。どこを直せばよいか分からないと、
       打ち間違いを見つけられない。
    """
    r = BulkParse()
    seen: dict[str, int] = {}
    for no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        email = password = ""
        for sep in (",", ":", "\t", " "):
            if sep in line:
                email, _, password = line.partition(sep)
                break
        email, password = email.strip(), password.strip()
        if not email or not password or "@" not in email:
            r.bad.append((no, line[:60]))
            continue
        key = email.lower()
        if key in seen:      # 同じものを2回登録しない
            r.dup.append((no, email))
            continue
        seen[key] = no
        r.entries.append((email, password, email.split("@")[0][:32]))
    return r


def _parse_bulk(raw: str) -> list[tuple[str, str, str]]:
    """読み取れた分だけを返す（従来どおりの入口）。"""
    return _parse_bulk_report(raw).entries


async def _existing_emails() -> set[str]:
    """すでに登録されているメールアドレス（小文字）。

    ⚠️ メールは暗号化して持っているので、**復号して**照合する。
       鍵が合わず読めないものは照合できないので黙って飛ばす
       （「登録済み」と誤って断るほうが困る）。
    """
    from sqlalchemy import select

    out: set[str] = set()
    async with session_scope() as s:
        rows = (await s.execute(select(McdAccount))).scalars().all()
        for acc in rows:
            mail = crypto.try_decrypt(acc.email_enc)
            if mail:
                out.add(mail.strip().lower())
    return out


def _few(lines, limit: int = 10) -> str:
    """多すぎる一覧は頭だけ出す（埋め込みの字数に収めるため）。"""
    got = list(lines)
    tail = f"\n…ほか {len(got) - limit} 件" if len(got) > limit else ""
    return "\n".join(got[:limit]) + tail


def _skip_report(parse: "BulkParse | None", already: list[str]) -> str:
    """飛ばした行の報告（登録済み・重複・読めない行）。

    ⚠️ 行番号を添える。どこを直せばよいか分からないと、打ち間違いを
       見つけられない。
    """
    blocks = []
    if already:
        blocks.append(
            f"{E.INFO} **すでに登録済み（飛ばしました）: {len(already)}件**\n"
            + _few(f"・{_mask_mail(m)}" for m in already)
        )
    if parse and parse.dup:
        blocks.append(
            f"{E.WARN} **同じメールの重複: {len(parse.dup)}件**\n"
            + _few(f"・{no}行目 {_mask_mail(m)}" for no, m in parse.dup)
        )
    if parse and parse.bad:
        blocks.append(
            f"{E.NG} **読めなかった行: {len(parse.bad)}件**\n"
            + _few(f"・{no}行目 `{t}`" for no, t in parse.bad)
        )
    return "\n\n".join(blocks)


def _skip_summary(r: "BulkParse") -> str:
    """読み込み直後に出す一行だけの要約。"""
    bits = []
    if r.dup:
        bits.append(f"重複 {len(r.dup)}件")
    if r.bad:
        bits.append(f"読めない行 {len(r.bad)}件")
    return f"\n{E.WARN} " + "・".join(bits) + " は飛ばします。" if bits else ""


def _bulk_preview(r: "BulkParse", limit: int) -> str:
    """下見（ドライラン）の結果。"""
    parts = [f"{E.OK} **登録できる行: {len(r.entries)}件**"]
    skip = _skip_report(r, r.already)
    if skip:
        parts.append(skip)
    if len(r.entries) > limit:
        parts.append(
            f"{E.NG} 一度に登録できるのは **{limit}件** までです"
            f"（{len(r.entries)}件ありました）。分けて実行してください。"
        )
    return "\n\n".join(parts)


async def _read_bulk_file(file: discord.Attachment) -> str | None:
    """添付ファイルを文字列にする。読めなければ None。

    ⚠️ 大きすぎるものは読まない。間違ったファイルを添付したときに、
       いきなり何百件もログインを試させないため。

    ⚠️ 文字コードを決め打ちしない。Excel から出すと cp932 になることが
       あり、UTF-8 だけだと「読めません」で止まってしまう。
    """
    if (file.size or 0) > 1_000_000:
        return None
    name = (file.filename or "").lower()
    if not name.endswith((".txt", ".csv")):
        return None
    try:
        data = await file.read()
    except Exception:
        log.exception("添付ファイルを取得できませんでした")
        return None
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _jst_text(dt) -> str:
    """日時を日本時間の文字列にする。

    ⚠️ **必ず as_utc を通す。** SQLite はタイムゾーンを保存しないので、
       読み戻した値は naive になる。そのまま astimezone すると
       「実行した機械の時間帯」として扱われ、何時間かずれる。
    """
    got = as_utc(dt)
    return f"{got.astimezone(JST):%Y-%m-%d %H:%M}" if got else ""


async def _export_csv() -> tuple[bytes, int]:
    """登録済みアカウントをCSVにする。(中身, 件数) を返す。

    ⚠️ **パスワードは保存していないので出せない。** これは控え・移行用で、
       このファイルから登録をやり直すことはできない（やり直すときは
       手元のメールとパスワードの一覧を使う）。

    ⚠️ メールはそのまま書く。伏せると控えの意味が無くなる。
       本人にだけ届く形で渡すこと。
    """
    import csv
    from sqlalchemy import select

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "表示名", "メール", "状態", "カードID", "プロキシ",
        "今日の注文数", "連続失敗", "最後に使った日時", "登録日時",
        "最後のエラー",
    ])
    n = 0
    async with session_scope() as s:
        rows = (await s.execute(
            select(McdAccount).order_by(McdAccount.id)
        )).scalars().all()
        for a in rows:
            w.writerow([
                a.id, a.label,
                crypto.try_decrypt(a.email_enc) or "(鍵が合わず読めません)",
                a.status, a.card_id or "", "あり" if a.proxy_url else "",
                a.orders_today, a.consecutive_failures,
                _jst_text(a.last_used_at), _jst_text(a.created_at),
                (a.last_error or "").replace("\n", " ")[:200],
            ])
            n += 1
    # ⚠️ BOM を付ける。付けないと Excel で開いたときに日本語が化ける。
    return buf.getvalue().encode("utf-8-sig"), n


def _parse_relabel(raw: str) -> tuple[dict[int, str], list[tuple[int, str]]]:
    """`ID,新しい名前` の行から {ID: 名前} を取り出す。

    ⚠️ 読めなかった行は行番号つきで返す（黙って飛ばさない）。
    """
    out: dict[int, str] = {}
    bad: list[tuple[int, str]] = []
    for no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = tail = ""
        for sep in (",", ":", "\t", " ", "="):
            if sep in line:
                head, _, tail = line.partition(sep)
                break
        head, tail = head.strip(), tail.strip()
        if not head.isdigit() or not tail:
            bad.append((no, line[:60]))
            continue
        out[int(head)] = tail[:32]
    return out, bad


class McdRelabelModal(discord.ui.Modal, title="表示名をまとめて付け替え"):
    lines = discord.ui.TextInput(
        label="IDと新しい名前（1行に1件）",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1500,
        placeholder="1,メイン\n2,予備\n（`/mcd list` でIDが分かります）",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select

        want, bad = _parse_relabel(str(self.lines.value))
        if not want:
            await interaction.response.send_message(
                embed=embeds.error(
                    "読み取れる行がありませんでした。\n"
                    "`ID,新しい名前` の形で1行に1件、入れてください。"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        done: list[str] = []
        missing: list[int] = []
        async with session_scope() as s:
            rows = (await s.execute(
                select(McdAccount).where(McdAccount.id.in_(list(want)))
            )).scalars().all()
            found = {a.id for a in rows}
            missing = [i for i in want if i not in found]
            for a in rows:
                before = a.label
                a.label = want[a.id]
                done.append(f"・`#{a.id}` {before} → **{a.label}**")

        for i in sorted(found):
            await audit.record(
                actor_id=interaction.user.id, actor_name=str(interaction.user),
                action="account.relabel", target=f"mcd:{i}", after=want[i],
            )

        parts = []
        if done:
            parts.append(f"{E.OK} **付け替えました（{len(done)}件）**\n" + _few(done))
        if missing:
            parts.append(
                f"{E.WARN} **見つからないID: {len(missing)}件**\n"
                + _few(f"・`#{i}`" for i in missing)
            )
        if bad:
            parts.append(
                f"{E.NG} **読めなかった行: {len(bad)}件**\n"
                + _few(f"・{no}行目 `{t}`" for no, t in bad)
            )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.PEOPLE} 表示名の付け替え",
                description="\n\n".join(parts)[:4000],
                color=embeds.GREEN if done else embeds.RED,
            ),
            ephemeral=True,
        )


def _bulk_report_text(added, failed, already, parse) -> str:
    """結果の全文。埋め込みに収まらないときファイルで渡す。

    ⚠️ ここには**メールをそのまま**書く。どの行を直すのか特定できないと
       直せない。画面には伏せ字で出し、控えのファイルにだけ残す。
    """
    L = ["まとめ登録の結果", "=" * 40, ""]
    L.append(f"■ 登録できた: {len(added)}件")
    for aid, label, card, n in added:
        L.append(f"   #{aid}\t{label}\tカード{n}枚\t{card}")
    L.append("")
    L.append(f"■ 登録できなかった: {len(failed)}件")
    for m, why in failed:
        L.append(f"   {m}\t{why}")
    L.append("")
    if already:
        L.append(f"■ すでに登録済みで飛ばした: {len(already)}件")
        L += [f"   {m}" for m in already]
        L.append("")
    if parse and parse.dup:
        L.append(f"■ 重複で飛ばした: {len(parse.dup)}件")
        L += [f"   {no}行目\t{m}" for no, m in parse.dup]
        L.append("")
    if parse and parse.bad:
        L.append(f"■ 読めなかった行: {len(parse.bad)}件")
        L += [f"   {no}行目\t{t}" for no, t in parse.bad]
    return "\n".join(L)


class BulkRetryView(discord.ui.View):
    """登録できなかった分だけ、やり直す。

    ⚠️ やり直しにはパスワードが要るので預かる。**時間切れで必ず捨てる**
       （要らなくなったものを持ち続けない）。
    """

    def __init__(self, owner_id: int, entries: list[tuple[str, str, str]]) -> None:
        super().__init__(timeout=OTP_WAIT_SECONDS)
        self.owner_id = owner_id
        self.entries = entries

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        # ⚠️ パスワードを抱えたままにしない
        self.entries = []

    @discord.ui.button(
        label="失敗した分だけやり直す", emoji=E.LOADING,
        style=discord.ButtonStyle.primary,
    )
    async def retry(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # ⚠️ 取り出したら手元からは消す。二度押しで二重に登録しない。
        entries, self.entries = self.entries, []
        if not entries:
            await interaction.response.send_message(
                embed=embeds.info(
                    "やり直す分がありません。"
                    "（時間が経ったか、すでにやり直し済みです）"
                ),
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.info(
                f"{E.LOADING} {len(entries)} 件をやり直しています…"
            ),
            view=None,
        )
        await _run_bulk(interaction, entries)


async def _after_mcd_added(
    interaction: discord.Interaction, account_id: int, label: str,
    cards: list[dict], *, note: str = "",
) -> None:
    """アカウントを登録したあとの案内。

    ⚠️ カードが**1枚だけなら自動で設定する**。毎回 `/mcd card` を
       打たせると手間なうえ、設定し忘れたアカウントは注文に選ばれず、
       使えるアカウントが静かに減っていく。

    ⚠️ **2枚以上なら自動で決めない。** どれで決済するかはお金の話で、
       勝手に選んでよいものではない。
    """
    usable = [c for c in cards if c.get("card_id")]
    head = f"アカウント `#{account_id}` **{label}** を登録しました。"
    if note:
        head += f"\n{E.INFO} {note}"

    # ⚠️ 1枚だけのときの扱いは **services/mcd/accounts.py に一本化**。
    #    ここで同じ判断を書き直すと、片方だけ直して食い違う。
    #    一覧はもう持っているので、渡して通信をやり直さない。
    picked, _n, auto_name = await mcd_accounts.auto_pick_card(
        account_id, cards=cards,
    )
    if picked:
        name = auto_name
        await interaction.followup.send(
            embed=embeds.ok(
                f"{head}\n\n{E.CARD} 決済カードも自動で設定しました"
                f"{f'（{name}）' if name else ''}。\n"
                f"{E.INFO} 変えたいときは `/mcd card {account_id}`。"
            ),
            ephemeral=True,
        )
        return

    if not usable:
        await interaction.followup.send(
            embed=embeds.warn(
                f"{head}\n\n"
                f"{E.WARN} **決済カードがありません。このままでは注文に使われません。**\n\n"
                "① 公式アプリ／サイトでこのアカウントにログイン\n"
                "② カードを登録（3Dセキュアの確認があります）\n"
                "③ 下の「カードを探す」を押す"
            ),
            view=CardScanView(interaction.user.id, [account_id]),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=embeds.ok(f"{head}\n決済に使うカードを選んでください。"),
        view=CardSelectView(interaction.user.id, account_id, usable),
        ephemeral=True,
    )


class CardScanView(discord.ui.View):
    """カード未設定のアカウントを、まとめて探し直す画面。

    ⚠️ 公式アプリで登録した直後に押してもらう想定。こちらから
       カードを登録する手段は無い（公式のAPIにその呼び出しが無く、
       3-Dセキュアの本人確認を通す必要があるため）。
    """

    def __init__(self, owner_id: int, account_ids: list[int]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.account_ids = list(account_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="カードを探す", emoji=E.SYNC,
                       style=discord.ButtonStyle.primary)
    async def scan(self, interaction: discord.Interaction,
                   btn: discord.ui.Button) -> None:
        # ⚠️ 1件ずつ通信するので時間がかかる。先に応答しておかないと
        #    「BOTは時間内に応答しませんでした」になる。
        btn.disabled = True
        await interaction.response.edit_message(
            embed=embeds.info(f"{E.LOADING} カードを探しています…"), view=self)

        done, several, none_, failed = [], [], [], []
        for aid in self.account_ids:
            try:
                ok_, count, name = await mcd_accounts.auto_pick_card(aid)
            except Exception:
                log.exception("カードの確認に失敗しました: %s", aid)
                failed.append(aid)
                continue
            if ok_:
                done.append((aid, name))
            elif count > 1:
                several.append((aid, count))
            elif count == 0:
                none_.append(aid)

        parts = []
        if done:
            parts.append(f"{E.OK} **設定しました**\n" + "\n".join(
                f"・`#{a}`　{n}" for a, n in done))
        if several:
            # ⚠️ 2枚以上あるときは選ばない。どれで決済するかはお金の話で、
            #    勝手に決めてよいものではない。
            parts.append(f"{E.WARN} **複数あるので選んでください**\n" + "\n".join(
                f"・`#{a}`　{c}枚 → `/mcd card {a}`" for a, c in several))
        if none_:
            parts.append(f"{E.NG} **まだカードがありません**\n" + "\n".join(
                f"・`#{a}`" for a in none_)
                + "\n公式アプリで登録してから、もう一度お試しください。")
        if failed:
            parts.append(f"{E.NG} **確認できませんでした**（ログインできない等）\n"
                         + "\n".join(f"・`#{a}`　→ `/mcd history {a}`" for a in failed))

        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title=f"{E.CARD} カードを探しました",
                description="\n\n".join(parts)[:4000] or "変化はありませんでした。",
                color=embeds.GREEN if done else embeds.YELLOW,
            ),
            view=None,
        )


class CardSelectView(discord.ui.View):
    def __init__(self, owner_id: int, account_id: int, cards: list[dict]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.account_id = account_id
        options = [
            discord.SelectOption(
                label=(c.get("masked") or c.get("card_id", ""))[:100],
                value=c["card_id"],
                description=f"{c.get('name', '')} {c.get('expiry', '')}".strip()[:100] or None,
            )
            for c in cards if c.get("card_id")
        ][:25]
        sel = discord.ui.Select(placeholder="決済カードを選んでください", options=options)
        sel.callback = self._on_pick
        self.add_item(sel)
        self._sel = sel
        # ⚠️ 期限も覚えておく。選んだあとで取り直すと通信が増えるし、
        #    控えが無いと「切れる前の知らせ」が出せない。
        self._expiry = {
            c["card_id"]: (c.get("expiry") or "")[:8]
            for c in cards if c.get("card_id")
        }

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        card_id = self._sel.values[0]
        async with session_scope() as s:
            acc = await s.get(McdAccount, self.account_id)
            if acc:
                acc.card_id = card_id
                acc.card_expiry = self._expiry.get(card_id) or None
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.ok(
                f"{E.CARD} 決済カードを設定しました。\n"
                f"アカウント `#{self.account_id}` は注文に使用できます。"
            ),
            view=None,
        )


# ============================================================
#  Kyash
# ============================================================

class KyashCredModal(discord.ui.Modal, title="Kyashアカウントを追加"):
    label = discord.ui.TextInput(label="表示名（任意）", required=False, max_length=32)
    email = discord.ui.TextInput(label="メールアドレス", required=True, max_length=128)
    password = discord.ui.TextInput(label="パスワード", required=True, max_length=128)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        email = str(self.email.value).strip()
        password = str(self.password.value)
        label = str(self.label.value).strip() or email.split("@")[0]

        await interaction.response.send_message(
            embed=embeds.info(f"{E.LOADING} ログインしています…"), ephemeral=True
        )

        client = KyashClient()
        try:
            need_otp = await client.start_login(email, password)
        except KyashError as e:
            await client.aclose()
            await interaction.edit_original_response(embed=embeds.error(str(e)))
            return

        if not need_otp:
            account_id = await _save_kyash(client, email, password, label)
            await client.aclose()
            await interaction.edit_original_response(
                embed=embeds.ok(f"Kyashアカウント `#{account_id}` **{label}** を登録しました。")
            )
            return

        await interaction.edit_original_response(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。\n"
                "SMS に届いた **6桁** を、下のボタンから入力してください。"
            ),
            view=KyashOtpView(interaction.user.id, client, email, password, label),
        )


class KyashOtpView(discord.ui.View):
    def __init__(self, owner_id, client, email, password, label) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.client = client
        self.email = email
        self.password = password
        self.label = label
        self.attempts = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_timeout(self) -> None:
        await self.client.aclose()

    @discord.ui.button(label="認証コードを入力", emoji=E.KEY, style=discord.ButtonStyle.primary)
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(KyashOtpModal(self))


class KyashOtpModal(discord.ui.Modal, title="認証コードの入力"):
    otp = discord.ui.TextInput(label="6桁の認証コード", required=True, min_length=4, max_length=8)

    def __init__(self, parent: KyashOtpView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self.parent
        try:
            await p.client.verify_otp(str(self.otp.value).strip(), p.email)
        except KyashError as e:
            p.attempts += 1
            if p.attempts >= 3:
                await p.client.aclose()
                p.stop()
                await interaction.followup.send(
                    embed=embeds.error("認証に3回失敗しました。最初からやり直してください。"),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                embed=embeds.error(f"{e}\nもう一度お試しください（残り {3 - p.attempts} 回）。"),
                ephemeral=True,
            )
            return

        account_id = await _save_kyash(p.client, p.email, p.password, p.label)
        await p.client.aclose()
        p.stop()
        await interaction.followup.send(
            embed=embeds.ok(
                f"Kyashアカウント `#{account_id}` **{p.label}** を登録しました。\n"
                f"{E.INFO} 次回以降は認証コードなしで再ログインできます。"
            ),
            ephemeral=True,
        )


async def _save_kyash(client: KyashClient, email: str, password: str, label: str) -> int:
    cipher = get_cipher()
    is_kyc = None
    balance = None
    try:
        is_kyc = (await client.get_profile()).is_kyc
        balance = (await client.get_wallet()).all_balance
    except KyashError:
        pass

    async with session_scope() as s:
        acc = KyashAccount(
            label=label,
            email_enc=cipher.encrypt(email),
            password_enc=cipher.encrypt(password),
            client_uuid=client.session.client_uuid,
            installation_uuid=client.session.installation_uuid,
            access_token_enc=cipher.encrypt(client.session.access_token),
            token_obtained_at=utcnow(),
            is_kyc=is_kyc,
            last_balance=balance,
        )
        s.add(acc)
        await s.flush()
        return acc.id


# ============================================================
#  コマンド
# ============================================================

# ⚠️ PayPay の登録はSMSのURLを挟むので、途中の状態を持っておく必要がある。
#    BOTを再起動すると消えるが、やり直せばよいだけなので保存はしない
#    （パスワードをディスクに残さないほうが安全）。
_PENDING_PAYPAY: dict[int, object] = {}


class AccountCog(commands.Cog):
    """マクドナルド／Kyash アカウントの管理"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    mcd = app_commands.Group(name="mcd", description="マクドナルドアカウント（管理者用）")
    kyash = app_commands.Group(name="kyash", description="Kyashアカウント（管理者用）")
    paypay = app_commands.Group(name="paypay", description="PayPayアカウント（管理者用）")

    # -- マクドナルド -------------------------------------------

    @mcd.command(name="add", description="マクドナルドアカウントを追加します")
    @admin_only()
    async def mcd_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(McdCredModal())

    @mcd.command(name="list", description="登録済みアカウントの一覧")
    @admin_only()
    async def mcd_list(self, interaction: discord.Interaction) -> None:
        from ui import admin_flows

        await admin_flows.show_accounts(interaction)

    @mcd.command(name="card", description="このアカウント1件の決済カードを選ぶ")
    @app_commands.describe(account_id="アカウントID")
    @admin_only()
    async def mcd_card(self, interaction: discord.Interaction, account_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        handle = None
        try:
            handle = await mcd_accounts.open_account(account_id)
            await handle.client.ensure_auth()
            cards = await handle.client.get_cards()
        except McdError as e:
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return
        finally:
            if handle:
                await handle.aclose()

        if not cards:
            await interaction.followup.send(
                embed=embeds.warn("登録されているカードがありません。"), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=embeds.info("決済に使うカードを選んでください。"),
            view=CardSelectView(interaction.user.id, account_id, cards),
            ephemeral=True,
        )

    @mcd.command(name="bulk", description="自分のアカウントをまとめて登録します")
    @app_commands.describe(
        file="メールとパスワードを並べたファイル（.txt / .csv・1行に1件）",
        dry_run="登録せずに、読み取れるかどうかだけ確かめます",
    )
    @admin_only()
    async def mcd_bulk(
        self, interaction: discord.Interaction,
        file: discord.Attachment | None = None,
        dry_run: bool = False,
    ) -> None:
        """⚠️ すでに持っているアカウントを取り込むもの。
           新しくアカウントを作る機能ではない。

        ⚠️ 貼り付けの入力窓は2000文字・{BULK_MAX_PASTE}件までしか入らない。
           件数が多いときはファイルを添付する。
        """
        # ファイルが無く、下見でもなければ、今までどおり貼り付け窓を出す
        if file is None and not dry_run:
            await interaction.response.send_modal(McdBulkModal())
            return
        if file is None:
            await interaction.response.send_message(
                embed=embeds.error(
                    "下見をするときは、ファイルを添付してください。\n"
                    "貼り付けで入れる場合は、何も付けずに実行してください。"
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = await _read_bulk_file(file)
        if raw is None:
            await interaction.followup.send(
                embed=embeds.error(
                    "ファイルを読めませんでした。\n"
                    "`.txt` か `.csv`（1MBまで）にしてください。"
                ),
                ephemeral=True,
            )
            return

        r = _parse_bulk_report(raw)
        # 下見でも「すでに登録済み」まで見せる。実行前に分かるほうがよい。
        have = await _existing_emails()
        r.already = [e for e, _, _ in r.entries if e.lower() in have]
        r.entries = [x for x in r.entries if x[0].lower() not in have]

        if dry_run:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"{E.INFO} 下見の結果（登録はしていません）",
                    description=_bulk_preview(r, BULK_MAX_FILE)[:4000],
                    color=embeds.BLUE,
                ),
                ephemeral=True,
            )
            return

        if not r.entries:
            await interaction.followup.send(
                embed=embeds.error(
                    "登録できる行がありませんでした。\n\n"
                    + _bulk_preview(r, BULK_MAX_FILE)[:3500]
                ),
                ephemeral=True,
            )
            return
        if len(r.entries) > BULK_MAX_FILE:
            await interaction.followup.send(
                embed=embeds.error(
                    f"一度に登録できるのは{BULK_MAX_FILE}件までです"
                    f"（{len(r.entries)}件ありました）。\n"
                    "ファイルを分けて実行してください。"
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=embeds.info(
                f"{E.LOADING} {len(r.entries)} 件を順に登録しています…\n"
                f"{E.INFO} 1件ずつログインするので時間がかかります。"
                + _skip_summary(r)
            ),
            ephemeral=True,
        )
        # ⚠️ already はここで渡す。_run_bulk 側でもう一度照合すると、
        #    すでに外してあるので二重に数えてしまう。
        await _run_bulk(interaction, r.entries, parse=r)

    @mcd.command(name="export", description="登録済みアカウントの一覧をCSVで出します")
    @admin_only()
    async def mcd_export(self, interaction: discord.Interaction) -> None:
        """⚠️ パスワードは保存していないので出せない。控え・移行用。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        data, n = await _export_csv()
        if not n:
            await interaction.followup.send(
                embed=embeds.info("登録されているアカウントがありません。"),
                ephemeral=True,
            )
            return
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.export", target="mcd", detail=f"{n}件",
        )
        await interaction.followup.send(
            embed=embeds.info(
                f"{E.OK} {n} 件を書き出しました。\n"
                f"{E.WARN} **パスワードは保存していないため入っていません。**\n"
                "このファイルから登録をやり直すことはできません（控え・確認用）。\n"
                f"{E.INFO} メールアドレスがそのまま入っています。扱いにご注意ください。"
            ),
            file=discord.File(
                io.BytesIO(data), filename="マクドナルドアカウント一覧.csv"
            ),
            ephemeral=True,
        )

    @mcd.command(name="relabel", description="表示名をまとめて付け替えます")
    @app_commands.describe(
        prefix="この名前＋連番に、全件まとめて付け替えます（例: メイン → メイン1, メイン2…）",
    )
    @admin_only()
    async def mcd_relabel(
        self, interaction: discord.Interaction, prefix: str | None = None,
    ) -> None:
        """表示名を付け替える。

        prefix を付けると全件を「名前＋連番」にそろえる。
        付けなければ `ID,新しい名前` を貼る窓を出す。
        """
        if not prefix:
            await interaction.response.send_modal(McdRelabelModal())
            return

        from sqlalchemy import select

        head = prefix.strip()[:24]
        if not head:
            await interaction.response.send_message(
                embed=embeds.error("名前を入れてください。"), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        rows_text = []
        async with session_scope() as s:
            rows = (await s.execute(
                select(McdAccount).order_by(McdAccount.id)
            )).scalars().all()
            for i, a in enumerate(rows, start=1):
                before = a.label
                a.label = f"{head}{i}"[:32]
                rows_text.append(f"・`#{a.id}` {before} → **{a.label}**")

        if not rows_text:
            await interaction.followup.send(
                embed=embeds.info("登録されているアカウントがありません。"),
                ephemeral=True,
            )
            return

        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.relabel", target="mcd:all",
            detail=f"{head}＋連番 / {len(rows_text)}件",
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.OK} 表示名をそろえました（{len(rows_text)}件）",
                description=_few(rows_text, 20)[:4000],
                color=embeds.GREEN,
            ),
            ephemeral=True,
        )

    @mcd.command(name="proxy", description="アカウントの通信の出口（プロキシ）を設定します")
    @app_commands.describe(
        url="http://ユーザー:パスワード@ホスト:ポート（`clear` と書くと解除）",
        account_id="1件だけに設定する場合のID（省略すると全件）",
        force="つながるか確かめずに、そのまま設定します",
    )
    @admin_only()
    async def mcd_proxy(
        self, interaction: discord.Interaction, url: str,
        account_id: int | None = None, force: bool = False,
    ) -> None:
        """アカウントごとの通信の出口を決める。

        ⚠️ McdAccount.proxy_url は読まれているのに、これまで**設定する
           手段が無かった**。そのため全アカウントが同じ出口から出ていた。

        ⚠️ パスワードを画面にも記録にも残さない。**必ず mask を通す。**

        ⚠️ 設定する前に、通るか・日本から出ているかを確かめる。
           海外の出口を入れると、注文も入金も黙って通らなくなる。
        """
        from sqlalchemy import select

        raw = url.strip()
        clearing = raw.lower() in ("clear", "none", "off", "解除", "なし", "")
        target = "" if clearing else raw

        if not clearing and (why := core_proxy.problem(target)):
            await interaction.response.send_message(
                embed=embeds.error(why), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        note = ""
        if not clearing and not force:
            okay, detail, country = await core_proxy.check_exit(target)
            if not okay:
                tip = core_proxy.advice(target, detail)
                await interaction.followup.send(
                    embed=embeds.error(
                        f"このプロキシは通りませんでした。\n`{detail}`"
                        + (f"\n\n{tip}" if tip else "")
                        + "\n\n確かめずに設定する場合は `force:True` を付けてください。"
                    ),
                    ephemeral=True,
                )
                return
            if country and country != "JP":
                await interaction.followup.send(
                    embed=embeds.error(
                        f"このプロキシは **日本の外（{country}）** から出ています"
                        f"（出口のIP `{detail}`）。\n"
                        "PayPay とマクドナルドは日本の外からの通信を弾くので、"
                        "このままだと注文も入金も通りません。\n\n"
                        "それでも設定する場合は `force:True` を付けてください。"
                    ),
                    ephemeral=True,
                )
                return
            where = "日本" if country == "JP" else "国は分かりませんでした"
            note = f"\n{E.OK} 出口のIP `{detail}`・{where}"

        changed: list[str] = []
        async with session_scope() as s:
            q = select(McdAccount).order_by(McdAccount.id)
            if account_id is not None:
                q = q.where(McdAccount.id == account_id)
            rows = (await s.execute(q)).scalars().all()
            for a in rows:
                a.proxy_url = target or None
                changed.append(f"・`#{a.id}` **{a.label}**")

        if not changed:
            await interaction.followup.send(
                embed=embeds.error(
                    f"`#{account_id}` が見つかりませんでした。"
                    if account_id is not None
                    else "登録されているアカウントがありません。"
                ),
                ephemeral=True,
            )
            return

        shown = core_proxy.mask(target) if target else "（解除）"
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.proxy",
            target=(f"mcd:{account_id}" if account_id is not None else "mcd:all"),
            after=shown,
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title=(f"{E.OK} プロキシを解除しました（{len(changed)}件）"
                       if clearing
                       else f"{E.OK} プロキシを設定しました（{len(changed)}件）"),
                description=(
                    f"出口: `{shown}`" + note + "\n\n" + _few(changed, 20)
                )[:4000],
                color=embeds.GREEN,
            ),
            ephemeral=True,
        )

    @mcd.command(name="cards", description="全アカウントのカードを一覧で見る・まとめて探す")
    @admin_only()
    async def mcd_cards(self, interaction: discord.Interaction) -> None:
        """どのアカウントにカードが付いているかを一目で見る。

        ⚠️ カードが無いアカウントは**注文に選ばれない**。気づかないと
           使えるアカウントが静かに減り、同時注文がさばけなくなる。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await mcd_accounts.card_overview()
        if not rows:
            await interaction.followup.send(
                embed=embeds.info("登録されているアカウントがありません。"),
                ephemeral=True)
            return

        lines, missing = [], []
        for r in rows:
            if r["has_card"]:
                lines.append(f"{E.OK} `#{r['id']}` **{r['label']}**　カードあり")
            else:
                lines.append(f"{E.NG} `#{r['id']}` **{r['label']}**　"
                             f"**カード未設定（注文に使われません）**")
                missing.append(r["id"])

        e = discord.Embed(
            title=f"{E.CARD} 決済カードの状況",
            description="\n".join(lines)[:4000],
            color=embeds.YELLOW if missing else embeds.GREEN,
        )
        e.add_field(
            name="使える数",
            value=f"**{len(rows) - len(missing)}** / {len(rows)} アカウント",
            inline=True,
        )
        if missing:
            e.add_field(
                name=f"{E.WARN} カードの付け方",
                value=(
                    "① 公式アプリ／サイトでそのアカウントにログイン\n"
                    "② カードを登録（3Dセキュアの確認があります）\n"
                    "③ 下の「カードを探す」を押す\n\n"
                    f"{E.INFO} 1枚だけなら自動で設定します。"
                    "複数ある場合は選んでいただきます。"
                ),
                inline=False,
            )
        await interaction.followup.send(
            embed=e,
            view=(CardScanView(interaction.user.id, missing) if missing else None),
            ephemeral=True,
        )

    @mcd.command(name="history", description="アカウントに起きたことの履歴を表示します")
    @app_commands.describe(account_id="アカウントID", count="表示する件数")
    @admin_only()
    async def mcd_history(
        self, interaction: discord.Interaction, account_id: int,
        count: app_commands.Range[int, 1, 30] = 10,
    ) -> None:
        """なぜ止まったのかを後から調べるための窓口。

        ⚠️ `last_error` は最後の1件しか残らない。止まった理由は
           止まった**後**に調べるものなので、そのときには上書き
           されている。だから経緯を別に残してある。
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        from db.models import McdAccount

        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            label = acc.label if acc else ""
            status = acc.status if acc else ""
        if not label:
            await interaction.followup.send(
                embed=embeds.error("そのアカウントが見つかりません。"), ephemeral=True)
            return

        events = await mcd_accounts.account_events(account_id, limit=count)
        if not events:
            await interaction.followup.send(
                embed=embeds.info(
                    f"**{label}** には、まだ記録がありません。\n"
                    f"{E.INFO} 失敗・隔離・復帰が起きると、ここに残ります。"
                ),
                ephemeral=True,
            )
            return

        MARK = {
            "failure": E.WARN, "degrade": E.YELLOW,
            "quarantine": E.NG, "recover": E.OK,
        }
        lines = []
        for e in events:
            when = e.created_at.astimezone(JST)
            kind = f"`{e.kind}`" if e.kind else ""
            head = f"{MARK.get(e.action, E.INFO)} **{when:%m/%d %H:%M}**　{kind}"
            body = (e.message or "").strip().replace("\n", " ")[:90]
            lines.append(head + (f"\n　{body}" if body else ""))

        # ⚠️ 何が多いかを数えて先に見せる。1件ずつ読ませると、
        #    「たまたま1回」と「ずっと同じ理由」の区別が付かない。
        from collections import Counter
        kinds = Counter(e.kind or "不明" for e in events if e.action != "recover")

        e_ = discord.Embed(
            title=f"{E.HISTORY} {label} の履歴",
            description="\n".join(lines)[:4000],
            color=embeds.BLUE,
        )
        e_.add_field(name="いまの状態", value=f"`{status}`", inline=True)
        if kinds:
            e_.add_field(
                name="理由の内訳",
                value="　".join(f"{k} **{n}**" for k, n in kinds.most_common(5)),
                inline=True,
            )
        e_.set_footer(
            text="原因が分からないときは、生の応答が残っています（管理者にご相談ください）"
        )
        await interaction.followup.send(embed=e_, ephemeral=True)

    @mcd.command(name="enable", description="アカウントを再び使えるようにします")
    @app_commands.describe(account_id="対象のアカウントID（/mcd list で確認）")
    @admin_only()
    async def mcd_enable(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            acc.status = "ACTIVE"
            acc.consecutive_failures = 0
            label = acc.label
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を有効にしました。"), ephemeral=True
        )

    @mcd.command(name="disable", description="アカウントを一時的に使わないようにします")
    @app_commands.describe(account_id="対象のアカウントID（/mcd list で確認）")
    @admin_only()
    async def mcd_disable(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            acc.status = "BANNED"
            label = acc.label
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を無効にしました。"), ephemeral=True
        )

    @mcd.command(name="remove", description="アカウントを削除します")
    @app_commands.describe(account_id="削除するアカウントID（/mcd list で確認）")
    @admin_only()
    async def mcd_remove(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(McdAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            label = acc.label
            token = await s.get(McdToken, account_id)
            if token:
                await s.delete(token)
            await s.delete(acc)
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.remove", target=f"mcd:{account_id}", before=label,
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を削除しました。"), ephemeral=True
        )

    @mcd.command(name="health", description="登録したアカウント1件ずつの生死を確かめる")
    @admin_only()
    async def mcd_health(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await mcd_accounts.healthcheck_all()
        if not results:
            await interaction.followup.send(
                embed=embeds.info("登録済みのアカウントがありません。"), ephemeral=True
            )
            return
        lines = [
            f"{E.OK if alive else E.NG} `#{aid}` **{label}**"
            for aid, label, alive in results
        ]
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.KEY} アカウントの状態", description="\n".join(lines), color=embeds.BLUE
            ),
            ephemeral=True,
        )

    # -- PayPay --------------------------------------------------

    @paypay.command(name="add", description="PayPayアカウントを追加します")
    @app_commands.describe(
        label="見分けるための名前", phone="電話番号", password="パスワード",
        proxy="このアカウント専用のプロキシ（省略可）",
    )
    @admin_only()
    async def paypay_add(
        self, interaction: discord.Interaction,
        label: app_commands.Range[str, 1, 50],
        phone: app_commands.Range[str, 1, 20],
        password: app_commands.Range[str, 1, 200],
        proxy: app_commands.Range[str, 1, 300] | None = None,
    ) -> None:
        """
        ⚠️ ログインは2段階。ここを実行すると SMS で **URL** が届くので、
           `/paypay confirm` にそのURLを貼ること（数字のOTPではない）。
        """
        from core.crypto import get_cipher
        from db.models import PayPayAccount
        from services.paypay import accounts as pp_accounts
        from services.paypay.client import PayPayClient, PayPayError

        await interaction.response.defer(ephemeral=True, thinking=True)

        # ⚠️ プロキシの形を**使う前に**確かめる。httpx は知らない形を渡されると
        #    ValueError を投げるので、そのままだと生のエラーで落ちる。
        #    `/config proxy set` では確かめているのに、ここだけ素通りだった。
        if proxy:
            why = core_proxy.problem(proxy.strip())
            if why:
                await interaction.followup.send(
                    embed=embeds.error(f"プロキシの指定が正しくありません。\n\n{why}"),
                    ephemeral=True,
                )
                return

        cipher = get_cipher()
        client = PayPayClient(proxy=proxy or pp_accounts.config_proxy())
        try:
            started = await client.start_login(phone, password)
        except PayPayError as e:
            await client.aclose()
            await interaction.followup.send(
                embed=embeds.error(f"ログインを始められませんでした。\n{e}"),
                ephemeral=True,
            )
            return

        async with session_scope() as s:
            acc = PayPayAccount(
                label=label,
                phone_enc=cipher.encrypt(phone),
                password_enc=cipher.encrypt(password),
                device_uuid=client.session.device_uuid,
                client_uuid=client.session.client_uuid,
                proxy_url=proxy,
                status=pp_accounts.STATUS_DEGRADED,   # 確認が済むまでは使わせない
            )
            s.add(acc)
            await s.flush()
            account_id = acc.id

        # 端末が登録済みなら、SMSなしでそのまま入れている
        if started.get("done"):
            await pp_accounts.save_session(account_id, started["session"])
            note = ""
            try:
                bal = await client.get_balance()
                note = f"\n{E.WALLET} いまの残高 **{embeds.yen(bal.all_balance)}**"
            except PayPayError:
                pass
            finally:
                await client.aclose()
            await interaction.followup.send(
                embed=embeds.ok(
                    f"**#{account_id} {label}** を登録し、ログインまで完了しました。"
                    f"{note}\n\n"
                    f"{E.INFO} SMSの確認は不要でした。\n"
                    f"{E.INFO} トークンは約90日もちます。"
                ),
                ephemeral=True,
            )
            return

        # ⚠️ 確認の手順で同じ検証子（PKCE）と Cookie が要る。
        #    クライアントごと預かっておく（作り直すと通らない）。
        _PENDING_PAYPAY[account_id] = client

        await interaction.followup.send(
            embed=embeds.ok(
                f"**#{account_id} {label}** を登録しました。\n\n"
                f"{E.INFO} SMS に **URL** が届きます（数字ではありません）。\n"
                f"`/paypay confirm account_id:{account_id} url:<届いたURL>` "
                "を実行してください。\n\n"
                f"{E.WARN} ログインに3回失敗するとアカウントが一時ロックされます。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="confirm", description="SMSで届いたURLを貼って確定します")
    @app_commands.describe(account_id="アカウントID", url="SMSで届いたURL")
    @admin_only()
    async def paypay_confirm(
        self, interaction: discord.Interaction, account_id: int,
        url: app_commands.Range[str, 1, 1000],
    ) -> None:
        from services.paypay import accounts as pp_accounts
        from services.paypay.client import PayPayError

        await interaction.response.defer(ephemeral=True, thinking=True)
        client = _PENDING_PAYPAY.get(account_id)
        if client is None:
            await interaction.followup.send(
                embed=embeds.error(
                    "この登録の続きが見つかりませんでした。\n"
                    "`/paypay add` からやり直してください。\n"
                    f"{E.INFO} BOTを再起動すると、やりかけの登録は消えます。"
                ),
                ephemeral=True,
            )
            return
        try:
            session = await client.login_confirm(url)
        except PayPayError as e:
            await interaction.followup.send(
                embed=embeds.error(f"確認できませんでした。\n{e}"), ephemeral=True
            )
            return
        finally:
            pass

        await pp_accounts.save_session(account_id, session)
        _PENDING_PAYPAY.pop(account_id, None)
        try:
            balance = await client.get_balance()
            note = f"\n{E.WALLET} いまの残高 **{embeds.yen(balance.all_balance)}**"
        except PayPayError:
            note = ""
        finally:
            await client.aclose()

        await interaction.followup.send(
            embed=embeds.ok(
                f"**#{account_id}** のログインが完了しました。{note}\n\n"
                f"{E.INFO} トークンは約90日もちます。"
                "期限が近づくと管理者へお知らせします。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="methods", description="チャージに使える決済を選びます")
    @app_commands.describe(mode="利用者が使えるチャージ方法")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Kyash と PayPay の両方（推奨）", value="both"),
        app_commands.Choice(name="Kyash のみ", value="kyash"),
        app_commands.Choice(name="PayPay のみ", value="paypay"),
    ])
    @admin_only()
    async def paypay_methods(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        from core import settings

        await settings.set_value(
            "charge_methods", mode.value, updated_by=interaction.user.id
        )
        body = f"チャージに使える決済を **{mode.name}** にしました。\n"
        if mode.value in ("both", "paypay"):
            body += (
                f"\n{E.WARN} PayPay は **日本からしかアクセスできません**。"
                "国外で動かす場合は `/paypay proxy` でプロキシをご指定ください。\n"
                f"{E.INFO} `/paypay add` で口座を登録してください。"
            )
        body += f"\n{E.INFO} チャージパネルの文面も変わります（`/panel refresh`）。"
        await interaction.response.send_message(embed=embeds.ok(body), ephemeral=True)

    @paypay.command(name="proxy", description="PayPay用のプロキシ（全口座の既定）")
    @app_commands.describe(url="http://user:pass@host:port 形式。空で解除")
    @admin_only()
    async def paypay_proxy(
        self, interaction: discord.Interaction,
        url: app_commands.Range[str, 0, 300] = "",
    ) -> None:
        """
        ⚠️ 口座ごとに指定があれば、そちらが優先される。
           ここは「指定が無い口座の既定」。
        """
        from core import proxy as proxy_mod
        from core import settings

        # ⚠️ ここでも形を確かめる。確かめないと、使えない値が入ったまま
        #    「設定できた」と見えてしまい、実際には素のIPで出ていく。
        why = proxy_mod.problem(url.strip())
        if why:
            await interaction.response.send_message(
                embed=embeds.error(f"この値では設定できません。\n{why}"),
                ephemeral=True,
            )
            return

        await settings.set_value(
            "paypay_proxy", url.strip(), updated_by=interaction.user.id
        )
        await interaction.response.send_message(
            embed=embeds.ok(
                "PayPay用のプロキシを設定しました。\n"
                f"{E.INFO} 口座ごとに指定がある場合は、そちらが優先されます。"
                if url.strip() else "PayPay用のプロキシを解除しました。"
            ),
            ephemeral=True,
        )

    @paypay.command(name="list", description="登録済みPayPayアカウントの一覧")
    @admin_only()
    async def paypay_list(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select
        from db.models import PayPayAccount
        from services.paypay import accounts as pp_accounts

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with session_scope() as s:
            rows = (await s.execute(select(PayPayAccount))).scalars().all()
            items = [
                (a.id, a.label, a.status, pp_accounts.token_days_left(a),
                 a.last_balance, a.proxy_url, a.last_error)
                for a in rows
            ]
        if not items:
            await interaction.followup.send(
                embed=embeds.info(
                    "まだ登録がありません。`/paypay add` から追加してください。"
                ),
                ephemeral=True,
            )
            return
        lines = []
        for aid, label, status, left, bal, proxy, err in items:
            mark = {"ACTIVE": E.GREEN, "DEGRADED": E.YELLOW}.get(status, E.RED)
            days = f"残り{int(left)}日" if left is not None else "未ログイン"
            lines.append(
                f"{mark} `#{aid}` **{label}**　{days}"
                + (f"　残高 {embeds.yen(int(bal))}" if bal is not None else "")
                + (f"　{E.INFO}プロキシ有" if proxy else "")
                + (f"\n　　{E.WARN} {err[:60]}" if err else "")
            )
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"{E.YEN} PayPayアカウント",
                description="\n".join(lines), color=embeds.BLUE,
            ),
            ephemeral=True,
        )

    # -- Kyash ---------------------------------------------------

    @kyash.command(name="add", description="Kyashアカウントを追加します")
    @admin_only()
    async def kyash_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(KyashCredModal())

    @kyash.command(name="list", description="登録済みKyashアカウントの一覧")
    @admin_only()
    async def kyash_list(self, interaction: discord.Interaction) -> None:
        from ui import admin_flows

        await admin_flows.show_accounts(interaction)

    @kyash.command(name="relogin", description="Kyashへ再ログインします")
    @app_commands.describe(account_id="アカウントID")
    @admin_only()
    async def kyash_relogin(self, interaction: discord.Interaction, account_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        cipher = get_cipher()
        async with session_scope() as s:
            acc = await s.get(KyashAccount, account_id)
            if acc is None:
                await interaction.followup.send(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            # ⚠️ 鍵が合わないと復号で落ちる。生の例外を利用者に見せない。
            #    バックアップを鍵なしで戻すと、全アカウントがこうなる。
            if crypto.is_unreadable(acc.email_enc) or crypto.is_unreadable(
                acc.password_enc
            ):
                await interaction.followup.send(
                    embed=embeds.error(
                        "このアカウントの登録内容を、いまの鍵では読み取れませんでした。\n\n"
                        f"{E.WARN} `data/encryption_key.txt` が、登録したときと"
                        "違うものになっています。バックアップを鍵なしで戻した場合や、"
                        "`ENCRYPTION_KEY` を変えた場合に起きます。\n\n"
                        "元の鍵を戻すか、登録し直してください。"
                    ),
                    ephemeral=True,
                )
                return
            email = crypto.try_decrypt(acc.email_enc) or ""
            password = crypto.try_decrypt(acc.password_enc) or ""
            label = acc.label
            client_uuid = acc.client_uuid or ""
            installation_uuid = acc.installation_uuid or ""

        if not password:
            await interaction.followup.send(
                embed=embeds.error(
                    "パスワードが保存されていないため自動で再ログインできません。"
                    "`/kyash add` で登録し直してください。"
                ),
                ephemeral=True,
            )
            return

        from services.kyash.client import KyashSession

        client = KyashClient(
            KyashSession(client_uuid=client_uuid, installation_uuid=installation_uuid)
        )
        try:
            need_otp = await client.start_login(email, password)
        except KyashError as e:
            await client.aclose()
            await interaction.followup.send(embed=embeds.error(str(e)), ephemeral=True)
            return

        if not need_otp:
            async with session_scope() as s:
                acc = await s.get(KyashAccount, account_id)
                acc.access_token_enc = cipher.encrypt(client.session.access_token)
                acc.token_obtained_at = utcnow()
                acc.status = "ACTIVE"
                acc.last_error = None
            await client.aclose()
            await interaction.followup.send(
                embed=embeds.ok(f"`#{account_id}` **{label}** に再ログインしました。"),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=embeds.info(
                f"{E.KEY} 認証コードを送信しました。下のボタンから入力してください。"
            ),
            view=KyashOtpView(interaction.user.id, client, email, password, label),
            ephemeral=True,
        )

    @kyash.command(name="remove", description="Kyashアカウントを削除します")
    @app_commands.describe(account_id="削除するアカウントID（/kyash list で確認）")
    @admin_only()
    async def kyash_remove(self, interaction: discord.Interaction, account_id: int) -> None:
        async with session_scope() as s:
            acc = await s.get(KyashAccount, account_id)
            if acc is None:
                await interaction.response.send_message(
                    embed=embeds.error("アカウントが見つかりません。"), ephemeral=True
                )
                return
            label = acc.label
            await s.delete(acc)
        await audit.record(
            actor_id=interaction.user.id, actor_name=str(interaction.user),
            action="account.remove", target=f"kyash:{account_id}", before=label,
        )
        await interaction.response.send_message(
            embed=embeds.ok(f"`#{account_id}` **{label}** を削除しました。"), ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if not await handle_check_failure(interaction, error):
            log.exception("account コマンドでエラー", exc_info=error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AccountCog(bot))
