# startup routine of the standalone app. These are the steps that need
# to be taken by external apps importing the cashu mint.

import importlib

from loguru import logger

from cashu.core.db import Database
from cashu.core.migrations import migrate_databases
from cashu.core.settings import settings
from cashu.lightning.fake import FakeWallet  # type: ignore
from cashu.lightning.lnbits import LNbitsWallet  # type: ignore
from cashu.mint import migrations
from cashu.mint.ledger import Ledger

logger.debug("Enviroment Settings:")
for key, value in settings.dict().items():
    logger.debug(f"{key}: {value}")

wallets_module = importlib.import_module("cashu.lightning")
lightning_backend = getattr(wallets_module, settings.mint_lightning_backend)()

ledger = Ledger(
    db=Database("mint", settings.mint_database),
    seed=settings.mint_private_key,
    derivation_path="0/0/0/1",
    lightning=lightning_backend,
)


async def start_mint_init():

    await migrate_databases(ledger.db, migrations)
    await ledger.load_used_proofs()
    await ledger.init_keysets()

    if settings.lightning:
        logger.info(f"Using backend: {settings.mint_lightning_backend}")
        error_message, balance = await ledger.lightning.status()
        if error_message:
            logger.warning(
                f"The backend for {ledger.lightning.__class__.__name__} isn't working properly: '{error_message}'",
                RuntimeWarning,
            )
        logger.info(f"Lightning balance: {balance} msat")

    logger.info(f"Data dir: {settings.cashu_dir}")
    logger.info("Mint started.")
