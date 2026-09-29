"""
テスト用の偽Interaction

Discordに接続せずに、パネルやボタンの流れを確かめるために使う。
「応答を2回しようとした」「応答せずに終わった」といった間違いも検出する。
"""
from __future__ import annotations

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

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, **kw):
        self._mark("send_message", content=content, embed=embed, view=view, ephemeral=ephemeral)

    async def edit_message(self, *, content=None, embed=None, view=None, **kw):
        self._mark("edit_message", content=content, embed=embed, view=view)

    async def send_modal(self, modal):
        self._mark("send_modal", modal=modal)

    async def defer(self, *, ephemeral=False, thinking=False):
        self._mark("defer", ephemeral=ephemeral, thinking=thinking)


class FakeFollowup:
    def __init__(self, parent: "FakeInteraction") -> None:
        self.parent = parent

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False, file=None, **kw):
        if not self.parent.response.is_done():
            raise RuntimeError("defer していないのに followup.send を呼びました")
        self.parent.actions.append(
            ("followup", {"content": content, "embed": embed, "view": view, "file": file})
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

    def get_channel(self, cid):
        return FakeChannel(cid, self)


class FakeChannel:
    def __init__(self, cid, client): self.id = cid; self.client = client
    async def send(self, **kw):
        self.client.sent.setdefault(self.id, []).append(kw)


class FakeInteraction:
    def __init__(self, user: FakeUser, client: FakeClient | None = None) -> None:
        self.user = user
        self.client = client or FakeClient()
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.actions: list[tuple[str, dict]] = []
        self.guild = None
        self.message = None

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
            e = kw.get("embed")
            if e is not None:
                parts += [e.title or "", e.description or ""]
                parts += [f"{f.name}{f.value}" for f in e.fields]
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
