import asyncio
import math
import stat
import time
from typing import List, Optional, Tuple

from lnbits import bolt11
from lnbits.core.crud import get_standalone_payment
from lnbits.core.services import (
    check_transaction_status,
    create_invoice,
    fee_reserve,
    pay_invoice,
)
from loguru import logger

from .lib.cashu.core.base import (
    DLEQ,
    Amount,
    BlindedMessage,
    BlindedSignature,
    GetInfoResponse,
    KeysetsResponse,
    KeysetsResponseKeyset,
    KeysResponse,
    KeysResponseKeyset,
    MeltQuote,
    Method,
    MintKeyset,
    MintQuote,
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
    Proof,
    ProofState,
    SpentState,
    Unit,
)
from .lib.cashu.core.crypto.keys import (
    derive_keyset_id,
    derive_keyset_id_deprecated,
    derive_pubkey,
    random_hash,
)
from .lib.cashu.core.errors import (
    CashuError,
    LightningError,
    NotAllowedError,
    QuoteNotPaidError,
)
from .lib.cashu.core.helpers import sum_proofs
from .lib.cashu.core.settings import settings
from .lib.cashu.lightning.base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentStatus,
)

# -------- cashu imports
from .lib.cashu.mint.ledger import Ledger
from .models import Cashu

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


async def lnbits_mint_quote(
    ledger: Ledger, quote_request: PostMintQuoteRequest, cashu: Cashu
) -> MintQuote:
    if cashu.mint_peg_out_only:
        raise NotAllowedError("Mint does not allow minting new tokens.")
    assert quote_request.amount > 0, "amount must be positive"
    if settings.mint_max_peg_in and quote_request.amount > settings.mint_max_peg_in:
        raise NotAllowedError(f"Maximum mint amount is {settings.mint_max_peg_in} sat.")
    if settings.mint_peg_out_only:
        raise NotAllowedError("Mint does not allow minting new tokens.")

    unit = Unit[quote_request.unit]
    method = Method.bolt11
    if settings.mint_max_balance:
        balance = await ledger.get_balance()
        if balance + quote_request.amount > settings.mint_max_balance:
            raise NotAllowedError("Mint has reached maximum balance.")

    logger.trace(f"requesting invoice for {unit.str(quote_request.amount)}")
    payment_hash, payment_request = await create_invoice(
        wallet_id=cashu.wallet,
        amount=quote_request.amount,
        memo=f"{cashu.name}",
        extra={"tag": "cashu"},
    )
    invoice_response = InvoiceResponse(
        ok=True,
        checking_id=payment_hash,
        payment_request=payment_request,
        error_message=None,
    )

    logger.trace(
        f"got invoice {invoice_response.payment_request} with checking id"
        f" {invoice_response.checking_id}"
    )

    assert (
        invoice_response.payment_request and invoice_response.checking_id
    ), LightningError("could not fetch bolt11 payment request from backend")

    # get invoice expiry time
    invoice_obj = bolt11.decode(invoice_response.payment_request)

    quote = MintQuote(
        quote=random_hash(),
        method=method.name,
        request=invoice_response.payment_request,
        checking_id=invoice_response.checking_id,
        unit=quote_request.unit,
        amount=quote_request.amount,
        issued=False,
        paid=False,
        created_time=int(time.time()),
        expiry=invoice_obj.expiry or 0,
    )
    await ledger.crud.store_mint_quote(
        quote=quote,
        db=ledger.db,
    )
    return quote


async def lnbits_get_mint_quote(
    ledger: Ledger, quote_id: str, cashu: Cashu
) -> MintQuote:
    quote = await ledger.crud.get_mint_quote(quote_id=quote_id, db=ledger.db)
    assert quote, "quote not found"
    assert quote.method == Method.bolt11.name, "only bolt11 supported"
    unit = Unit[quote.unit]
    method = Method[quote.method]
    if not quote.paid:
        logger.trace(f"Lightning: checking invoice {quote.checking_id}")
        status: PaymentStatus = await check_transaction_status(
            cashu.wallet, quote.checking_id
        )
        if status.paid:
            logger.trace(f"Setting quote {quote_id} as paid")
            quote.paid = True
            await ledger.crud.update_mint_quote(quote=quote, db=ledger.db)
    return quote


async def lnbits_mint(
    ledger: Ledger, outputs: List[BlindedSignature], quote_id: str, cashu: Cashu
) -> List[BlindedSignature]:
    logger.trace("called mint")
    if cashu.mint_peg_out_only:
        raise NotAllowedError("Mint does not allow minting new tokens.")
    keyset = ledger.keysets[cashu.keyset_id]

    await ledger._verify_outputs(outputs)
    sum_amount_outputs = sum([b.amount for b in outputs])

    ledger.locks[quote_id] = (
        ledger.locks.get(quote_id) or asyncio.Lock()
    )  # create a new lock if it doesn't exist
    async with ledger.locks[quote_id]:
        quote = await lnbits_get_mint_quote(ledger, quote_id, cashu)
        assert quote.paid, QuoteNotPaidError()
        assert not quote.issued, "quote already issued"
        assert (
            quote.amount == sum_amount_outputs
        ), "amount to mint does not match quote amount"
        if quote.expiry:
            assert quote.expiry > int(time.time()), "quote expired"

        promises = await ledger._generate_promises(outputs, keyset)
        logger.trace("generated promises")

        logger.trace(f"crud: setting quote {quote_id} as issued")
        quote.issued = True
        await ledger.crud.update_mint_quote(quote=quote, db=ledger.db)
    del ledger.locks[quote_id]
    return promises


