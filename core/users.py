"""利用者レコードの補助"""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from db.session import session_scope


def anon_code(discord_id: int) -> str:
    """
    実績パネル用の匿名コード。

    Discord IDから導出するので、同じ人はいつも同じコードになる。
    逆算はできない。
    """
    h = hashlib.sha256(f"mcdbot:{discord_id}".encode()).hexdigest()
    return f"U-{h[:4].upper()}"


async def ensure_user(session: AsyncSession, discord_id: int) -> User:
    user = await session.get(User, discord_id)
    if user is None:
        user = User(discord_id=discord_id, anon_code=anon_code(discord_id))
        session.add(user)
        await session.flush()
    return user


async def get_or_create(discord_id: int) -> User:
    async with session_scope() as s:
        return await ensure_user(s, discord_id)


async def is_banned(discord_id: int) -> bool:
    async with session_scope() as s:
        user = await s.get(User, discord_id)
        return bool(user and user.is_banned)
