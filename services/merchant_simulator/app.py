"""
Merchant simulator.

Simulates every merchant/terminal in the seed data from a single process:
one asyncio task per merchant, each started after its own random jitter
so traffic doesn't begin in lock-step, then looping forever, drawing a
request template from that merchant's own pool in data/seed/authorizations.json,
freshening its identifiers/timestamp, occasionally attaching a PIN block
(built fresh, enciphered under that terminal's own ZPK) or a CVV2 (for
card-not-present templates), and sending it to the gateway over a new
TCP connection per transaction.

Overall traffic rate is TARGET_TPS, split across merchants in proportion
to how many template transactions they have (busier merchants in the
seed data stay busier live), and is controlled at runtime by polling the
dashboard's pause/resume/rate-multiplier state.
"""
import asyncio
import os
import random
import signal
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from services.common import iso8583
from services.common.control import ControlPoller
from services.common.crypto import iso4_encode_pin_block, key_from_hex
from services.common.util import load_json, setup_logging, wait_for_files

log = setup_logging("merchant-simulator")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
MERCHANTS_PATH = os.path.join(SEED_DIR, "seed", "merchants.json")
TERMINALS_PATH = os.path.join(SEED_DIR, "seed", "terminals.json")
AUTHS_PATH = os.path.join(SEED_DIR, "seed", "authorizations.json")
CUSTOMER_PINS_PATH = os.path.join(SEED_DIR, "seed", "customer_pins.json")
CUSTOMER_CVVS_PATH = os.path.join(SEED_DIR, "seed", "customer_cvvs.json")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "gateway")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8583"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")
TARGET_TPS = float(os.environ.get("TARGET_TPS", "20"))

PIN_TYPO_PROBABILITY = 0.03
CVV_TYPO_PROBABILITY = 0.03

_http = httpx.AsyncClient(timeout=2.0)
_control = ControlPoller(_http, DASHBOARD_URL)
_stop_event = asyncio.Event()  # set on SIGTERM/SIGINT: stop *originating* new sends


def maybe_typo(value: str, probability: float) -> str:
    if random.random() >= probability:
        return value
    pos = random.randrange(len(value))
    digits = list(value)
    digits[pos] = str((int(digits[pos]) + random.randint(1, 9)) % 10)
    return "".join(digits)


def build_live_message(template: dict, terminal_key, customer_pins, customer_cvvs) -> dict:
    now = datetime.now(timezone.utc)
    amount_minor = round(float(template["amount"]) * 100)
    exp_mm, exp_yy = template["expiry_date"].split("/")

    values = {
        "pan": template["pan"],
        "processing_code": iso8583.PROCESSING_CODES.get(template["transaction_type"], "000000"),
        "amount_minor_units": f"{amount_minor:012d}",
        "transmission_datetime": now.strftime("%m%d%H%M%S"),
        "stan": f"{random.randint(0, 999999):06d}",
        "expiry_date": exp_yy + exp_mm,  # ISO 8583 field 14 is YYMM
        "mcc": template["mcc"],
        "pos_entry_mode": iso8583.POS_ENTRY_MODE_CODES.get(template["pos_entry_mode"], "01"),
        "acquirer_id": template["acquirer_id"],
        "retrieval_reference_number": "".join(str(random.randint(0, 9)) for _ in range(12)),
        "terminal_id": template["terminal_id"],
        "merchant_id": template["merchant_id"],
        "currency_code_numeric": template["currency_code_numeric"],
        "transaction_id": str(uuid.uuid4()),
        "extra_json": {
            "card_network": template["card_network"],
            "transaction_type": template["transaction_type"],
            "currency_code_alpha": template["currency_code_alpha"],
            "pin_present": template["pin_present"],
            "cvv_present": template.get("cvv_present", False),
        },
    }

    if template["pin_present"]:
        real_pin = customer_pins.get(template["pan"])
        if real_pin:
            entered_pin = maybe_typo(real_pin, PIN_TYPO_PROBABILITY)
            values["pin_block"] = iso4_encode_pin_block(entered_pin, template["pan"], terminal_key)

    if template.get("cvv_present"):
        real_cvv = customer_cvvs.get(template["pan"])
        if real_cvv:
            values["extra_json"]["cvv"] = maybe_typo(real_cvv, CVV_TYPO_PROBABILITY)

    return values


async def send_transaction(values: dict):
    reader, writer = await asyncio.open_connection(GATEWAY_HOST, GATEWAY_PORT)
    try:
        await iso8583.write_message(writer, iso8583.MTI_AUTH_REQUEST, values)
        await iso8583.read_message(reader)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


