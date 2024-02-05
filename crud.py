from typing import List, Optional, Union

from . import db
from .models import Cashu


async def create_cashu(
    cashu_id: str, keyset_id: str, wallet_id: str, data: Cashu
) -> Cashu:
    await db.execute(
        """
        INSERT INTO cashu.cashu (id, wallet, name, keyset_id, mint_peg_out_only, mint_description)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            cashu_id,
            wallet_id,
            data.name,
            keyset_id,
            data.mint_peg_out_only,
            data.mint_description,
        ),
    )

    cashu = await get_cashu(cashu_id)
    assert cashu, "Newly created cashu couldn't be retrieved"
    return cashu


async def get_cashu(cashu_id) -> Optional[Cashu]:
    row = await db.fetchone("SELECT * FROM cashu.cashu WHERE id = ?", (cashu_id,))
    return Cashu(**row) if row else None


async def update_cashu(cashu_id, data: Cashu) -> None:
    await db.execute(
        """
        UPDATE cashu.cashu
        SET name = ?, mint_description = ?, mint_description_long = ?, mint_max_balance = ?, mint_max_peg_in = ?, mint_max_peg_out = ?, mint_peg_out_only = ?
        WHERE id = ?
        """,
        (
            data.name,
            data.mint_description or "",
            data.mint_description_long or "",
            data.mint_max_balance or 0,
            data.mint_max_peg_in or 0,
            data.mint_max_peg_out or 0,
            data.mint_peg_out_only or False,
            cashu_id,
        ),
    )
    return await get_cashu(cashu_id)


async def get_cashus(wallet_ids: Union[str, List[str]]) -> List[Cashu]:
    if isinstance(wallet_ids, str):
        wallet_ids = [wallet_ids]

    q = ",".join(["?"] * len(wallet_ids))
    rows = await db.fetchall(
        f"SELECT * FROM cashu.cashu WHERE wallet IN ({q})", (*wallet_ids,)
    )

    return [Cashu(**row) for row in rows]


async def delete_cashu(cashu_id) -> None:
    await db.execute("DELETE FROM cashu.cashu WHERE id = ?", (cashu_id,))