async def lnbits_melt_quote(
    self: Ledger, melt_quote: PostMeltQuoteRequest, cashu: Cashu
) -> PostMeltQuoteResponse:
    """Creates a melt quote and stores it in the database.

    Args:
        melt_quote (PostMeltQuoteRequest): Melt quote request.

    Raises:
        Exception: Quote invalid.
        Exception: Quote already paid.
        Exception: Quote already issued.

    Returns:
        PostMeltQuoteResponse: Melt quote response.
    """
    unit = Unit[melt_quote.unit]
    method = Method.bolt11
    invoice_obj = bolt11.decode(melt_quote.request)
    assert invoice_obj.amount_msat, "invoice has no amount."

    # check if there is a mint quote with the same payment request
    # so that we can handle the transaction internally without lightning
    # and respond with zero fees
    mint_quote = await self.crud.get_mint_quote_by_checking_id(
        checking_id=invoice_obj.payment_hash, db=self.db
    )
    if mint_quote:
        # internal transaction, validate and return amount from
        # associated mint quote and demand zero fees
        assert (
            Amount(unit, mint_quote.amount).to(Unit.msat).amount
            == invoice_obj.amount_msat
        ), "amounts do not match"
        assert melt_quote.request == mint_quote.request, "bolt11 requests do not match"
        assert mint_quote.unit == melt_quote.unit, "units do not match"
        assert mint_quote.method == method.name, "methods do not match"
        assert not mint_quote.paid, "mint quote already paid"
        assert not mint_quote.issued, "mint quote already issued"
        payment_quote = PaymentQuoteResponse(
            checking_id=mint_quote.checking_id,
            amount=Amount(unit, mint_quote.amount),
            fee=Amount(unit=Unit.msat, amount=0),
        )
        logger.info(
            f"Issuing internal melt quote: {melt_quote.request} ->"
            f" {mint_quote.quote} ({mint_quote.amount} {mint_quote.unit})"
        )
    else:
        # not internal, get quote by backend
        invoice_obj = bolt11.decode(melt_quote.request)
        assert invoice_obj.amount_msat, Exception("Invoice amount is zero.")
        fees_sat = fee_reserve_internal(invoice_obj.amount_msat)
        payment_quote = PaymentQuoteResponse(
            checking_id=invoice_obj.payment_hash,
            amount=Amount(
                unit=Unit.sat, amount=math.ceil(invoice_obj.amount_msat / 1000)
            ),
            fee=Amount(unit=Unit.sat, amount=fees_sat),
        )

    quote = MeltQuote(
        quote=random_hash(),
        method=method.name,
        request=melt_quote.request,
        checking_id=payment_quote.checking_id,
        unit=melt_quote.unit,
        amount=payment_quote.amount.to(unit).amount,
        paid=False,
        fee_reserve=payment_quote.fee.to(unit).amount,
        created_time=int(time.time()),
    )
    await self.crud.store_melt_quote(quote=quote, db=self.db)
    return PostMeltQuoteResponse(
        quote=quote.quote,
        amount=quote.amount,
        fee_reserve=quote.fee_reserve,
        paid=quote.paid,
    )


async def lnbits_get_melt_quote(self, quote_id: str, cashu: Cashu) -> MeltQuote:
    """Returns a melt quote.

    If melt quote is not paid yet, checks with the backend for the state of the payment request.

    If the quote has been paid, updates the melt quote in the database.

    Args:
        quote_id (str): ID of the melt quote.

    Raises:
        Exception: Quote not found.

    Returns:
        MeltQuote: Melt quote object.
    """
    melt_quote = await self.crud.get_melt_quote(quote_id=quote_id, db=self.db)
    assert melt_quote, "quote not found"
    assert melt_quote.method == Method.bolt11.name, "only bolt11 supported"
    unit = Unit[melt_quote.unit]
    method = Method[melt_quote.method]

    # we only check the state with the backend if there is no associated internal
    # mint quote for this melt quote
    mint_quote = await self.crud.get_mint_quote_by_checking_id(
        checking_id=melt_quote.checking_id, db=self.db
    )

    if not melt_quote.paid and not mint_quote:
        logger.trace(
            "Lightning: checking outgoing Lightning payment"
            f" {melt_quote.checking_id}"
        )
        # get the actual paid fees from the db entry
        payment = await get_standalone_payment(melt_quote.checking_id)

        # payment not found, return the quote as is
        if not payment:
            return melt_quote

        paid_fee_msat = payment.fee
        status = PaymentStatus(
            paid=not payment.pending,
            fee=Amount(unit=Unit.sat, amount=math.ceil(paid_fee_msat / 1000)),
            preimage=payment.preimage,
        )

        if status.paid:
            logger.trace(f"Setting quote {quote_id} as paid")
            melt_quote.paid = True
            if status.fee:
                melt_quote.fee_paid = status.fee.to(unit).amount
            if status.preimage:
                melt_quote.proof = status.preimage
            melt_quote.paid_time = int(time.time())
            await self.crud.update_melt_quote(quote=melt_quote, db=self.db)

    return melt_quote


