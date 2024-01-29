import asyncio
import math
import time
import datetime
from http import HTTPStatus
from typing import Dict, List, Union, Any

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
from .lib.cashu.core.errors import CashuError, NotAllowedError, LightningError
from .lib.cashu.core.settings import settings
from .ledger import (
    lnbits_get_quote,
    lnbits_mint,
    lnbits_melt_quote,
    lnbits_melt,
    lnbits_get_melt_quote,
)

# -------- cashu imports
from .lib.cashu.core.base import (
    GetInfoResponse,
    KeysetsResponse,
    KeysetsResponseKeyset,
    KeysResponse,
    KeysResponseKeyset,
    PostCheckStateRequest,
    PostCheckStateResponse,
    PostMeltQuoteRequest,
    PostMeltQuoteResponse,
    PostMeltRequest,
    PostMeltResponse,
    PostMintQuoteRequest,
    PostMintQuoteResponse,
    PostMintRequest,
    PostMintResponse,
    PostRestoreResponse,
    PostSplitRequest,
    PostSplitResponse,
    DLEQ,
    Amount,
    BlindedMessage,
    BlindedSignature,
    MeltQuote,
    Method,
    MintKeyset,
    MintQuote,
    PostMeltQuoteRequest,
    PostMeltQuoteResponse,
    PostMintQuoteRequest,
    Proof,
    ProofState,
    SpentState,
    Unit,
)
from .lib.cashu.lightning.base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentStatus,
)
from .lib.cashu.core.crypto.keys import (
    derive_keyset_id,
    derive_keyset_id_deprecated,
    derive_pubkey,
    random_hash,
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
    keyset = await ledger.activate_keyset(
        derivation_path="m/0'/0'/0'", seed=urlsafe_short_hash()
    )
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
    "/api/v1/{cashu_id}/v1/info",
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

    # determine all method-unit pairs
    method_unit_pairs: List[List[str]] = []
    for method, unit_dict in ledger.backends.items():
        for unit in unit_dict.keys():
            method_unit_pairs.append([method.name, unit.name])
    supported_dict = dict(supported=True)

    mint_features: Dict[int, Dict[str, Any]] = {
        4: dict(
            methods=method_unit_pairs,
        ),
        5: dict(
            methods=method_unit_pairs,
            disabled=False,
        ),
        7: supported_dict,
        8: supported_dict,
        9: supported_dict,
        10: supported_dict,
        11: supported_dict,
        12: supported_dict,
    }

    return GetInfoResponse(
        name=cashu.name,
        version=f"LNbitsCashu/{installed_version}",
        nuts=mint_features,
    )


@cashu_ext.get(
    "/api/v1/{cashu_id}/v1/keys",
    name="Mint public keys",
    summary="Get the public keys of the newest mint keyset",
    response_description=(
        "All supported token values their associated public keys for all active keysets"
    ),
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

    keyset = ledger.keysets[cashu.keyset_id]

    return KeysResponse(
        keysets=[
            KeysResponseKeyset(
                id=keyset.id,
                unit=keyset.unit.name,
                keys={k: v for k, v in keyset.public_keys_hex.items()},
            )
        ]
    )


@cashu_ext.get(
    "/api/v1/{cashu_id}/v1/keys/{keyset_id}",
    name="Keyset public keys",
    summary="Public keys of a specific keyset",
    response_description=(
        "All supported token values of the mint and their associated"
        " public key for a specific keyset."
    ),
    status_code=HTTPStatus.OK,
    response_model=KeysResponse,
)
async def keyset_keys(cashu_id: str, keyset_id: str):
    """
    Get the public keys of the mint from a specific keyset id.
    """

    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    if keyset_id != cashu.keyset_id:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Keyset not found."
        )

    # BEGIN BACKWARDS COMPATIBILITY < 0.15.0
    # if keyset_id is not hex, we assume it is base64 and sanitize it
    try:
        int(keyset_id, 16)
    except ValueError:
        keyset_id = keyset_id.replace("-", "+").replace("_", "/")
    # END BACKWARDS COMPATIBILITY < 0.15.0

    keyset = ledger.keysets.get(keyset_id)
    if keyset is None:
        raise CashuError(code=0, detail="Keyset not found.")
    keyset_for_response = KeysResponseKeyset(
        id=keyset.id,
        unit=keyset.unit.name,
        keys={k: v for k, v in keyset.public_keys_hex.items()},
    )
    return KeysResponse(keysets=[keyset_for_response])


@cashu_ext.get(
    "/api/v1/{cashu_id}/v1/keysets",
    status_code=HTTPStatus.OK,
    name="Active keysets",
    summary="Get all active keyset id of the mind",
    response_model=KeysetsResponse,
    response_description="A list of all active keyset ids of the mint.",
)
async def keysets(cashu_id: str) -> KeysetsResponse:
    """Get the public keys of the mint"""
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)

    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    keyset = ledger.keysets[cashu.keyset_id]

    return KeysetsResponse(
        keysets=[
            KeysetsResponseKeyset(
                id=keyset.id, unit=keyset.unit.name, active=keyset.active or False
            )
        ]
    )


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/mint/quote/bolt11",
    name="Request mint quote",
    summary="Request a quote for minting of new tokens",
    response_model=PostMintQuoteResponse,
    response_description="A payment request to mint tokens of a denomination",
)
async def mint_quote(
    cashu_id: str, payload: PostMintQuoteRequest
) -> PostMintQuoteResponse:
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

    quote_request = payload
    quote = await lnbits_get_quote(ledger, quote_request, cashu)
    resp = PostMintQuoteResponse(
        request=quote.request,
        quote=quote.quote,
        paid=quote.paid,
        expiry=quote.expiry,
    )
    return resp


