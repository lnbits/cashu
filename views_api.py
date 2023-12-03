import asyncio
import math
from http import HTTPStatus
from typing import Dict, List, Union

from fastapi import Depends, Query
from lnbits import bolt11
from lnbits.core.crud import get_standalone_payment, get_installed_extension, get_user
from lnbits.core.services import (
    check_transaction_status,
    create_invoice,
    fee_reserve,
    pay_invoice,
)
from lnbits.decorators import WalletTypeInfo, get_key_type, require_admin_key
from lnbits.helpers import urlsafe_short_hash
from lnbits.wallets.base import PaymentStatus
from loguru import logger
from starlette.exceptions import HTTPException

from . import cashu_ext, ledger
from .crud import create_cashu, delete_cashu, get_cashu, get_cashus
from .lib.cashu.core.helpers import sum_proofs
from .lib.cashu.core.crypto.keys import random_hash

# try to import service_fee from lnbits.core.services but fallback to 0.5% if it doesn't exist
service_fee_present = True
try:
    from lnbits.core.services import service_fee
except ImportError:
    logger.warning("Cashu: could not import service_fee from lnbits.core.services")
    service_fee_present = False


def fee_reserve_internal(amount_msat: int) -> int:
    """
    Calculates the fee reserve in sat for a given amount in msat.
    """
    fee_reserve_sat = math.ceil(fee_reserve(amount_msat) / 1000)
    if service_fee_present:
        return fee_reserve_sat + math.ceil(service_fee(amount_msat) / 1000)
    else:
        # fallback to 0.5% if service_fee is not present
        return fee_reserve_sat + math.ceil(amount_msat * 0.005 / 1000)


# -------- cashu imports
from .lib.cashu.core.base import (
    BlindedSignature,
    CheckFeesRequest,
    CheckFeesResponse,
    CheckSpendableRequest,
    CheckSpendableResponse,
    GetInfoResponse,
    GetMeltResponse,
    GetMintResponse,
    Invoice,
    KeysetsResponse,
    KeysResponse,
    PostMeltRequest,
    PostMintRequest,
    PostMintResponse,
    PostRestoreResponse,
    PostSplitRequest,
    PostSplitResponse,
    PostSplitResponse_Deprecated,
)
from .lib.cashu.core.db import lock_table
from .models import Cashu

# --------- extension imports

# WARNING: Do not set this to False in production! This will create
# tokens for free otherwise. This is for testing purposes only!

LIGHTNING = True

if not LIGHTNING:
    logger.warning(
        "Cashu: LIGHTNING is set False! That means that I will create ecash for free!"
    )

########################################
############### LNBITS MINTS ###########
########################################


@cashu_ext.get("/api/v1/mints", status_code=HTTPStatus.OK)
async def api_cashus(
    all_wallets: bool = Query(False), wallet: WalletTypeInfo = Depends(get_key_type)
):
    """
    Get all mints of this wallet.
    """
    wallet_ids = [wallet.wallet.id]
    if all_wallets:
        user = await get_user(wallet.wallet.user)
        if user:
            wallet_ids = user.wallet_ids

    return [cashu.dict() for cashu in await get_cashus(wallet_ids)]


@cashu_ext.post("/api/v1/mints", status_code=HTTPStatus.CREATED)
async def api_cashu_create(
    data: Cashu,
    wallet: WalletTypeInfo = Depends(get_key_type),
):
    """
    Create a new mint for this wallet.
    """
    cashu_id = urlsafe_short_hash()

    # generate a new keyset in cashu
    keyset_derivation_path = urlsafe_short_hash()
    keyset = await ledger.load_keyset(derivation_path=keyset_derivation_path)
    assert keyset.id

    cashu = await create_cashu(
        cashu_id=cashu_id, keyset_id=keyset.id, wallet_id=wallet.wallet.id, data=data
    )
    logger.debug(f"Cashu mint created: {cashu_id}")
    return cashu.dict()


@cashu_ext.delete("/api/v1/mints/{cashu_id}")
async def api_cashu_delete(
    cashu_id: str, wallet: WalletTypeInfo = Depends(require_admin_key)
):
    """
    Delete an existing cashu mint.
    """
    cashu = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Cashu mint does not exist."
        )

    if cashu.wallet != wallet.wallet.id:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail="Not your Cashu mint."
        )

    await delete_cashu(cashu_id)
    raise HTTPException(status_code=HTTPStatus.NO_CONTENT)


