from sqlite3 import Row
from typing import List

from fastapi import Query
from pydantic import BaseModel


class Cashu(BaseModel):
    id: str = Query(None)
    name: str = Query(None)
    wallet: str = Query(None)
    keyset_id: str = Query(None)
    mint_max_balance: int = Query(None)
    mint_max_peg_in: int = Query(None)
    mint_max_peg_out: int = Query(None)
    mint_peg_out_only: bool = Query(None)
    mint_name: str = Query(None)
    mint_description: str = Query(None)
    mint_description_long: str = Query(None)

    @classmethod
    def from_row(cls, row: Row):
        return cls(**dict(row))