_BACKGROUND_TASKS: set = set()


async def send_one(merchant_id: str, template: dict, terminal_key, customer_pins: dict, customer_cvvs: dict):
    """Runs as its own background task, independent of the merchant's
    send-pacing loop below -- if the gateway/issuer are slow to respond
    (e.g. the issuer is paused, see services/dashboard/app.py), this
    transaction just sits waiting for its response without blocking the
    merchant from originating further ones, which is what lets
    outstanding authorizations actually pile up rather than capping out
    at one per merchant."""
    try:
        await asyncio.sleep(random.uniform(0.01, 0.1))  # terminal processing time
        values = build_live_message(template, terminal_key, customer_pins, customer_cvvs)
        await send_transaction(values)
    except Exception as exc:
        # a single bad/dropped/stuck transaction must never take down
        # this merchant's task, let alone the whole asyncio.gather() below
        log.warning("dropping one transaction for %s: %s", merchant_id, exc)


async def _sleep_or_stop(seconds: float) -> bool:
    """Sleeps up to `seconds`, but returns early (True) the moment shutdown
    is signalled, so a shutdown doesn't have to wait out a merchant's full
    next-send delay -- some of these can be many seconds at low TPS."""
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def run_merchant(merchant_id: str, pool: list, lam: float, terminal_keys: dict, customer_pins: dict, customer_cvvs: dict):
    if await _sleep_or_stop(random.uniform(0, 10)):
        return
    log.info("merchant %s starting, base rate %.3f tx/s over %d templates", merchant_id, lam, len(pool))
    while not _stop_event.is_set():
        control = await _control.get()
        if control.get("merchant_paused"):
            if await _sleep_or_stop(1.0):
                break
            continue

        effective_lam = max(lam * control.get("rate_multiplier", 1.0), 0.001)
        if await _sleep_or_stop(random.expovariate(effective_lam)):
            break

        template = random.choice(pool)
        terminal_key = terminal_keys.get(template["terminal_id"])
        if terminal_key is None:
            continue

        # asyncio only holds a *weak* reference to a task -- one that
        # isn't referenced anywhere else can be garbage-collected mid-flight,
        # which silently cancels it and (via send_transaction's finally)
        # closes its gateway connection early. That looked exactly like a
        # burst of dropped/failed transactions under normal load, not just
        # under a paused issuer, until traced to this. Keeping a strong
        # reference in _BACKGROUND_TASKS (discarded once the task finishes)
        # is the standard fix.
        task = asyncio.create_task(send_one(merchant_id, template, terminal_key, customer_pins, customer_cvvs))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


async def main():
    await wait_for_files([MERCHANTS_PATH, TERMINALS_PATH, AUTHS_PATH, CUSTOMER_PINS_PATH, CUSTOMER_CVVS_PATH, KEYS_PATH])

    merchants = load_json(MERCHANTS_PATH)
    auths = load_json(AUTHS_PATH)
    customer_pins = load_json(CUSTOMER_PINS_PATH)
    customer_cvvs = load_json(CUSTOMER_CVVS_PATH)
    keys = load_json(KEYS_PATH)
    terminal_keys = {tid: key_from_hex(k) for tid, k in keys["terminals"].items()}

    pools = defaultdict(list)
    for a in auths:
        pools[a["merchant_id"]].append(a)

    total = sum(len(p) for p in pools.values()) or 1
    merchant_ids = []
    tasks = []
    for m in merchants:
        pool = pools.get(m["merchant_id"])
        if not pool:
            continue
        lam = TARGET_TPS * (len(pool) / total)
        merchant_ids.append(m["merchant_id"])
        tasks.append(run_merchant(m["merchant_id"], pool, lam, terminal_keys, customer_pins, customer_cvvs))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop_event.set)

    log.info("simulating %d merchants, target %.1f tx/s combined", len(tasks), TARGET_TPS)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for merchant_id, result in zip(merchant_ids, results):
        if isinstance(result, Exception):
            log.error("merchant task for %s died: %s", merchant_id, result)

    if _stop_event.is_set() and _BACKGROUND_TASKS:
        log.info("no longer originating new authorizations, waiting on %d in-flight one(s)...",
                  len(_BACKGROUND_TASKS))
        _done, pending = await asyncio.wait(list(_BACKGROUND_TASKS), timeout=25)
        if pending:
            log.warning("%d in-flight transaction(s) still running after 25s, exiting anyway", len(pending))
    log.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