#######################################
########### CASHU ENDPOINTS ###########
#######################################
@cashu_ext.get(
    "/api/v1/{cashu_id}/info",
    name="Mint information",
    summary="Mint information, operator contact information, and other info.",
    response_model=GetInfoResponse,
    response_model_exclude_none=True,
)
async def info(cashu_id: str):
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    extension_info = await get_installed_extension("cashu")
    if extension_info:
        installed_version = extension_info.installed_version
    else:
        installed_version = "unknown"
    return GetInfoResponse(
        name=cashu.name,
        version=f"LNbitsCashu/{installed_version}",
    )


@cashu_ext.get(
    "/api/v1/{cashu_id}/keys",
    name="Mint public keys",
    summary="Get the public keys of the newest mint keyset",
    status_code=HTTPStatus.OK,
    response_model=KeysResponse,
)
async def keys(cashu_id: str):
    """Get the public keys of the mint"""
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    keyset = ledger.get_keyset(keyset_id=cashu.keyset_id)
    return KeysResponse.parse_obj(keyset).__root__


@cashu_ext.get(
    "/api/v1/{cashu_id}/keys/{idBase64Urlsafe}",
    name="Keyset public keys",
    summary="Public keys of a specific keyset",
    status_code=HTTPStatus.OK,
    response_model=KeysResponse,
)
async def keyset_keys(cashu_id: str, idBase64Urlsafe: str):
    """
    Get the public keys of the mint of a specificy keyset id.
    The id is encoded in base64_urlsafe and needs to be converted back to
    normal base64 before it can be processed.
    """

    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    id = idBase64Urlsafe.replace("-", "+").replace("_", "/")
    keyset = ledger.get_keyset(keyset_id=id)
    return KeysResponse.parse_obj(keyset).__root__


@cashu_ext.get(
    "/api/v1/{cashu_id}/keysets",
    status_code=HTTPStatus.OK,
    name="Active keysets",
    summary="Get all active keyset id of the mind",
)
async def keysets(cashu_id: str) -> KeysetsResponse:
    """Get the public keys of the mint"""
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    return KeysetsResponse.parse_obj({"keysets": [cashu.keyset_id]})


@cashu_ext.get(
    "/api/v1/{cashu_id}/mint",
    name="Request mint",
    summary="Request minting of new tokens",
)
async def request_mint(cashu_id: str, amount: int = 0) -> GetMintResponse:
    """
    Request minting of new tokens. The mint responds with a Lightning invoice.
    This endpoint can be used for a Lightning invoice UX flow.

    Call `POST /mint` after paying the invoice.
    """
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    # create an invoice that the wallet needs to pay
    try:
        payment_hash, payment_request = await create_invoice(
            wallet_id=cashu.wallet,
            amount=amount,
            memo=f"{cashu.name}",
            extra={"tag": "cashu"},
        )
        invoice = Invoice(
            amount=amount,
            bolt11=payment_request,
            id=random_hash(),
            payment_hash=payment_hash,
            issued=False,
        )
        # await store_lightning_invoice(cashu_id, invoice)
        await ledger.crud.store_lightning_invoice(invoice=invoice, db=ledger.db)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail=str(e))

    print(f"Lightning invoice: {payment_request}")
    resp = GetMintResponse(pr=invoice.bolt11, hash=invoice.id)
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/mint",
    name="Mint tokens",
    summary="Mint tokens in exchange for a Bitcoin paymemt that the user has made",
)
async def mint(
    data: PostMintRequest,
    cashu_id: str,
    hash: str = Query(None),
    payment_hash: str = Query(None),
) -> PostMintResponse:
    """
    Requests the minting of tokens belonging to a paid payment request.
    Call this endpoint after `GET /mint`.

    Note: This endpoint implements the logic in ledger.mint() and ledger._check_lightning_invoice()
    """
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    await ledger._verify_outputs(data.outputs)

    # BEGIN: backwards compatibility < 0.12 where we used to lookup payments with payment_hash
    # We use the payment_hash to lookup the hash from the database and pass that one along.
    id = payment_hash or hash
    # END: backwards compatibility < 0.12

    keyset = ledger.keysets.keysets[cashu.keyset_id]

    ledger.locks[id] = (
        ledger.locks.get(id) or asyncio.Lock()
    )  # create a new lock if it doesn't exist
    async with ledger.locks[id]:
        invoice = await ledger.crud.get_lightning_invoice(db=ledger.db, id=id)
        if invoice is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="Mint does not know this invoice.",
            )
        if invoice.issued:
            raise HTTPException(
                status_code=HTTPStatus.PAYMENT_REQUIRED,
                detail="Tokens already issued for this invoice.",
            )
        # set this invoice as issued
        await ledger.crud.update_lightning_invoice(db=ledger.db, id=id, issued=True)
    del ledger.locks[id]

    try:
        total_requested = sum([bm.amount for bm in data.outputs])
        if total_requested > invoice.amount:
            raise HTTPException(
                status_code=HTTPStatus.PAYMENT_REQUIRED,
                detail=f"Requested amount too high: {total_requested}. Invoice amount: {invoice.amount}",
            )
        status: PaymentStatus = await check_transaction_status(
            cashu.wallet, invoice.payment_hash
        )
        if not status.paid:
            raise HTTPException(
                status_code=HTTPStatus.PAYMENT_REQUIRED, detail="Invoice not paid."
            )
        promises = await ledger._generate_promises(B_s=data.outputs, keyset=keyset)
    except (Exception, HTTPException) as e:
        logger.debug(f"Cashu: /mint {str(e) or getattr(e, 'detail')}")
        # unset issued flag because something went wrong
        await ledger.crud.update_lightning_invoice(db=ledger.db, id=id, issued=False)
        raise HTTPException(
            status_code=getattr(e, "status_code") or HTTPStatus.INTERNAL_SERVER_ERROR,
            detail=str(e) or getattr(e, "detail"),
        )

    return PostMintResponse(promises=promises)


