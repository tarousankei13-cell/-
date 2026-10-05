"""利用者レコードの補助"""

from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from db.session import session_scope


# 匿名コードの長さ（16進の桁数）。
#
#   ⚠️ **短くしてはいけない。** かつて4桁だったが、取りうる値が
#      65,536通りしかなく、誕生日問題で
#        100人 … 7%、300人 … 50%、1000人 … ほぼ確実
#      の割合で衝突していた。
#      衝突すると anon_code の一意制約で **利用者の行そのものが
#      作れなくなり**、その人はチャージも注文も一切できなくなる。
#      原因も表に出ないので、まず気づけない。
#
#      6桁なら 16,777,216通りで、5,000人規模でも衝突はまれ。
#      それでも起きうるので、下の _free_code() で必ず逃がす。
CODE_LENGTH = 6

# 衝突したときに試す桁数。順に長くしていく。
CODE_FALLBACKS = (8, 10, 12)


def anon_code(discord_id: int, *, length: int = CODE_LENGTH) -> str:
    """
    実績パネル用の匿名コード。

    Discord IDから導出するので、同じ人はいつも同じコードになる。
    逆算はできない。
    """
    h = hashlib.sha256(f"mcdbot:{discord_id}".encode()).hexdigest()
    return f"U-{h[:length].upper()}"


async def _free_code(session: AsyncSession, discord_id: int) -> str:
    """
    その人に使える匿名コード。他の人と重ならないものを返す。

    ⚠️ 同じ人には**いつも同じ**コードを返す（桁を伸ばすだけで、
       元のハッシュは変えない）。実績パネルで「同じ人」と分かる
       ことが匿名コードの存在理由なので、ここが揺れてはいけない。
    """
    from sqlalchemy import select

    for length in (CODE_LENGTH, *CODE_FALLBACKS):
        code = anon_code(discord_id, length=length)
        taken = (await session.execute(
            select(User.discord_id).where(User.anon_code == code)
        )).scalar()
        if taken is None or taken == discord_id:
            return code
    # 12桁まで全部ぶつかることは現実には起こらないが、
    # それでも行を作れないよりはよい。
    return f"U-{discord_id % 10 ** 10:010d}"


async def ensure_user(session: AsyncSession, discord_id: int) -> User:
    user = await session.get(User, discord_id)
    if user is None:
        user = User(
            discord_id=discord_id,
            anon_code=await _free_code(session, discord_id),
        )
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