async def lnbits_melt(
    self: Ledger,
    *,
    proofs: List[Proof],
    quote: str,
    outputs: Optional[List[BlindedMessage]] = None,
    cashu: Cashu,
) -> Tuple[str, List[BlindedSignature]]:
    """Invalidates proofs and pays a Lightning invoice.

    Args:
        proofs (List[Proof]): Proofs provided for paying the Lightning invoice
        quote (str): ID of the melt quote.
        outputs (Optional[List[BlindedMessage]]): Blank outputs for returning overpaid fees to the wallet.

    Raises:
        e: Lightning payment unsuccessful

    Returns:
        Tuple[str, List[BlindedMessage]]: Proof of payment and signed outputs for returning overpaid fees to wallet.
    """
    # get melt quote and settle transaction internally if possible
    melt_quote = await lnbits_get_melt_quote(self, quote_id=quote, cashu=cashu)
    method = Method[melt_quote.method]
    unit = Unit[melt_quote.unit]
    assert not melt_quote.paid, "melt quote already paid"

    # make sure that the outputs (for fee return) are in the same unit as the quote
    if outputs:
        await self._verify_outputs(outputs, skip_amount_check=True)
        assert outputs[0].id, "output id not set"
        outputs_unit = self.keysets[outputs[0].id].unit
        assert melt_quote.unit == outputs_unit.name, (
            f"output unit {outputs_unit.name} does not match quote unit"
            f" {melt_quote.unit}"
        )

    # verify that the amount of the input proofs is equal to the amount of the quote
    total_provided = sum_proofs(proofs)
    total_needed = melt_quote.amount + (melt_quote.fee_reserve or 0)
    assert total_provided >= total_needed, (
        f"not enough inputs provided for melt. Provided: {total_provided}, needed:"
        f" {total_needed}"
    )

    # verify that the amount of the proofs is not larger than the maximum allowed
    if settings.mint_max_peg_out and total_provided > settings.mint_max_peg_out:
        raise NotAllowedError(
            f"Maximum melt amount is {settings.mint_max_peg_out} sat."
        )

    # verify inputs and their spending conditions
    await self.verify_inputs_and_outputs(proofs=proofs)

    # set proofs to pending to avoid race conditions
    await self._set_proofs_pending(proofs)
    try:
        melt_quote = await self.melt_mint_settle_internally(melt_quote)

        # quote not paid yet (not internal), pay it with the backend
        if not melt_quote.paid:
            logger.debug(f"Lightning: pay invoice {melt_quote.request}")
            invoice_obj = bolt11.decode(melt_quote.request)
            checking_id = await pay_invoice(
                wallet_id=cashu.wallet,
                payment_request=melt_quote.request,
                description="Pay Cashu invoice",
                extra={"tag": "cashu", "cashu_name": cashu.name},
            )
            logger.debug(
                f"Cashu: Wallet {cashu.wallet} checking PaymentStatus of {invoice_obj.payment_hash}"
            )
            status = await check_transaction_status(
                cashu.wallet, invoice_obj.payment_hash
            )
            payment = PaymentResponse(
                ok=status.paid,
                checking_id=checking_id,
                fee=Amount(unit=Unit.sat, amount=math.ceil(status.fee_msat / 1000)),
                preimage=status.preimage,
            )
            logger.debug(
                f"Melt status: {payment.ok}: preimage: {payment.preimage},"
                f" fee: {payment.fee.str() if payment.fee else 0}"
            )
            if not payment.ok:
                raise LightningError("Lightning payment unsuccessful.")
            if payment.fee:
                melt_quote.fee_paid = payment.fee.to(to_unit=unit, round="up").amount
            if payment.preimage:
                melt_quote.proof = payment.preimage
            # set quote as paid
            melt_quote.paid = True
            melt_quote.paid_time = int(time.time())
            await self.crud.update_melt_quote(quote=melt_quote, db=self.db)

        # melt successful, invalidate proofs
        await self._invalidate_proofs(proofs)

        # prepare change to compensate wallet for overpaid fees
        return_promises: List[BlindedSignature] = []
        if outputs:
            assert outputs[0].id, "output id not set"
            keyset = self.keysets[cashu.keyset_id]
            return_promises = await self._generate_change_promises(
                input_amount=total_provided,
                output_amount=melt_quote.amount,
                output_fee_paid=melt_quote.fee_paid,
                outputs=outputs,
                keyset=keyset,
            )

    except Exception as e:
        logger.trace(f"Melt exception: {e}")
        raise e
    finally:
        # delete proofs from pending list
        await self._unset_proofs_pending(proofs)

    return melt_quote.proof or "", return_promises