@cashu_ext.post(
    "/api/v1/{cashu_id}/melt",
    name="Melt tokens",
    summary="Melt tokens for a Bitcoin payment that the mint will make for the user in exchange",
)
async def melt(payload: PostMeltRequest, cashu_id: str) -> GetMeltResponse:
    """Invalidates proofs and pays a Lightning invoice."""
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    proofs = payload.proofs
    invoice = payload.pr
    outputs = payload.outputs

    # !!!!!!! MAKE SURE THAT PROOFS ARE ONLY FROM THIS CASHU KEYSET ID
    # THIS IS NECESSARY BECAUSE THE CASHU BACKEND WILL ACCEPT ANY VALID
    # TOKENS
    accepted_keysets = [cashu.keyset_id]
    if ledger.master_key:
        # NOTE: bug fix, fee return tokens for v 0.3.1 are from the master keyset
        # we need to accept them but only if a master keyset is set, otherwise it's unsafe
        # to accept them.
        accepted_keysets += [ledger.keyset.id]
    assert all([p.id in accepted_keysets for p in proofs]), HTTPException(
        status_code=HTTPStatus.METHOD_NOT_ALLOWED,
        detail="Error: Tokens are from another mint.",
    )

    # set proofs as pending
    await ledger._set_proofs_pending(proofs)
    try:
        total_provided = sum_proofs(proofs)
        invoice_obj = bolt11.decode(invoice)
        assert invoice_obj.amount_msat, "invoice has no amount."
        invoice_amount = math.ceil(invoice_obj.amount_msat / 1000)

        reserve_fees_sat = fee_reserve_internal(invoice_obj.amount_msat)

        # verify overspending attempt
        assert total_provided >= invoice_amount + reserve_fees_sat, Exception(
            "provided proofs not enough for Lightning payment. Provided:"
            f" {total_provided}, needed: {invoice_amount + reserve_fees_sat}"
        )
        # verify outputs
        await ledger._verify_outputs(outputs)

        # verify spending inputs and their spending conditions
        await ledger.verify_inputs_and_outputs(proofs)

        logger.debug(f"Cashu: Initiating payment of {total_provided} sats")
        await pay_invoice(
            wallet_id=cashu.wallet,
            payment_request=invoice,
            description="Pay Cashu invoice",
            extra={"tag": "cashu", "cashu_name": cashu.name},
        )
        logger.debug(
            f"Cashu: Wallet {cashu.wallet} checking PaymentStatus of {invoice_obj.payment_hash}"
        )
        status: PaymentStatus = await check_transaction_status(
            cashu.wallet, invoice_obj.payment_hash
        )

        if not status.paid:
            raise Exception(f"Cashu: Payment failed for {invoice_obj.payment_hash}")

        logger.debug(
            f"Cashu: Payment successful, invalidating proofs for {invoice_obj.payment_hash}"
        )
        await ledger._invalidate_proofs(proofs)

        # get the actual paid fees from the db entry
        payment = await get_standalone_payment(invoice_obj.payment_hash)
        assert payment, Exception("Payment not found.")
        paid_fee_msat = payment.fee

        # prepare change to compensate wallet for overpaid fees
        return_promises: List[BlindedSignature] = []
        if outputs and paid_fee_msat is not None:
            keyset = ledger.keysets.keysets[cashu.keyset_id]
            return_promises = await ledger._generate_change_promises(
                total_provided=total_provided,
                invoice_amount=invoice_amount,
                ln_fee_msat=paid_fee_msat,
                outputs=outputs,
                keyset=keyset,
            )
    except Exception as e:
        logger.debug(f"Cashu /melt: Exception: {str(e)}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Cashu: {str(e)}",
        )
    finally:
        # delete proofs from pending list
        await ledger._unset_proofs_pending(proofs)

    return GetMeltResponse(
        paid=status.paid, preimage=status.preimage, change=return_promises
    )


