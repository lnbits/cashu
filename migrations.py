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
    await db.execute("ALTER TABLE cashu.cashu ADD COLUMN max_mint INT;")
    await db.execute(
        "ALTER TABLE cashu.cashu ADD COLUMN peg_out_only BOOL DEFAULT false;"
    )
    await db.execute("ALTER TABLE cashu.cashu ADD COLUMN mint_information TEXT;")