@cashu_ext.get(
    "/api/v1/{cashu_id}/v1/mint/quote/bolt11/{quote}",
    summary="Get mint quote",
    response_model=PostMintQuoteResponse,
    response_description="Get an existing mint quote to check its status.",
)
async def get_mint_quote(cashu_id: str, quote: str) -> PostMintQuoteResponse:
    """
    Get mint quote state.
    """
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)
    if not cashu:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    logger.trace(f"> GET /v1/mint/quote/bolt11/{quote}")
    mint_quote = await ledger.get_mint_quote(quote)
    resp = PostMintQuoteResponse(
        quote=mint_quote.quote,
        request=mint_quote.request,
        paid=mint_quote.paid,
        expiry=mint_quote.expiry,
    )
    logger.trace(f"< GET /v1/mint/quote/bolt11/{quote}")
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/mint/bolt11",
    name="Mint tokens with a Lightning payment",
    summary="Mint tokens by paying a bolt11 Lightning invoice.",
    response_model=PostMintResponse,
    response_description=(
        "A list of blinded signatures that can be used to create proofs."
    ),
)
async def mint(
    payload: PostMintRequest,
    cashu_id: str,
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

    outputs = payload.outputs
    quote_id = payload.quote
    promises = await lnbits_mint(ledger, outputs, quote_id, cashu)
    blinded_signatures = PostMintResponse(signatures=promises)
    logger.trace(f"< POST /v1/mint/bolt11: {blinded_signatures}")
    return blinded_signatures


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/melt/quote/bolt11",
    summary="Request a quote for melting tokens",
    response_model=PostMeltQuoteResponse,
    response_description="Melt tokens for a payment on a supported payment method.",
)
async def get_melt_quote(
    payload: PostMeltQuoteRequest,
    cashu_id: str,
) -> PostMeltQuoteResponse:
    """
    Request a quote for melting tokens.
    """
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    logger.trace(f"> POST /v1/melt/quote/bolt11: {payload}")
    quote = await lnbits_melt_quote(ledger, payload, cashu)
    logger.trace(f"< POST /v1/melt/quote/bolt11: {quote}")
    return quote


