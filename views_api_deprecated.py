import asyncio
import math
from http import HTTPStatus
from typing import Dict, List, Union

from fastapi import Depends, Query
from lnbits import bolt11
from lnbits.core.crud import (get_installed_extension, get_standalone_payment,
                              get_user)
from lnbits.core.services import (check_transaction_status, create_invoice,
                                  fee_reserve, pay_invoice)
from lnbits.decorators import WalletTypeInfo, get_key_type, require_admin_key
from lnbits.helpers import urlsafe_short_hash
from lnbits.wallets.base import PaymentStatus
from loguru import logger
from starlette.exceptions import HTTPException

from . import cashu_ext, ledger
from .crud import create_cashu, delete_cashu, get_cashu, get_cashus
from .lib.cashu.core.errors import CashuError, LightningError, NotAllowedError
from .lib.cashu.core.helpers import sum_proofs
from .lib.cashu.core.settings import settings

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


from .ledger import (lnbits_get_melt_quote, lnbits_melt, lnbits_melt_quote,
                     lnbits_mint, lnbits_mint_quote)
# -------- cashu imports
from .lib.cashu.core.base import (BlindedSignature,
                                  CheckFeesRequest_deprecated,
                                  CheckFeesResponse_deprecated,
                                  CheckSpendableRequest_deprecated,
                                  CheckSpendableResponse_deprecated,
                                  GetInfoResponse_deprecated,
                                  GetMintResponse_deprecated,
                                  KeysetsResponse_deprecated,
                                  KeysResponse_deprecated,
                                  PostMeltQuoteRequest,
                                  PostMeltRequest_deprecated,
                                  PostMeltResponse_deprecated,
                                  PostMintQuoteRequest,
                                  PostMintRequest_deprecated,
                                  PostMintResponse_deprecated,
                                  PostRestoreResponse,
                                  PostSplitRequest_Deprecated,
                                  PostSplitResponse_Deprecated,
                                  PostSplitResponse_Very_Deprecated,
                                  SpentState)
from .lib.cashu.core.db import lock_table
from .models import Cashu

# --------- extension imports


#######################################
########### CASHU ENDPOINTS ###########
#######################################
@cashu_ext.get(
    "/api/v1/{cashu_id}/info",
    name="Mint information",
    summary="Mint information, operator contact information, and other info.",
    response_model=GetInfoResponse_deprecated,
    response_model_exclude_none=True,
    deprecated=True,
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

    logger.trace("> GET /info")
    return GetInfoResponse_deprecated(
        name=cashu.name,
        version=f"LNbitsCashu/{installed_version}",
        description=settings.mint_info_description,
        description_long=settings.mint_info_description_long,
        contact=settings.mint_info_contact,
        nuts=settings.mint_info_nuts,
        motd=settings.mint_info_motd,
        parameter={
            "max_peg_in": settings.mint_max_peg_in,
            "max_peg_out": settings.mint_max_peg_out,
            "peg_out_only": settings.mint_peg_out_only,
        },
    )


@cashu_ext.get(
    "/api/v1/{cashu_id}/keys",
    name="Mint public keys",
    summary="Get the public keys of the newest mint keyset",
    response_description=(
        "A dictionary of all supported token values of the mint and their associated"
        " public key of the current keyset."
    ),
    response_model=KeysResponse_deprecated,
    deprecated=True,
)
async def keys_deprecated(cashu_id: str):
    """Get the public keys of the mint"""
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    keyset = ledger.get_keyset(keyset_id=cashu.keyset_id)
    logger.trace("> GET /keys")
    keys = KeysResponse_deprecated.parse_obj(keyset)
    return keys.__root__


@cashu_ext.get(
    "/api/v1/{cashu_id}/keys/{idBase64Urlsafe}",
    name="Keyset public keys",
    summary="Public keys of a specific keyset",
    response_description=(
        "A dictionary of all supported token values of the mint and their associated"
        " public key for a specific keyset."
    ),
    response_model=KeysResponse_deprecated,
    deprecated=True,
)
async def keyset_deprecated(cashu_id: str, idBase64Urlsafe: str):
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
    keys = KeysResponse_deprecated.parse_obj(keyset)
    return keys.__root__


@cashu_ext.get(
    "/api/v1/{cashu_id}/keysets",
    name="Active keysets",
    summary="Get all active keyset id of the mind",
    response_model=KeysetsResponse_deprecated,
    response_description="A list of all active keyset ids of the mint.",
    deprecated=True,
)
async def keysets_deprecated(cashu_id: str) -> KeysetsResponse_deprecated:
    """Get the public keys of the mint"""
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )

    logger.trace("> GET /keysets")
    keysets = KeysetsResponse_deprecated(keysets=[cashu.keyset_id])
    return keysets


