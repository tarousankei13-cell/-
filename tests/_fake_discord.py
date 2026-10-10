"""
テスト用の偽Interaction

Discordに接続せずに、パネルやボタンの流れを確かめるために使う。
「応答を2回しようとした」「応答せずに終わった」といった間違いも検出する。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord


class FakeResponse:
    def __init__(self, parent: "FakeInteraction") -> None:
        self.parent = parent
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def _mark(self, kind: str, **kw) -> None:
        if self._done:
            raise RuntimeError(
                f"応答済みなのに {kind} を呼びました"
                f"（先に {self.parent.actions[-1][0] if self.parent.actions else '?'} を実行済み）"
            )
        self._done = True
        self.parent.actions.append((kind, kw))

    async def send_message(self, content=None, *, embed=None, view=None,
                           ephemeral=False, file=None, files=None, **kw):
        # ⚠️ file も残すこと。画像認証のように「画像が出たか」を
        #    確かめたい場面があり、捨てると検証できない。
        self._mark("send_message", content=content, embed=embed, view=view,
                   ephemeral=ephemeral, file=file, files=files)

    async def edit_message(self, *, content=None, embed=None, view=None, **kw):
        self._mark("edit_message", content=content, embed=embed, view=view)

    async def send_modal(self, modal):
        self._mark("send_modal", modal=modal)

    async def defer(self, *, ephemeral=False, thinking=False):
        self._mark("defer", ephemeral=ephemeral, thinking=thinking)


class FakeFollowup:
    def __init__(self, parent: "FakeInteraction") -> None:
        self.parent = parent

    async def send(self, content=None, *, embed=None, embeds=None, view=None,
                   ephemeral=False, file=None, **kw):
        if not self.parent.response.is_done():
            raise RuntimeError("defer していないのに followup.send を呼びました")
        self.parent.actions.append(
            ("followup", {"content": content, "embed": embed, "embeds": embeds,
                          "view": view, "file": file, "ephemeral": ephemeral})
        )


class FakeUser:
    def __init__(self, user_id: int, name="テスト太郎", roles=()) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.roles = list(roles)
        self.dm_ok = True
        self.dms: list[dict] = []
        self.guild_permissions = discord.Permissions(administrator=False)
        # 紹介プログラムの条件判定に使う（既定は条件を満たす古さ）
        self.created_at = datetime.now(timezone.utc) - timedelta(days=365)
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=30)

    async def send(self, **kw):
        if not self.dm_ok:
            raise discord.Forbidden(_FakeResp(403), "DMを拒否しています")
        self.dms.append(kw)


class _FakeResp:
    def __init__(self, status): self.status = status; self.reason = ""


class FakeClient:
    def __init__(self) -> None:
        self.owner_ids = {1}
        self.admin_role_ids = set()
        self.sent: dict[int, list] = {}
        self.users: dict[int, "FakeUser"] = {}

    def get_channel(self, cid):
        return FakeChannel(cid, self)

    # -- 利用者の取得（一斉通知の検証に使う） --
    def get_user(self, uid):
        return self.users.get(int(uid))

    async def fetch_user(self, uid):
        u = self.users.get(int(uid))
        if u is None:
            raise discord.NotFound(_FakeResp(404), "見つかりません")
        return u


class FakeChannel:
    """
    チャンネル。2つの使い方を兼ねる。

      - client 付き … client.sent[cid] に溜める（通知先の検証用）
      - client 無し … self.sent に溜める（操作したチャンネルの検証用）
    """

    def __init__(self, cid: int = 1000, client=None, name: str = "general") -> None:
        self.id = cid
        self.client = client
        self.name = name
        self.sent: list[dict] = []
        self.guild = None
        self.can_invite = True
        self.invite_seq = 0
        self.invites_made: list["FakeInvite"] = []

    def permissions_for(self, _member):
        return None   # 判定できないときは送れる扱い（ui/balance_panel 参照）

    async def create_invite(self, **kw):
        """招待リンクの発行。can_invite=False にすると権限なしを再現する。"""
        if not self.can_invite:
            raise discord.Forbidden(_FakeResp(403), "招待を作れません")
        self.invite_seq += 1
        code = f"code{self.invite_seq:04d}"
        inv = FakeInvite(code, channel=self)
        self.invites_made.append(inv)
        if self.guild is not None:
            self.guild.invite_list.append(inv)
        return inv

    async def send(self, content=None, **kw):
        # ⚠️ 本物は content を位置引数で受け取れる（channel.send("文字列")）。
        #    ここで受け取れないと TypeError になり、送った側が
        #    例外を握りつぶしていると「送っていない」ように見えてしまう。
        if content is not None:
            kw["content"] = content
        self.sent.append(kw)
        if self.client is not None:
            self.client.sent.setdefault(self.id, []).append(kw)


class FakeInvite:
    def __init__(self, code: str, channel=None, inviter=None, uses: int = 0) -> None:
        self.code = code
        self.channel = channel
        self.inviter = inviter
        self.uses = uses
        self.url = f"https://discord.gg/{code}"


class FakeGuild:
    def __init__(self, gid: int = 1, name: str = "マクドナルドジャパン") -> None:
        self.id = gid
        self.name = name
        self.me = None
        self.channels: dict[int, "FakeChannel"] = {}
        self.invite_list: list[FakeInvite] = []
        self.can_list_invites = True

    def add_channel(self, channel: "FakeChannel") -> "FakeChannel":
        channel.guild = self
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, cid):
        return self.channels.get(int(cid))

    async def invites(self):
        if not self.can_list_invites:
            raise discord.Forbidden(_FakeResp(403), "招待を見られません")
        return list(self.invite_list)


class FakeInteraction:
    def __init__(
        self, user: FakeUser, client: FakeClient | None = None,
        *, channel: "FakeChannel | None" = None, guild: "FakeGuild | None" = None,
    ) -> None:
        self.user = user
        self.client = client or FakeClient()
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.actions: list[tuple[str, dict]] = []
        self.channel = channel if channel is not None else FakeChannel()
        self.guild = guild if guild is not None else FakeGuild()
        # ⚠️ 本物の Interaction は必ず持っている（サーバー外なら None）。
        #    偽物に無いと、サーバーを見る処理が AttributeError で落ちる。
        self.guild_id = getattr(self.guild, "id", None)
        self.channel_id = getattr(self.channel, "id", None)
        self.message = None

    @property
    def public_embeds(self) -> list:
        """チャンネルへ公開で出した埋め込み（本人だけに見えるものは含まない）"""
        return [kw["embed"] for kw in getattr(self.channel, "sent", []) if kw.get("embed")]

    async def edit_original_response(self, *, content=None, embed=None, view=None, **kw):
        self.actions.append(("edit_original", {"content": content, "embed": embed, "view": view}))

    # -- 検査用 --
    @property
    def kinds(self) -> list[str]:
        return [a[0] for a in self.actions]

    def last_embed(self):
        for kind, kw in reversed(self.actions):
            if kw.get("embed") is not None:
                return kw["embed"]
        return None

    def last_view(self):
        for kind, kw in reversed(self.actions):
            if kw.get("view") is not None:
                return kw["view"]
        return None

    def last_modal(self):
        for kind, kw in reversed(self.actions):
            if kw.get("modal") is not None:
                return kw["modal"]
        return None

    def text(self) -> str:
        """出した内容をまとめて文字列にする（文言の確認用）"""
        parts = []
        for _, kw in self.actions:
            if kw.get("content"):
                parts.append(str(kw["content"]))
            group = list(kw.get("embeds") or [])
            if kw.get("embed") is not None:
                group.append(kw["embed"])
            for e in group:
                parts += [e.title or "", e.description or ""]
                parts += [f"{f.name}{f.value}" for f in e.fields]
                if getattr(e, "footer", None) is not None and e.footer.text:
                    parts.append(e.footer.text)
        return "\n".join(parts)


def press(view, custom_id_or_label):
    """ボタンを名前かcustom_idで探す"""
    for c in view.children:
        if getattr(c, "custom_id", None) == custom_id_or_label:
            return c
        if getattr(c, "label", None) == custom_id_or_label:
            return c
    raise AssertionError(
        f"ボタン {custom_id_or_label!r} が見つかりません: "
        f"{[getattr(c, 'label', getattr(c, 'placeholder', '?')) for c in view.children]}"
    )