@cashu_ext.get(
    "/api/v1/{cashu_id}/v1/melt/quote/bolt11/{quote}",
    summary="Get melt quote",
    response_model=PostMeltQuoteResponse,
    response_description="Get an existing melt quote to check its status.",
)
async def melt_quote(quote: str, cashu_id: str) -> PostMeltQuoteResponse:
    """
    Get melt quote state.
    """
    cashu: Union[Cashu, None] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    logger.trace(f"> GET /v1/melt/quote/bolt11/{quote}")
    melt_quote = await lnbits_get_melt_quote(quote)
    resp = PostMeltQuoteResponse(
        quote=melt_quote.quote,
        amount=melt_quote.amount,
        fee_reserve=melt_quote.fee_reserve,
        paid=melt_quote.paid,
    )
    logger.trace(f"< GET /v1/melt/quote/bolt11/{quote}")
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/melt/bolt11",
    name="Melt tokens",
    summary=(
        "Melt tokens for a Bitcoin payment that the mint will make for the user in"
        " exchange"
    ),
    response_model=PostMeltResponse,
    response_description=(
        "The state of the payment, a preimage as proof of payment, and a list of"
        " promises for change."
    ),
)
async def melt(payload: PostMeltRequest, cashu_id: str) -> PostMeltResponse:
    """Invalidates proofs and pays a Lightning invoice."""
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    proofs = payload.inputs
    quote = payload.quote
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
    preimage, change_promises = await lnbits_melt(
        ledger,
        proofs=payload.inputs,
        quote=payload.quote,
        outputs=payload.outputs,
        cashu=cashu,
    )
    resp = PostMeltResponse(
        paid=True, payment_preimage=preimage, change=change_promises
    )
    logger.trace(f"< POST /v1/melt/bolt11: {resp}")
    return resp


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/swap",
    name="Swap tokens",
    summary="Swap inputs for outputs of the same value",
    response_model=PostSplitResponse,
    response_description=(
        "An array of blinded signatures that can be used to create proofs."
    ),
)
async def swap(
    payload: PostSplitRequest,
    cashu_id: str,
) -> PostSplitResponse:
    """
    Requests a set of Proofs to be split into two a new set of BlindedSignatures.

    This endpoint is used by Alice to split a set of proofs before making a payment to Carol.
    It is then used by Carol (by setting split=total) to redeem the tokens.
    """
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    proofs = payload.inputs

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

    logger.trace(f"> POST /v1/swap: {payload}")
    assert payload.outputs, Exception("no outputs provided.")
    keyset = ledger.keysets[cashu.keyset_id]
    signatures = await ledger.split(
        proofs=payload.inputs, outputs=payload.outputs, keyset=keyset
    )

    return PostSplitResponse(signatures=signatures)


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/checkstate",
    name="Check proof state",
    summary="Check whether a proof is spent already or is pending in a transaction",
    response_model=PostCheckStateResponse,
    response_description=(
        "Two lists of booleans indicating whether the provided proofs "
        "are spendable or pending in a transaction respectively."
    ),
)
async def check_state(
    payload: PostCheckStateRequest,
    cashu_id: str,
) -> PostCheckStateResponse:
    """Check whether a secret has been spent already or not."""
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    logger.trace(f"> POST /v1/checkstate: {payload}")
    proof_states = await ledger.check_proofs_state(payload.secrets)
    return PostCheckStateResponse(states=proof_states)


@cashu_ext.post(
    "/api/v1/{cashu_id}/v1/restore",
    name="Restore",
    summary="Restores a blinded signature from a secret",
    response_model=PostRestoreResponse,
    response_description=(
        "Two lists with the first being the list of the provided outputs that "
        "have an associated blinded signature which is given in the second list."
    ),
)
async def restore(payload: PostMintRequest, cashu_id: str) -> PostRestoreResponse:
    cashu: Union[None, Cashu] = await get_cashu(cashu_id)
    if cashu is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
        )
    assert payload.outputs, Exception("no outputs provided.")
    outputs, promises = await ledger.restore(payload.outputs)
    return PostRestoreResponse(outputs=outputs, promises=promises)


# @cashu_ext.post(
#     "/api/v1/{cashu_id}/v1/check",
#     name="Check proof state",
#     summary="Check whether a proof is spent already or is pending in a transaction",
# )
# async def check_spendable(payload, cashu_id: str):
#     """Check whether a secret has been spent already or not."""
#     cashu: Union[None, Cashu] = await get_cashu(cashu_id)
#     if cashu is None:
#         raise HTTPException(
#             status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
#         )
#     spendableList, pendingList = await ledger.check_proof_state(payload.proofs)
#     return CheckSpendableResponse(spendable=spendableList, pending=pendingList)