@cashu_ext.get(
    "/api/v1/{cashu_id}/mint",
    name="Request mint",
    summary="Request minting of new tokens",
    response_model=GetMintResponse_deprecated,
    response_description=(
        "A Lightning invoice to be paid and a hash to request minting of new tokens"
        " after payment."
    ),
    deprecated=True,
)
async def request_mint_deprecated(
    cashu_id: str, amount: int = 0
) -> GetMintResponse_deprecated:
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

    logger.trace(f"> GET /mint: amount={amount}")
    if amount > 21_000_000 * 100_000_000 or amount <= 0:
        raise CashuError(code=0, detail="Amount must be a valid amount of sat.")
    if settings.mint_peg_out_only:
        raise CashuError(code=0, detail="Mint does not allow minting new tokens.")
    quote = await lnbits_mint_quote(
        ledger, PostMintQuoteRequest(amount=amount, unit="sat"), cashu
    )
    resp = GetMintResponse_deprecated(pr=quote.request, hash=quote.quote)
    logger.trace(f"< GET /mint: {resp}")
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/mint",
    name="Mint tokens",
    summary="Mint tokens in exchange for a Bitcoin payment that the user has made",
    response_model=PostMintResponse_deprecated,
    response_description=(
        "A list of blinded signatures that can be used to create proofs."
    ),
    deprecated=True,
)
async def mint_deprecated(
    payload: PostMintRequest_deprecated,
    cashu_id: str,
    hash: str = Query(None),
    payment_hash: str = Query(None),
) -> PostMintResponse_deprecated:
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
    # BEGIN: backwards compatibility < 0.12 where we used to lookup payments with payment_hash
    # We use the payment_hash to lookup the hash from the database and pass that one along.
    hash = payment_hash or hash
    # END: backwards compatibility < 0.12

    # BEGIN BACKWARDS COMPATIBILITY < 0.15
    # Mint expects "id" in outputs to know which keyset to use to sign them.
    for output in payload.outputs:
        if not output.id:
            output.id = cashu.keyset_id
    # END BACKWARDS COMPATIBILITY < 0.15

    # BEGIN: backwards compatibility < 0.12 where we used to lookup payments with payment_hash
    # We use the payment_hash to lookup the hash from the database and pass that one along.
    hash = payment_hash or hash
    assert hash, "hash must be set."
    # END: backwards compatibility < 0.12
    try:
        promises = await lnbits_mint(
            ledger, outputs=payload.outputs, quote_id=hash, cashu=cashu
        )
    except Exception as e:
        logger.info(f"Error while minting tokens: {e}")
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Error while minting tokens: {e}",
        )
    blinded_signatures = PostMintResponse_deprecated(promises=promises)

    logger.trace(f"< POST /mint: {blinded_signatures}")
    return blinded_signatures


