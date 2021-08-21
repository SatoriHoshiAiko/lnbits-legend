import time
import trio
import traceback
from http import HTTPStatus
from quart import current_app
from typing import List, Callable

from lnbits.settings import WALLET
from lnbits.core.crud import (
    get_payments,
    get_standalone_payment,
    delete_expired_invoices,
    get_balance_checks,
)
from lnbits.core.services import redeem_lnurl_withdraw


deferred_async: List[Callable] = []


def record_async(func: Callable) -> Callable:
    def recorder(state):
        deferred_async.append(func)

    return recorder


def run_deferred_async():
    for func in deferred_async:
        current_app.nursery.start_soon(catch_everything_and_restart, func)


async def catch_everything_and_restart(func):
    try:
        limit = trio.CapacityLimiter(40)
        async with limit:
        # We are holding a token!
            await func()
    except trio.Cancelled:
        raise  # because we must pass this up
    except Exception as exc:
        print("caught exception in background task:", exc)
        print(traceback.format_exc())
        print("will restart the task in 5 seconds.")
        await trio.sleep(5)
        await catch_everything_and_restart(func)


async def send_push_promise(a, b) -> None:
    pass


invoice_listeners: List[trio.MemorySendChannel] = []


def register_invoice_listener(send_chan: trio.MemorySendChannel):
    """
    A method intended for extensions to call when they want to be notified about
    new invoice payments incoming.
    """
    invoice_listeners.append(send_chan)


async def webhook_handler():
    handler = getattr(WALLET, "webhook_listener", None)
    if handler:
        return await handler()
    return "", HTTPStatus.NO_CONTENT


internal_invoice_paid, internal_invoice_received = trio.open_memory_channel(0)


async def internal_invoice_listener():
    async for checking_id in internal_invoice_received:
        current_app.nursery.start_soon(invoice_callback_dispatcher, checking_id)


async def invoice_listener():
    async for checking_id in WALLET.paid_invoices_stream():
        print("> got a payment notification", checking_id)
        current_app.nursery.start_soon(invoice_callback_dispatcher, checking_id)


async def check_pending_payments():
    await delete_expired_invoices()

    outgoing = True
    incoming = True

    while True:
        for payment in await get_payments(
            since=(int(time.time()) - 60 * 60 * 24 * 15),  # 15 days ago
            complete=False,
            pending=True,
            outgoing=outgoing,
            incoming=incoming,
            exclude_uncheckable=True,
        ):
            await payment.check_pending()

        # after the first check we will only check outgoing, not incoming
        # that will be handled by the global invoice listeners, hopefully
        incoming = False

        await trio.sleep(60 * 30)  # every 30 minutes


async def perform_balance_checks():
    while True:
        for bc in await get_balance_checks():
            redeem_lnurl_withdraw(bc.wallet, bc.url)

        await trio.sleep(60 * 60 * 6)  # every 6 hours


async def invoice_callback_dispatcher(checking_id: str):
    payment = await get_standalone_payment(checking_id)
    if payment and payment.is_in:
        await payment.set_pending(False)
        for send_chan in invoice_listeners:
            await send_chan.send(payment)