# @cashu_ext.post(
#     "/api/v1/{cashu_id}/v1/checkfees",
#     name="Check fees",
#     summary="Check fee reserve for a Lightning payment",
# )
# async def check_fees(payload, cashu_id: str):
#     """
#     Responds with the fees necessary to pay a Lightning invoice.
#     Used by wallets for figuring out the fees they need to supply.
#     This is can be useful for checking whether an invoice is internal (Cashu-to-Cashu).
#     """
#     cashu: Union[None, Cashu] = await get_cashu(cashu_id)
#     if cashu is None:
#         raise HTTPException(
#             status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
#         )
#     invoice_obj = bolt11.decode(payload.pr)
#     assert invoice_obj.amount_msat, Exception("Invoice amount is zero.")
#     fees_sat = fee_reserve_internal(invoice_obj.amount_msat)
#     return CheckFeesResponse(fee=fees_sat)


# @cashu_ext.post(
#     "/api/v1/{cashu_id}/v1/split",
#     name="Split",
#     summary="Split proofs at a specified amount",
# )
# async def split(payload: PostSplitRequest, cashu_id: str):
#     """
#     Requetst a set of tokens with amount "total" to be split into two
#     newly minted sets with amount "split" and "total-split".
#     """
#     cashu: Union[None, Cashu] = await get_cashu(cashu_id)
#     if cashu is None:
#         raise HTTPException(
#             status_code=HTTPStatus.NOT_FOUND, detail="Mint does not exist."
#         )
#     proofs = payload.proofs

#     # !!!!!!! MAKE SURE THAT PROOFS ARE ONLY FROM THIS CASHU KEYSET ID
#     # THIS IS NECESSARY BECAUSE THE CASHU BACKEND WILL ACCEPT ANY VALID
#     # TOKENS
#     accepted_keysets = [cashu.keyset_id]
#     if ledger.master_key:
#         # NOTE: bug fix, fee return tokens for v 0.3.1 are from the master keyset
#         # we need to accept them but only if a master keyset is set, otherwise it's unsafe
#         # to accept them.
#         accepted_keysets += [ledger.keyset.id]
#     assert all([p.id in accepted_keysets for p in proofs]), HTTPException(
#         status_code=HTTPStatus.METHOD_NOT_ALLOWED,
#         detail="Error: Tokens are from another mint.",
#     )

#     assert payload.outputs, Exception("no outputs provided.")
#     try:
#         keyset = ledger.keysets.keysets[cashu.keyset_id]
#         promises = await ledger.split(
#             proofs=payload.proofs,
#             outputs=payload.outputs,
#             keyset=keyset,
#             amount=payload.amount,
#         )
#     except Exception as exc:
#         raise HTTPException(
#             status_code=HTTPStatus.BAD_REQUEST,
#             detail=str(exc),
#         )
#     if not promises:
#         raise HTTPException(
#             status_code=HTTPStatus.BAD_REQUEST,
#             detail="there was an error with the split",
#         )
#     if payload.amount:
#         # BEGIN backwards compatibility < 0.13
#         # old clients expect two lists of promises where the second one's amounts
#         # sum up to `amount`. The first one is the rest.
#         # The returned value `promises` has the form [keep1, keep2, ..., send1, send2, ...]
#         # The sum of the sendx is `amount`. We need to split this into two lists and keep the order of the elements.
#         frst_promises: List[BlindedSignature] = []
#         scnd_promises: List[BlindedSignature] = []
#         scnd_amount = 0
#         for promise in promises[::-1]:  # we iterate backwards
#             if scnd_amount < payload.amount:
#                 scnd_promises.insert(0, promise)  # and insert at the beginning
#                 scnd_amount += promise.amount
#             else:
#                 frst_promises.insert(0, promise)  # and insert at the beginning
#         logger.trace(
#             f"Split into keep: {len(frst_promises)}: {sum([p.amount for p in frst_promises])} sat and send: {len(scnd_promises)}: {sum([p.amount for p in scnd_promises])} sat"
#         )
#         return PostSplitResponse_Deprecated(fst=frst_promises, snd=scnd_promises)
#         # END backwards compatibility < 0.13
#     else:
#         return PostSplitResponse(promises=promises)


# @cashu_ext.post(
#     "/api/v1/{cashu_id}/v1/restore",
#     name="Restore",
#     summary="Restores a blinded signature from a secret",
# )
# async def restore(payload: PostMintRequest) -> PostRestoreResponse:
#     assert payload.outputs, Exception("no outputs provided.")
#     outputs, promises = await ledger.restore(payload.outputs)
#     return PostRestoreResponse(outputs=outputs, promises=promises)
