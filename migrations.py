async def m001_initial(db):
    """
    Initial cashu table.
    """
    await db.execute(
        """
        CREATE TABLE cashu.cashu (
            id TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            name TEXT NOT NULL,
            tickershort TEXT DEFAULT 'sats',
            fraction BOOL,
            maxsats INT,
            coins INT,
            keyset_id TEXT NOT NULL,
            issued_sat INT
        );
    """
    )

    """
    Initial cashus table.
    """
    await db.execute(
        """
        CREATE TABLE cashu.pegs (
            id TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            inout BOOL NOT NULL,
            amount INT
        );
    """
    )


async def m002_add_mint_settings(db):
    """
    Add mint options.
    """
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_max_balance INT DEFAULT 0;"
    )
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_max_peg_in INT DEFAULT 0;"
    )
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_max_peg_out INT DEFAULT 0;"
    )
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_peg_out_only BOOL DEFAULT false;"
    )
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_description TEXT DEFAULT '';"
    )
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN mint_description_long TEXT DEFAULT '';"
    )

    # drop cashu.pegs
    await db.execute("DROP TABLE cashu.pegs;")
    # clean up unused columns in cashu.cashu
    await db.execute("ALTER TABLE cashu.cashu DROP COLUMN issued_sat;")
    await db.execute("ALTER TABLE cashu.cashu DROP COLUMN coins;")
    await db.execute("ALTER TABLE cashu.cashu DROP COLUMN tickershort;")
    await db.execute("ALTER TABLE cashu.cashu DROP COLUMN fraction;")
    await db.execute("ALTER TABLE cashu.cashu DROP COLUMN maxsats;")