@cashu_ext.post(
    "/api/v1/{cashu_id}/check",
    name="Check proof state",
    summary="Check whether a proof is spent already or is pending in a transaction",
)
async def check_spendable(
    payload: CheckSpendableRequest, cashu_id: str
) -> CheckSpendableResponse:
    """Check whether a secret has been spent already or not."""
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    spendableList, pendingList = await ledger.check_proof_state(payload.proofs)
    return CheckSpendableResponse(spendable=spendableList, pending=pendingList)


@cashu_ext.post(
    "/api/v1/{cashu_id}/checkfees",
    name="Check fees",
    summary="Check fee reserve for a Lightning payment",
)
async def check_fees(payload: CheckFeesRequest, cashu_id: str) -> CheckFeesResponse:
    """
    Responds with the fees necessary to pay a Lightning invoice.
    Used by wallets for figuring out the fees they need to supply.
    This is can be useful for checking whether an invoice is internal (Cashu-to-Cashu).
    """
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    invoice_obj = bolt11.decode(payload.pr)
    assert invoice_obj.amount_msat, Exception("Invoice amount is zero.")
    fees_sat = fee_reserve_internal(invoice_obj.amount_msat)
    return CheckFeesResponse(fee=fees_sat)


@cashu_ext.post(
    "/api/v1/{cashu_id}/split",
    name="Split",
    summary="Split proofs at a specified amount",
)
async def split(
    payload: PostSplitRequest, cashu_id: str
) -> Union[PostSplitResponse, PostSplitResponse_Deprecated]:
    """
    Requetst a set of tokens with amount "total" to be split into two
    newly minted sets with amount "split" and "total-split".
    """
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    proofs = payload.proofs

    # !!!!!!! MAKE SURE THAT PROOFS ARE ONLY FROM THIS CASHU KEYSET ID
    # THIS IS NECESSARY BECAUSE THE CASHU BACKEND WILL ACCEPT ANY VALID
    # TOKENS
    accepted_keysets = [cashu.keyset_id]
    if ledger.master_key:
        # NOTE: bug fix, fee return tokens for v 0.3.1 are from the master keyset
        # we need to accept them but only if a master keyset is set, otherwise it's unsafe
        # to accept them.
        accepted_keysets += [ledger.keyset.id]
    assert all([p.id in accepted_keysets for p in proofs]), HTTPException(
        status_code=HTTPStatus.METHOD_NOT_ALLOWED,
        detail="Error: Tokens are from another mint.",
    )

    assert payload.outputs, Exception("no outputs provided.")
    try:
        keyset = ledger.keysets.keysets[cashu.keyset_id]
        promises = await ledger.split(
            proofs=payload.proofs,
            outputs=payload.outputs,
            keyset=keyset,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=str(exc),
        )
    if not promises:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="there was an error with the split",
        )
    if payload.amount:
        # BEGIN backwards compatibility < 0.13
        # old clients expect two lists of promises where the second one's amounts
        # sum up to `amount`. The first one is the rest.
        # The returned value `promises` has the form [keep1, keep2, ..., send1, send2, ...]
        # The sum of the sendx is `amount`. We need to split this into two lists and keep the order of the elements.
        frst_promises: List[BlindedSignature] = []
        scnd_promises: List[BlindedSignature] = []
        scnd_amount = 0
        for promise in promises[::-1]:  # we iterate backwards
            if scnd_amount < payload.amount:
                scnd_promises.insert(0, promise)  # and insert at the beginning
                scnd_amount += promise.amount
            else:
                frst_promises.insert(0, promise)  # and insert at the beginning
        logger.trace(
            f"Split into keep: {len(frst_promises)}: {sum([p.amount for p in frst_promises])} sat and send: {len(scnd_promises)}: {sum([p.amount for p in scnd_promises])} sat"
        )
        return PostSplitResponse_Deprecated(fst=frst_promises, snd=scnd_promises)
        # END backwards compatibility < 0.13
    else:
        return PostSplitResponse(promises=promises)


@cashu_ext.post(
    "/api/v1/{cashu_id}/restore",
    name="Restore",
    summary="Restores a blinded signature from a secret",
)
async def restore(payload: PostMintRequest) -> PostRestoreResponse:
    assert payload.outputs, Exception("no outputs provided.")
    outputs, promises = await ledger.restore(payload.outputs)
    return PostRestoreResponse(outputs=outputs, promises=promises)