@cashu_ext.post(
    "/api/v1/{cashu_id}/melt",
    name="Melt tokens",
    summary=(
        "Melt tokens for a Bitcoin payment that the mint will make for the user in"
        " exchange"
    ),
    response_model=PostMeltResponse_deprecated,
    response_description=(
        "The state of the payment, a preimage as proof of payment, and a list of"
        " promises for change."
    ),
    deprecated=True,
)
async def melt_deprecated(
    payload: PostMeltRequest_deprecated, cashu_id: str
) -> PostMeltResponse_deprecated:
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

    logger.trace(f"> POST /melt: {payload}")
    # BEGIN BACKWARDS COMPATIBILITY < 0.14: add "id" to outputs
    if payload.outputs:
        for output in payload.outputs:
            if not output.id:
                output.id = cashu.keyset_id
    # END BACKWARDS COMPATIBILITY < 0.14
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(request=payload.pr, unit="sat")
    )
    preimage, change_promises = await lnbits_melt(
        ledger,
        proofs=payload.proofs,
        quote=quote.quote,
        outputs=payload.outputs,
        cashu=cashu,
    )
    resp = PostMeltResponse_deprecated(
        paid=True, preimage=preimage, change=change_promises
    )
    logger.trace(f"< POST /melt: {resp}")
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/checkfees",
    name="Check fees",
    summary="Check fee reserve for a Lightning payment",
    response_model=CheckFeesResponse_deprecated,
    response_description="The fees necessary to pay a Lightning invoice.",
    deprecated=True,
)
async def check_fees(
    payload: CheckFeesRequest_deprecated, cashu_id: str
) -> CheckFeesResponse_deprecated:
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
    quote = await lnbits_melt_quote(
        ledger, PostMeltQuoteRequest(request=payload.pr, unit="sat"), cashu
    )
    fees_sat = quote.fee_reserve
    logger.trace(f"< POST /checkfees: {fees_sat}")
    return CheckFeesResponse_deprecated(fee=fees_sat)


@cashu_ext.post(
    "/api/v1/{cashu_id}/split",
    name="Split",
    summary="Split proofs at a specified amount",
    # response_model=Union[
    #     PostSplitResponse_Very_Deprecated, PostSplitResponse_Deprecated
    # ],
    response_description=(
        "A list of blinded signatures that can be used to create proofs."
    ),
    deprecated=True,
)
async def split_deprecated(payload: PostSplitRequest_Deprecated, cashu_id: str):
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
    # BEGIN BACKWARDS COMPATIBILITY < 0.14: add "id" to outputs
    if payload.outputs:
        for output in payload.outputs:
            if not output.id:
                output.id = cashu.keyset_id
    # END BACKWARDS COMPATIBILITY < 0.14
    keyset = ledger.keysets[cashu.keyset_id]
    promises = await ledger.split(
        proofs=payload.proofs,
        outputs=payload.outputs,
        keyset=keyset,
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
        return PostSplitResponse_Very_Deprecated(fst=frst_promises, snd=scnd_promises)
        # END backwards compatibility < 0.13
    else:
        return PostSplitResponse_Deprecated(promises=promises)


@cashu_ext.post(
    "/api/v1/{cashu_id}/check",
    name="Check proof state",
    summary="Check whether a proof is spent already or is pending in a transaction",
    response_model=CheckSpendableResponse_deprecated,
    response_description=(
        "Two lists of booleans indicating whether the provided proofs "
        "are spendable or pending in a transaction respectively."
    ),
    deprecated=True,
)
async def check_spendable_deprecated(
    payload: CheckSpendableRequest_deprecated, cashu_id: str
) -> CheckSpendableResponse_deprecated:
    """Check whether a secret has been spent already or not."""
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    logger.trace(f"> POST /check: {payload}")
    proofs_state = await ledger.check_proofs_state([p.secret for p in payload.proofs])
    spendableList: List[bool] = []
    pendingList: List[bool] = []
    for proof_state in proofs_state:
        if proof_state.state == SpentState.unspent:
            spendableList.append(True)
            pendingList.append(False)
        elif proof_state.state == SpentState.spent:
            spendableList.append(False)
            pendingList.append(False)
        elif proof_state.state == SpentState.pending:
            spendableList.append(True)
            pendingList.append(True)
    return CheckSpendableResponse_deprecated(
        spendable=spendableList, pending=pendingList
    )


@cashu_ext.post(
    "/api/v1/{cashu_id}/restore",
    name="Restore",
    summary="Restores a blinded signature from a secret",
    response_model=PostRestoreResponse,
    response_description=(
        "Two lists with the first being the list of the provided outputs that "
        "have an associated blinded signature which is given in the second list."
    ),
    deprecated=True,
)
async def restore(payload: PostMintRequest_deprecated) -> PostRestoreResponse:
    assert payload.outputs, Exception("no outputs provided.")
    outputs, promises = await ledger.restore(payload.outputs)
    return PostRestoreResponse(outputs=outputs, promises=promises)
