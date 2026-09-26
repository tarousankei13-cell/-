"""全スラッシュコマンドを実際に実行するスモークテスト。

型チェック (mypy) では検出できない実行時の不具合 (存在しない属性の参照、
ヘルパへの誤った引数、応答フローの誤りなど) を、101 個すべてのコマンドを
スタブ化した Interaction で呼び出して検出する。

確認ボタンは自動承認されるため、危険な操作は最後にまとめて実行する。

実行:
    python3 tests/test_commands_smoke.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_smoke"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "smoke.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"
config.RANKING_DEBOUNCE_SECONDS = 0.01

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

import requests  # noqa: E402

import ui  # noqa: E402
import utils  # noqa: E402


def _blocked(*args: object, **kwargs: object):
    """スモークテストでは外部通信を行わない。"""
    raise requests.exceptions.ConnectionError("smoke test: network disabled")


requests.get = _blocked  # type: ignore[assignment]
requests.post = _blocked  # type: ignore[assignment]
requests.put = _blocked  # type: ignore[assignment]

OK: list[str] = []
FAILURES: list[tuple[str, str]] = []

#: 確認ボタンで自動承認されると影響が大きいため最後に実行するコマンド
DESTRUCTIVE = {
    "maintenance on", "emergency_stop on", "campaign end", "config reset",
    "kyash logout", "server deny", "server suspend", "data delete",
}
#: 実行対象から外すコマンドとその理由
SKIPPED: dict[str, str] = {
    "server sync": "Discord へのコマンド同期を伴うため",
}


# ---------------------------------------------------------------------------
# Discord スタブ
# ---------------------------------------------------------------------------
class StubPermissions:
    def __init__(self, **kwargs: bool) -> None:
        self._values = {
            "manage_roles": True, "manage_guild": True, "create_instant_invite": True,
            "send_messages": True, "embed_links": True, "view_channel": True,
            "read_message_history": True, "administrator": False,
        }
        self._values.update(kwargs)

    def __getattr__(self, name: str) -> bool:
        return self._values.get(name, False)


class StubRole:
    def __init__(self, role_id: int, name: str, position: int = 5) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False
        self.mention = f"<@&{role_id}>"
        self.members: list["StubMember"] = []

    def is_default(self) -> bool:
        return self.position == 0

    def __ge__(self, other: "StubRole") -> bool:
        return self.position >= other.position

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StubRole) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)


class StubMember:
    def __init__(self, user_id: int, guild: "StubGuild", *, bot: bool = False,
                 created_days_ago: int = 120) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = bot
        self.name = f"user{user_id}"
        self.display_name = self.name
        self.mention = f"<@{user_id}>"
        self.created_at = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=10)
        self.roles: list[StubRole] = []
        self.guild_permissions = StubPermissions()
        self.top_role = StubRole(99_999, "top", position=100)
        self.dms: list[discord.Embed] = []

    async def add_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)

    async def send(self, **kwargs: object) -> None:
        embed = kwargs.get("embed")
        if isinstance(embed, discord.Embed):
            self.dms.append(embed)


class StubInvite:
    def __init__(self, code: str, uses: int = 0) -> None:
        self.code = code
        self.uses = uses
        self.url = f"https://discord.gg/{code}"


class StubMessage:
    _next_id = 900_000

    def __init__(self, channel: "StubChannel", embed: discord.Embed | None) -> None:
        StubMessage._next_id += 1
        self.id = StubMessage._next_id
        self.channel = channel
        self.embeds = [embed] if embed else []

    async def edit(self, **kwargs: object) -> None:
        embed = kwargs.get("embed")
        if isinstance(embed, discord.Embed):
            self.embeds = [embed]


class StubChannel(discord.TextChannel):
    """``discord.TextChannel`` のサブクラス。

    製品コードが ``isinstance(channel, discord.TextChannel)`` で検査するため、
    実際の型を継承して設置系コマンドの経路もテストできるようにする。
    """

    def __init__(self, channel_id: int, guild: "StubGuild", *, public: bool = False) -> None:
        self.id = channel_id
        self.guild = guild  # type: ignore[assignment]
        self.name = f"channel{channel_id}"
        self.messages: dict[int, StubMessage] = {}
        self.sent: list[discord.Embed] = []
        self._public = public

    @property
    def mention(self) -> str:  # type: ignore[override]
        return f"<#{self.id}>"

    def permissions_for(self, member: object) -> StubPermissions:  # type: ignore[override]
        if isinstance(member, StubRole):  # @everyone の判定
            return StubPermissions(view_channel=self._public)
        return StubPermissions()

    async def send(self, **kwargs: object) -> StubMessage:  # type: ignore[override]
        embed = kwargs.get("embed")
        if isinstance(embed, discord.Embed):
            self.sent.append(embed)
        message = StubMessage(self, embed if isinstance(embed, discord.Embed) else None)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> StubMessage:  # type: ignore[override]
        message = self.messages.get(message_id)
        if message is None:
            raise discord.NotFound(_FakeResponse(), "not found")
        return message

    async def create_invite(self, **kwargs: object) -> StubInvite:  # type: ignore[override]
        invite = StubInvite(f"code{len(self.guild.invite_objects) + 1}")
        self.guild.invite_objects.append(invite)
        return invite


class _FakeResponse:
    status = 404
    reason = "Not Found"


class StubGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.name = "スモークテストサーバー"
        self.icon = None
        self.owner_id = 1
        self.members: dict[int, StubMember] = {}
        self.roles: dict[int, StubRole] = {}
        self.invite_objects: list[StubInvite] = []
        self.channel = StubChannel(600_001, self)
        self.public_channel = StubChannel(600_002, self, public=True)
        self.text_channels = [self.channel, self.public_channel]
        self.rules_channel = None
        self.system_channel = None
        self.default_role = StubRole(guild_id, "@everyone", position=0)
        self.me = StubMember(999_999, self)

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        for channel in self.text_channels:
            if channel.id == channel_id:
                return channel
        return None

    async def invites(self) -> list[StubInvite]:
        return list(self.invite_objects)


class StubResponse:
    def __init__(self, record: "StubInteraction") -> None:
        self._record = record
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs: object) -> None:
        self._done = True
        self._record.calls.append("defer")

    async def send_message(self, **kwargs: object) -> None:
        self._done = True
        self._record.calls.append("send_message")
        self._record.record(kwargs)

    async def edit_message(self, **kwargs: object) -> None:
        self._done = True
        self._record.calls.append("edit_message")
        self._record.record(kwargs)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self._record.calls.append("send_modal")
        self._record.modals.append(modal)


class StubFollowup:
    def __init__(self, record: "StubInteraction") -> None:
        self._record = record

    async def send(self, **kwargs: object) -> StubMessage:
        self._record.calls.append("followup")
        self._record.record(kwargs)
        return StubMessage(self._record.channel, None)


class StubInteraction:
    def __init__(self, bot: object, guild: StubGuild, user: StubMember) -> None:
        self.client = bot
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.channel = guild.channel
        self.command = None
        self.message: object | None = None   # 審査カードのボタン操作で使う
        self.calls: list[str] = []
        self.embeds: list[discord.Embed] = []
        self.files: list[object] = []
        self.modals: list[discord.ui.Modal] = []
        self.response = StubResponse(self)
        self.followup = StubFollowup(self)

    def record(self, kwargs: dict) -> None:
        embed = kwargs.get("embed")
        if isinstance(embed, discord.Embed):
            self.embeds.append(embed)
        if kwargs.get("file") is not None:
            self.files.append(kwargs["file"])

    @property
    def titles(self) -> list[str]:
        return [e.title or "" for e in self.embeds]


class StubAttachment:
    def __init__(self, payload: dict) -> None:
        self.size = 256
        self.filename = "config.json"
        self._data = json.dumps(payload).encode("utf-8")

    async def read(self) -> bytes:
        return self._data


# ---------------------------------------------------------------------------
# 引数の自動生成
# ---------------------------------------------------------------------------
def _choice_annotated(command: app_commands.Command, name: str) -> bool:
    """その引数が ``app_commands.Choice[...]`` 注釈かどうか。

    discord.py は ``Choice`` 注釈のときだけ Choice オブジェクトを渡し、
    ``str`` 注釈のときは生の値を渡すため、テストでも同じ形を再現する。
    """
    import typing

    try:
        hints = typing.get_type_hints(command.callback, include_extras=True)
    except Exception:  # noqa: BLE001
        return False
    annotation = hints.get(name)
    if annotation is None:
        return False
    for candidate in (annotation, *typing.get_args(annotation)):
        if typing.get_origin(candidate) is app_commands.Choice or candidate is app_commands.Choice:
            return True
    return False


def build_arguments(
    command: app_commands.Command, context: dict[str, object]
) -> dict[str, object]:
    """コマンドのパラメータ定義から妥当な引数を生成する。"""
    overrides: dict[str, object] = {
        "guild_id": str(context["guild_id"]),
        "transaction_id": context["tx_id"],
        "history_id": context["history_id"],
        "purchase_id": context["purchase_id"],
        "item_id": context["item_id"],
        "record_id": context["record_id"],
        "disable_message_id": str(context["panel_message_id"]),
        "until": "2032-01-01",
        "ends_at": "2032-01-01",
        "date": datetime.now(utils.JST).strftime("%Y-%m-%d"),
        "url": "https://example.com/heartbeat",
        "color": "1B1B1F",
        "charge_rate": "140",
        "rate": "140",
        "directory": str(SCRATCH / "secondary"),
        "title": "テストパネル",
        "reason": "スモークテスト",
        "name": "スモークテスト",
        "description": "スモークテストの説明",
        "file": context["attachment"],
    }
    kwargs: dict[str, object] = {}
    for parameter in command.parameters:
        name = parameter.name
        if name in overrides and overrides[name] is not None:
            kwargs[name] = overrides[name]
            continue
        if parameter.choices:
            choice = parameter.choices[0]
            kwargs[name] = choice if _choice_annotated(command, name) else choice.value
            continue
        option_type = parameter.type
        if option_type is discord.AppCommandOptionType.user:
            kwargs[name] = context["target_member"]
            if name in ("from_user",):
                kwargs[name] = context["rich_member"]
            if name in ("to_user",):
                kwargs[name] = context["target_member"]
        elif option_type is discord.AppCommandOptionType.role:
            kwargs[name] = context["role"]
        elif option_type is discord.AppCommandOptionType.channel:
            kwargs[name] = context["channel"]
        elif option_type is discord.AppCommandOptionType.attachment:
            kwargs[name] = context["attachment"]
        elif option_type is discord.AppCommandOptionType.boolean:
            kwargs[name] = bool(parameter.default) if parameter.default is not None else False
        elif option_type is discord.AppCommandOptionType.integer:
            minimum = parameter.min_value if parameter.min_value is not None else 1
            kwargs[name] = max(int(minimum), 1)
        elif option_type is discord.AppCommandOptionType.number:
            kwargs[name] = 1
        else:
            kwargs[name] = "テスト"
    return kwargs


def qualified(command: app_commands.Command) -> str:
    return command.qualified_name


def iter_commands(tree: app_commands.CommandTree):
    for command in tree.get_commands():
        if isinstance(command, app_commands.Group):
            for sub in command.commands:
                yield sub
        else:
            yield command


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    # 確認ボタンは自動承認する (_confirm 自体は実コードを通す)
    async def auto_confirm(self: ui.ConfirmView) -> bool:
        self.value = True
        return False

    ui.ConfirmView.wait = auto_confirm  # type: ignore[assignment]

    import main as main_module
    from commands import setup_commands

    guild = StubGuild(11_000)

    class SmokeBot(main_module.ChargeBot):
        def __init__(self) -> None:
            super().__init__()
            self.owner_alerts: list[str] = []

        def get_guild(self, guild_id: int):  # type: ignore[override]
            return guild if guild_id == guild.id else None

        def get_channel(self, channel_id: int):  # type: ignore[override]
            return guild.get_channel(channel_id)

        def get_user(self, user_id: int):  # type: ignore[override]
            return guild.get_member(user_id)

        async def fetch_user(self, user_id: int):  # type: ignore[override]
            return guild.get_member(user_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild]

        @property
        def latency(self) -> float:  # type: ignore[override]
            return 0.05

        async def alert_owner(self, message: str) -> None:
            self.owner_alerts.append(message)

        async def is_server_admin(self, interaction) -> bool:  # type: ignore[override]
            return True

    bot = SmokeBot()

    async def fake_resolve_channel(guild_id, channel_id, setting_name):
        return guild.get_channel(channel_id) if guild_id == guild.id else None

    async def fake_resolve_message_channel(guild_id, channel_id):
        return guild.get_channel(channel_id) if guild_id == guild.id else None

    # スタブのチャンネルは discord.TextChannel ではないため解決処理を差し替える
    bot.charge._resolve_channel = fake_resolve_channel  # type: ignore[assignment]
    bot.charge._resolve_message_channel = fake_resolve_message_channel  # type: ignore[assignment]
    await bot.db.connect()
    await setup_commands(bot)

    # --- テストデータの用意 ---
    G = guild.id
    owner = guild.members.setdefault(
        main_module.BOT_OWNER_ID, StubMember(main_module.BOT_OWNER_ID, guild)
    )
    target = StubMember(12_001, guild)
    rich = StubMember(12_002, guild)
    guild.members[target.id] = target
    guild.members[rich.id] = rich
    role = StubRole(13_001, "VIP", position=10)
    guild.roles[role.id] = role
    role.members = [target, rich]

    await bot.db.set_guild_permission(G, "ALLOWED", owner.id)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=50_000,
        daily_limit=100_000, admin_role_id=role.id,
        achievement_channel_id=guild.channel.id, log_channel_id=guild.channel.id,
        balance_log_channel_id=guild.channel.id, summary_channel_id=guild.channel.id,
        summary_enabled=1,
    )
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=rich.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=50_000, operator_id=owner.id, reason="スモークテスト用",
    )
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=target.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=20_000, operator_id=owner.id, reason="スモークテスト用",
    )
    tx_id = await bot.db.create_transaction(
        guild_id=G, user_id=target.id, requested_amount=1000,
        charge_rate=Decimal("130"), expires_at=utils.now_ts() + 900,
    )
    await bot.db.transition_status(tx_id, config.TxStatus.VALIDATING,
                                  expected=(config.TxStatus.WAITING_LINK,))
    await bot.db.attach_link(tx_id, link_hash_value="smokehash", link_uuid="SMOKE-UUID",
                             received_amount=1000)
    await bot.db.transition_status(tx_id, config.TxStatus.PROCESSING,
                                  expected=(config.TxStatus.QUEUED,))
    await bot.db.transition_status(tx_id, config.TxStatus.RECEIVED,
                                   expected=(config.TxStatus.PROCESSING,))
    await bot.db.credit_transaction(tx_id, 1300)
    item_id = await bot.db.add_shop_item(
        guild_id=G, role_id=role.id, name="VIPロール", price=1000, duration_days=30,
        stock=5, purchase_limit=3, created_by=owner.id,
    )
    purchase = await bot.db.purchase_shop_item(guild_id=G, user_id=rich.id, item_id=item_id)
    await bot.db.activate_purchase(int(purchase["purchase_id"]))
    campaign_id = await bot.db.create_campaign(
        guild_id=G, name="スモークキャンペーン", inviter_reward=500, invited_reward=300,
        min_account_age_days=7, daily_limit=5, total_limit=50, require_charge=True,
        require_days=0, require_review=False, starts_at=None, ends_at=None,
        created_by=owner.id,
    )
    record_id, _ = await bot.db.record_invite(
        guild_id=G, campaign_id=campaign_id, inviter_id=rich.id, invited_id=target.id,
        code="smoke", status=config.InviteStatus.HOLD,
        reason=config.InviteRejectReason.SUSPICIOUS_BURST,
    )
    panel_message_id = 700_001
    guild.channel.messages[panel_message_id] = StubMessage(guild.channel, None)
    await bot.db.add_panel(G, guild.channel.id, panel_message_id, config.PANEL_TYPE_CHARGE)
    await bot.db.add_ranking_panel(G, guild.channel.id, 700_002, config.RankingType.BALANCE)
    history_rows, _ = await bot.db.list_balance_history_filtered(G, user_id=target.id, limit=1)
    history_id = int(history_rows[0]["id"])
    await bot.db.set_role_rate(G, role.id, Decimal("150"), 10)
    await bot.db.add_invite_blacklist(G, 12_099, "テスト", owner.id)

    context: dict[str, object] = {
        "guild_id": G,
        "tx_id": tx_id,
        "history_id": history_id,
        "purchase_id": int(purchase["purchase_id"]),
        "item_id": item_id,
        "record_id": record_id,
        "panel_message_id": panel_message_id,
        "target_member": target,
        "rich_member": rich,
        "role": role,
        "channel": guild.channel,
        "attachment": StubAttachment({
            "version": config.BOT_VERSION,
            "guild_id": G,
            "settings": {"charge_rate": "135", "minimum_charge": 200, "ranking_limit": 12},
            "role_rates": [],
            "shop_items": [],
        }),
    }

    all_commands = list(iter_commands(bot.tree))
    normal = [c for c in all_commands
              if qualified(c) not in DESTRUCTIVE and qualified(c) not in SKIPPED]
    destructive = [c for c in all_commands if qualified(c) in DESTRUCTIVE]
    print(f"対象コマンド: {len(all_commands)} "
          f"(通常 {len(normal)} / 危険 {len(destructive)} / スキップ {len(SKIPPED)})\n")

    async def run_command(command: app_commands.Command) -> None:
        name = qualified(command)
        interaction = StubInteraction(bot, guild, owner)
        try:
            kwargs = build_arguments(command, context)
            callback = command.callback
            if command.binding is not None:
                await callback(command.binding, interaction, **kwargs)
            else:
                await callback(interaction, **kwargs)
        except Exception:
            FAILURES.append((name, traceback.format_exc(limit=6)))
            print(f" FAIL  /{name}")
            return
        if not interaction.calls:
            FAILURES.append((name, "応答が行われていません (defer/send_message いずれも無し)"))
            print(f" FAIL  /{name} (応答なし)")
            return
        OK.append(name)
        detail = ", ".join(interaction.titles[:1]) or interaction.calls[-1]
        print(f"  ok   /{name:28} → {utils.truncate(detail, 46)}")

    print("--- 通常コマンド ---")
    for command in normal:
        await run_command(command)

    print("\n--- 危険な操作 (確認ボタンは自動承認) ---")
    for command in destructive:
        await run_command(command)

    print("\n--- スキップ ---")
    for name, reason in SKIPPED.items():
        print(f"  --   /{name} ({reason})")

    # ------------------------------------------------------------------
    # パネルのボタン・Modal 操作
    # ------------------------------------------------------------------
    print("\n--- ボタン / Modal 操作 ---")
    # 破壊的コマンドで停止させた状態を戻す
    await bot.db.set_guild_permission(G, "ALLOWED", owner.id)
    await bot.db.update_settings(
        G, maintenance=0, emergency_stop=0, shop_enabled=1, ranking_enabled=1,
        achievement_channel_id=guild.channel.id, log_channel_id=guild.channel.id,
        admin_role_id=role.id,
    )
    if await bot.db.get_active_campaign(G) is None:
        await bot.db.create_campaign(
            guild_id=G, name="ボタンテスト", inviter_reward=500, invited_reward=300,
            min_account_age_days=7, daily_limit=5, total_limit=50, require_charge=True,
            require_days=0, require_review=False, starts_at=None, ends_at=None,
            created_by=owner.id,
        )
    if not await bot.db.list_shop_items(G):
        await bot.db.add_shop_item(
            guild_id=G, role_id=role.id, name="VIPロール", price=1000, duration_days=30,
            stock=5, purchase_limit=3, created_by=owner.id,
        )
    # /data delete で残高も消えているため、UI テスト用に入れ直す
    for member_obj, amount in ((rich, 50_000), (target, 20_000)):
        if await bot.db.get_balance(G, member_obj.id) < 10_000:
            await bot.charge.admin_adjust_balance(
                guild_id=G, user_id=member_obj.id,
                change_type=config.BalanceChangeType.ADMIN_SET, amount=amount,
                operator_id=owner.id, reason="UIテスト用",
            )
    # Kyash を利用可能な状態に見せる (チャージボタンの事前チェック通過用)
    bot.kyash._client = object()  # type: ignore[assignment]
    bot.kyash._status = config.KyashAccountStatus.ACTIVE

    async def run_ui(label: str, coro, interaction: StubInteraction | None = None) -> None:
        try:
            await coro
        except Exception:
            FAILURES.append((label, traceback.format_exc(limit=6)))
            print(f" FAIL  {label}")
            return
        OK.append(label)
        detail = ""
        if interaction is not None:
            detail = (interaction.titles[0] if interaction.titles
                      else (interaction.calls[-1] if interaction.calls else "応答なし"))
        print(f"  ok   {label:34} → {utils.truncate(detail, 44)}")

    def fresh(user: StubMember | None = None) -> StubInteraction:
        return StubInteraction(bot, guild, user or owner)

    # --- Persistent View のボタン ---
    view_cases: list[tuple[str, discord.ui.View, StubMember]] = [
        ("チャージパネル", ui.ChargePanelView(), target),
        ("ランキングパネル", ui.RankingPanelView(), target),
        ("ショップパネル", ui.ShopPanelView(), rich),
        ("招待パネル", ui.InvitePanelView(), rich),
        ("管理パネル", ui.AdminPanelView(), owner),
    ]
    for view_label, view, actor in view_cases:
        for item in view.children:
            if not isinstance(item, discord.ui.Button):
                continue
            label = f"{view_label}: {item.label}"
            interaction = fresh(actor)
            await run_ui(label, item.callback(interaction), interaction)

    # 管理パネルの「メンテ切替」でメンテナンスが入るため元に戻す
    await bot.db.update_settings(G, maintenance=0, emergency_stop=0)

    # --- Modal (金額入力 → リンク入力) ---
    settings = await bot.db.get_settings(G)
    amount_modal = ui.AmountModal(settings)
    amount_modal.amount._value = "1000"  # type: ignore[attr-defined]
    amount_interaction = fresh(target)
    await run_ui("Modal: 金額入力", amount_modal.on_submit(amount_interaction),
                 amount_interaction)

    active_tx = await bot.db.get_active_transaction(G, target.id)
    if active_tx is None:
        FAILURES.append(("Modal: 金額入力", "Transaction が作成されませんでした: "
                         + ", ".join(amount_interaction.titles)))
        print(" FAIL  Modal: 金額入力 (Transaction が作成されていない)")
    if active_tx is not None:
        if active_tx["status"] != config.TxStatus.WAITING_LINK:
            FAILURES.append(("Modal: 金額入力",
                             f"状態が WAITING_LINK ではない: {active_tx['status']}"))
        else:
            OK.append("Modal: 金額入力で WAITING_LINK の取引が作られる")
            print("  ok   検証: 金額入力で WAITING_LINK の取引が作成された "
                  f"({active_tx['id']})")
        link_modal = ui.LinkModal(str(active_tx["id"]))
        link_modal.link._value = "https://kyash.me/payments/SMOKELINK001"  # type: ignore[attr-defined]
        link_interaction = fresh(target)
        await run_ui("Modal: 送金リンク入力", link_modal.on_submit(link_interaction),
                     link_interaction)
        # このテストでは外部通信を禁止しているため検証は失敗する。
        # その場合に取引が失われず再入力できる状態のままであることを確認する
        # (正常系のリンク検証は tests/test_charge_flow.py で検証)
        after = await bot.db.get_transaction(str(active_tx["id"]))
        if after is not None and after["status"] in (
            config.TxStatus.WAITING_LINK, config.TxStatus.QUEUED
        ):
            OK.append("通信失敗時も取引が維持され再入力できる")
            print(f"  ok   検証: 通信失敗でも取引を維持 (status={after['status']})")
        else:
            FAILURES.append((
                "Modal: 送金リンク入力",
                f"通信失敗で取引が失われた (status={after['status'] if after else 'なし'})",
            ))
            print(" FAIL  検証: 通信失敗で取引が失われた")
        # リンク送信 View のボタン
        submit_view = ui.LinkSubmitView(str(active_tx["id"]), owner_id=target.id, timeout=60)
        for item in submit_view.children:
            if isinstance(item, discord.ui.Button):
                interaction = fresh(target)
                await run_ui(f"リンク送信View: {item.label}",
                             item.callback(interaction), interaction)
    else:
        print("  --   Modal: 送金リンク入力 (アクティブな取引が無いためスキップ)")

    # --- Kyash ログイン Modal ---
    login_modal = ui.KyashLoginModal()
    login_modal.email._value = "test@example.com"  # type: ignore[attr-defined]
    login_modal.password._value = "dummy-password"  # type: ignore[attr-defined]
    login_interaction = fresh(owner)
    await run_ui("Modal: Kyashログイン", login_modal.on_submit(login_interaction),
                 login_interaction)
    otp_modal = ui.KyashOtpModal()
    otp_modal.otp._value = "123456"  # type: ignore[attr-defined]
    otp_interaction = fresh(owner)
    await run_ui("Modal: OTP入力", otp_modal.on_submit(otp_interaction), otp_interaction)
    otp_view = ui.KyashLoginStartView(owner_id=owner.id)
    for item in otp_view.children:
        if isinstance(item, discord.ui.Button):
            interaction = fresh(owner)
            await run_ui(f"OTP View: {item.label}", item.callback(interaction), interaction)

    # --- 履歴ページング ---
    history_view = ui.HistoryView(owner_id=target.id, guild_id=G)
    for item in history_view.children:
        if isinstance(item, discord.ui.Button):
            interaction = fresh(target)
            await run_ui(f"履歴View: {item.label}", item.callback(interaction), interaction)

    # --- ショップの商品選択 ---
    items = await bot.db.list_shop_items(G)
    if items:
        select_view = ui.ShopSelectView(items, owner_id=rich.id)
        select = select_view.children[0]
        select._values = [str(int(items[0]["id"]))]  # type: ignore[attr-defined]
        select_interaction = fresh(rich)
        balance_before_purchase = await bot.db.get_balance(G, rich.id)
        purchases_before = (await bot.db.list_purchases(G, limit=1))[1]
        await run_ui("ショップ: 商品選択→購入", select.callback(select_interaction),
                     select_interaction)
        purchases_after = (await bot.db.list_purchases(G, limit=1))[1]
        balance_after_purchase = await bot.db.get_balance(G, rich.id)
        price = int(items[0]["price"])
        if purchases_after == purchases_before + 1 and \
                balance_after_purchase == balance_before_purchase - price:
            OK.append("ショップ購入が成立し残高が引き落とされる")
            print(f"  ok   検証: 購入成立 (残高 {balance_before_purchase} → "
                  f"{balance_after_purchase})")
        else:
            FAILURES.append((
                "ショップ: 商品選択→購入",
                f"購入が成立していない (購入数 {purchases_before}→{purchases_after} / "
                f"残高 {balance_before_purchase}→{balance_after_purchase})",
            ))
            print(" FAIL  検証: ショップ購入が成立していない")

    # --- チャージ方式 (PayPay / LTC の申請と審査) ---
    await bot.db.set_destination(
        config.ChargeProvider.PAYPAY, address="paypay-smoke-001", label="受取用",
        note=None, updated_by=owner.id,
    )
    await bot.db.set_destination(
        config.ChargeProvider.LTC, address="ltc1qsmoketest0000000000000000",
        label="受取用", note=None, updated_by=owner.id,
    )
    await bot.charge.set_review_channel_id(guild.channel.id)
    # 価格 API は使わせない (固定価格でテストする)
    await bot.price.set_source(config.PRICE_SOURCE_MANUAL)
    await bot.price.set_manual_price(Decimal("12000"))

    settings = await bot.db.get_settings(G)
    entries = await bot.charge.provider_availability(G, settings)
    provider_view = ui.ProviderSelectView(entries)
    provider_select = provider_view.children[0]
    for provider in (config.ChargeProvider.PAYPAY, config.ChargeProvider.LTC):
        provider_select._values = [provider]  # type: ignore[attr-defined]
        interaction = fresh(rich)
        await run_ui(f"方式選択: {provider}", provider_select.callback(interaction),
                     interaction)

    for provider, proof, asset in (
        (config.ChargeProvider.PAYPAY, "SMOKE-PP-0001", None),
        (config.ChargeProvider.LTC, "ef" * 32, "0.09"),
    ):
        limits = await bot.charge.provider_limits(G, provider, settings)
        amount_modal = ui.ManualAmountModal(provider, settings, limits)
        amount_modal.amount._value = "1000"  # type: ignore[attr-defined]
        interaction = fresh(rich)
        await run_ui(f"Modal: {provider} 金額入力",
                     amount_modal.on_submit(interaction), interaction)
        pending, _ = await bot.db.list_requests(
            guild_id=G, user_id=rich.id, provider=provider,
            statuses=[config.RequestStatus.QUOTED], limit=1,
        )
        if not pending:
            FAILURES.append((f"Modal: {provider} 金額入力",
                             "申請が作成されませんでした: " + ", ".join(interaction.titles)))
            print(f" FAIL  検証: {provider} の申請が作成されていない")
            continue
        request_id = int(pending[0]["id"])
        print(f"  ok   検証: {provider} の申請を作成 (#{request_id})")

        deposit_view = ui.DepositView(request_id, provider, owner_id=rich.id, timeout=60)
        for item in deposit_view.children:
            if isinstance(item, discord.ui.Button) and item.label == "送金しました":
                interaction = fresh(rich)
                await run_ui(f"入金View: {provider} 送金しました",
                             item.callback(interaction), interaction)

        proof_modal = ui.RequestProofModal(request_id, provider)
        proof_modal.proof._value = proof  # type: ignore[attr-defined]
        if proof_modal.asset is not None and asset:
            proof_modal.asset._value = asset  # type: ignore[attr-defined]
        interaction = fresh(rich)
        await run_ui(f"Modal: {provider} 申請", proof_modal.on_submit(interaction),
                     interaction)
        row = await bot.db.get_request(request_id)
        if row["status"] == config.RequestStatus.PENDING and row["review_message_id"]:
            OK.append(f"{provider}: 申請が承認待ちになり審査カードが投稿される")
            print(f"  ok   検証: {provider} の申請が承認待ち + 審査カード投稿")
        else:
            FAILURES.append((f"Modal: {provider} 申請",
                             f"状態 {row['status']} / カード {row['review_message_id']}"))
            print(f" FAIL  検証: {provider} の申請が承認待ちになっていない")

    # 審査カードのボタン (承認 / 金額修正 / 却下 / 詳細)
    pending_rows = await bot.db.list_pending_requests(limit=5)
    review_view = ui.ReviewCardView()
    if pending_rows:
        approve_target = int(pending_rows[0]["id"])
        reject_target = int(pending_rows[-1]["id"])
        message_id = int(pending_rows[0]["review_message_id"] or 0)
        for item in review_view.children:
            if not isinstance(item, discord.ui.Button):
                continue
            interaction = fresh(owner)
            interaction.message = SimpleNamespace(id=message_id)
            await run_ui(f"審査カード: {item.label}", item.callback(interaction),
                         interaction)
        row = await bot.db.get_request(approve_target)
        if row["status"] == config.RequestStatus.APPROVED and row["transaction_id"]:
            OK.append("審査カードの承認で残高が付与される")
            print(f"  ok   検証: 承認で残高付与 (付与 {row['credited_amount']} / "
                  f"取引 {row['transaction_id']})")
        else:
            FAILURES.append(("審査カード: 承認", f"状態 {row['status']}"))
            print(f" FAIL  検証: 承認が反映されていない ({row['status']})")

        # 金額修正 Modal / 却下 Modal
        if reject_target != approve_target:
            edit_modal = ui.ApproveAmountModal(reject_target, 1200)
            edit_modal.amount._value = "900"  # type: ignore[attr-defined]
            edit_modal.note._value = "入金が 900 円だったため"  # type: ignore[attr-defined]
            interaction = fresh(owner)
            await run_ui("Modal: 金額修正して承認",
                         edit_modal.on_submit(interaction), interaction)
            edited = await bot.db.get_request(reject_target)
            if edited["status"] == config.RequestStatus.APPROVED \
                    and int(edited["credited_amount"] or 0) == 900:
                OK.append("金額を直して承認できる")
                print("  ok   検証: 金額を直して承認 (900 付与)")
            else:
                FAILURES.append(("Modal: 金額修正して承認",
                                 f"状態 {edited['status']} / 付与 {edited['credited_amount']}"))
                print(" FAIL  検証: 金額修正して承認が反映されていない")

        # 却下は新しい申請で確認する
        fresh_quote = await bot.charge.start_manual_charge(
            G, rich.id, config.ChargeProvider.PAYPAY, "1000")
        await bot.charge.submit_request(
            int(fresh_quote["request_id"]), rich.id, "SMOKE-PP-REJECT")
        reject_modal = ui.RejectReasonModal(int(fresh_quote["request_id"]))
        reject_modal.reason._value = "入金を確認できませんでした"  # type: ignore[attr-defined]
        interaction = fresh(owner)
        await run_ui("Modal: 却下理由", reject_modal.on_submit(interaction), interaction)
        rejected = await bot.db.get_request(int(fresh_quote["request_id"]))
        if rejected["status"] == config.RequestStatus.REJECTED:
            OK.append("却下で残高が動かない")
            print("  ok   検証: 却下が反映された")
        else:
            FAILURES.append(("Modal: 却下理由", f"状態 {rejected['status']}"))
            print(f" FAIL  検証: 却下が反映されていない ({rejected['status']})")
    else:
        print("  --   審査カード (承認待ちの申請が無いためスキップ)")

    # --- 確認ビュー (2段階) ---
    confirm_view = ui.ConfirmView(owner_id=owner.id, confirm_label="実行", stages=2)
    for item in confirm_view.children:
        if isinstance(item, discord.ui.Button):
            interaction = fresh(owner)
            await run_ui(f"確認View: {item.label}", item.callback(interaction), interaction)

    # --- 請求リンク (Kyash) の UI ---
    settings = await bot.db.get_settings(G)
    claim_limits = await bot.charge.provider_limits(
        G, config.ChargeProvider.KYASH_CLAIM, settings
    )
    claim_modal = ui.ManualAmountModal(
        config.ChargeProvider.KYASH_CLAIM, settings, claim_limits
    )
    claim_modal.amount._value = "1000"  # type: ignore[attr-defined]
    interaction = fresh(target)
    await run_ui("Modal: 請求リンクの金額入力", claim_modal.on_submit(interaction), interaction)
    # 外部通信は禁止しているため発行は失敗するが、取引が残らないことを確認する
    leftover = await bot.db.count_active_transactions(G, target.id)
    if leftover == 0:
        OK.append("請求リンクの発行失敗で取引が残らない")
        print(f"  ok   検証: 発行失敗で取引を残さない (進行中 {leftover}件)")
    else:
        FAILURES.append(("Modal: 請求リンクの金額入力",
                         f"失敗したのに取引が残っている ({leftover}件)"))
        print(f" FAIL  検証: 発行失敗で取引が残った ({leftover}件)")
    claim_view = ui.ClaimPaymentView("TX-NOTEXIST", owner_id=target.id, timeout=60)
    for item in claim_view.children:
        if isinstance(item, discord.ui.Button):
            interaction = fresh(target)
            await run_ui(f"請求リンクView: {item.label}",
                         item.callback(interaction), interaction)

    # --- コマンドの二重登録 (グローバル + ギルド) が起きないこと ---
    print("\n--- コマンド登録スコープ ---")
    tree_calls: list[str] = []
    fetched: dict[int, list[object]] = {guild.id: [object(), object(), object()]}

    class TreeSpy:
        """tree.sync / copy_global_to の呼び出しを記録する。

        グローバルとギルドの両方へ同じコマンドを登録すると Discord は
        それぞれを別枠で表示するため、全コマンドが二重に見えてしまう。
        ここではギルドへの「登録」が起きないこと、ギルドへの sync が
        「削除 (空の同期)」としてのみ使われることを確かめる。
        """

        def __init__(self) -> None:
            self.cleared: list[int] = []

        async def sync(self, *, guild=None):
            if guild is None:
                tree_calls.append("sync:global")
                return [object()] * 5
            gid = int(getattr(guild, "id", guild))
            tree_calls.append(f"sync:guild:{gid}")
            fetched[gid] = []
            return []

        def copy_global_to(self, *, guild) -> None:
            tree_calls.append("copy_global_to")

        def clear_commands(self, *, guild=None) -> None:
            gid = int(getattr(guild, "id", guild)) if guild is not None else 0
            tree_calls.append(f"clear:{gid}")
            self.cleared.append(gid)

        async def fetch_commands(self, *, guild=None):
            if guild is None:
                return [object()] * 5
            return fetched.get(int(getattr(guild, "id", guild)), [])

    from commands import ServerGroup

    server_group = ServerGroup()
    spy = TreeSpy()
    # CommandTree は読み取り専用プロパティなので内部属性を差し替える
    bot._connection = getattr(bot, "_connection", None)  # 触らないことを明示
    object.__setattr__(bot, "_TestTree__spy", spy)
    type(bot).tree = property(lambda self: spy)  # type: ignore[assignment]

    interaction = fresh(owner)
    await run_ui("/server allow (再同期しない)",
                 server_group.allow.callback(server_group, interaction, str(guild.id), None),
                 interaction)
    if "copy_global_to" in tree_calls:
        FAILURES.append(("/server allow", "ギルドへコマンドをコピーしている (二重登録の原因)"))
        print(" FAIL  検証: /server allow がギルドへコマンドをコピーしている")
    else:
        OK.append("/server allow はギルドへコマンドをコピーしない")
        print("  ok   検証: /server allow はギルドへコピーしない")
    if guild.id in spy.cleared:
        OK.append("/server allow が残骸のギルドコマンドを削除する")
        print(f"  ok   検証: 残骸のギルドコマンドを削除 (呼び出し {tree_calls})")
    else:
        FAILURES.append(("/server allow", "残骸のギルドコマンドを削除していない"))
        print(" FAIL  検証: 残骸のギルドコマンドを削除していない")

    tree_calls.clear()
    fetched[guild.id] = [object(), object()]
    await bot.db.set_system_value(bot.GUILD_COMMAND_CLEANUP_KEY, "")
    interaction = fresh(owner)
    await run_ui("/server sync (グローバルのみ)",
                 server_group.sync.callback(server_group, interaction, False), interaction)
    guild_syncs = [c for c in tree_calls if c.startswith("sync:guild")]
    if "copy_global_to" in tree_calls or guild_syncs:
        FAILURES.append(("/server sync", f"ギルドへも同期している: {tree_calls}"))
        print(f" FAIL  検証: /server sync がギルドへ同期している ({tree_calls})")
    else:
        OK.append("/server sync はグローバルのみ同期する")
        print(f"  ok   検証: グローバルのみ同期 ({tree_calls})")

    tree_calls.clear()
    interaction = fresh(owner)
    await run_ui("/server sync cleanup:True",
                 server_group.sync.callback(server_group, interaction, True), interaction)
    if guild.id in spy.cleared and "copy_global_to" not in tree_calls:
        OK.append("/server sync cleanup で重複を削除できる")
        print(f"  ok   検証: cleanup で重複を削除 ({tree_calls})")
    else:
        FAILURES.append(("/server sync cleanup", f"重複を削除していない: {tree_calls}"))
        print(f" FAIL  検証: cleanup が重複を削除していない ({tree_calls})")

    print("\n--- 権限チェック (一般利用者は拒否されるか) ---")
    class PlainBot(SmokeBot):
        async def is_server_admin(self, interaction) -> bool:  # type: ignore[override]
            return False

        def is_bot_owner(self, user) -> bool:  # type: ignore[override]
            return False

    plain_bot = PlainBot.__new__(PlainBot)
    for attribute in ("db", "charge", "kyash", "tasks", "cipher", "version", "started_at"):
        setattr(plain_bot, attribute, getattr(bot, attribute))
    plain_bot.owner_alerts = []
    denied = 0
    checked = 0
    plain_user = StubMember(12_500, guild)
    guild.members[plain_user.id] = plain_user
    for command in all_commands:
        if not command.checks:
            continue
        checked += 1
        interaction = StubInteraction(plain_bot, guild, plain_user)
        for predicate in command.checks:
            try:
                result = predicate(interaction)
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    denied += 1
                    break
            except app_commands.CheckFailure:
                denied += 1
                break
            except Exception as exc:  # noqa: BLE001
                FAILURES.append((f"{qualified(command)} (権限チェック)",
                                 f"{type(exc).__name__}: {exc}"))
                break
    print(f"  ok   権限チェックのあるコマンド {checked} 件のうち {denied} 件が一般利用者を拒否")
    if denied != checked:
        FAILURES.append(("権限チェック", f"{checked - denied} 件が一般利用者を拒否しなかった"))

    await bot.charge.shutdown()
    await bot.kyash.shutdown()
    await bot.db.close()

    print("\n" + "=" * 70)
    print(f"結果: {len(OK)} 件成功 / {len(FAILURES)} 件失敗")
    for name, detail in FAILURES:
        print(f"\n✗ /{name}\n{detail}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if FAILURES else 0)
