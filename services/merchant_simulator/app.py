"""
Merchant simulator.

Simulates every merchant/terminal in the seed data from a single process:
one asyncio task per merchant, each started after its own random jitter
so traffic doesn't begin in lock-step, then looping forever, drawing a
request template from that merchant's own pool in data/seed/authorizations.json,
freshening its identifiers/timestamp, occasionally attaching a PIN block
(built fresh, enciphered under that terminal's own ZPK), and sending it to
the gateway over a new TCP connection per transaction.

Overall traffic rate is TARGET_TPS, split across merchants in proportion
to how many template transactions they have (busier merchants in the
seed data stay busier live), and is controlled at runtime by polling the
dashboard's pause/resume/rate-multiplier state.
"""
import asyncio
import os
import random
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from services.common import iso8583
from services.common.crypto import iso4_encode_pin_block, key_from_hex
from services.common.util import load_json, setup_logging, wait_for_files

log = setup_logging("merchant-simulator")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
MERCHANTS_PATH = os.path.join(SEED_DIR, "seed", "merchants.json")
TERMINALS_PATH = os.path.join(SEED_DIR, "seed", "terminals.json")
AUTHS_PATH = os.path.join(SEED_DIR, "seed", "authorizations.json")
CUSTOMER_PINS_PATH = os.path.join(SEED_DIR, "seed", "customer_pins.json")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "gateway")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8583"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")
TARGET_TPS = float(os.environ.get("TARGET_TPS", "20"))

PIN_TYPO_PROBABILITY = 0.03

_http = httpx.AsyncClient(timeout=2.0)
_control_cache = {"paused": False, "rate_multiplier": 1.0}
_control_cache_at = 0.0


async def get_control_state():
    global _control_cache_at
    loop = asyncio.get_event_loop()
    if loop.time() - _control_cache_at < 2.0:
        return _control_cache
    try:
        resp = await _http.get(f"{DASHBOARD_URL}/api/control")
        _control_cache.update(resp.json())
    except Exception as exc:
        log.debug("control fetch failed, keeping last known state: %s", exc)
    _control_cache_at = loop.time()
    return _control_cache


def maybe_typo(pin: str) -> str:
    if random.random() >= PIN_TYPO_PROBABILITY:
        return pin
    pos = random.randrange(len(pin))
    digits = list(pin)
    digits[pos] = str((int(digits[pos]) + random.randint(1, 9)) % 10)
    return "".join(digits)


def build_live_message(template: dict, terminal_key, customer_pins) -> dict:
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
        },
    }

    if template["pin_present"]:
        real_pin = customer_pins.get(template["pan"])
        if real_pin:
            entered_pin = maybe_typo(real_pin)
            values["pin_block"] = iso4_encode_pin_block(entered_pin, template["pan"], terminal_key)

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


async def run_merchant(merchant_id: str, pool: list, lam: float, terminal_keys: dict, customer_pins: dict):
    await asyncio.sleep(random.uniform(0, 10))
    log.info("merchant %s starting, base rate %.3f tx/s over %d templates", merchant_id, lam, len(pool))
    while True:
        control = await get_control_state()
        if control.get("paused"):
            await asyncio.sleep(1.0)
            continue

        effective_lam = max(lam * control.get("rate_multiplier", 1.0), 0.001)
        await asyncio.sleep(random.expovariate(effective_lam))

        template = random.choice(pool)
        terminal_key = terminal_keys.get(template["terminal_id"])
        if terminal_key is None:
            continue

        await asyncio.sleep(random.uniform(0.01, 0.1))  # terminal processing time
        try:
            values = build_live_message(template, terminal_key, customer_pins)
            await send_transaction(values)
        except Exception as exc:
            # a single bad/dropped transaction must never take down this
            # merchant's task, let alone the whole asyncio.gather() below
            log.warning("dropping one transaction for %s: %s", merchant_id, exc)


async def main():
    await wait_for_files([MERCHANTS_PATH, TERMINALS_PATH, AUTHS_PATH, CUSTOMER_PINS_PATH, KEYS_PATH])

    merchants = load_json(MERCHANTS_PATH)
    auths = load_json(AUTHS_PATH)
    customer_pins = load_json(CUSTOMER_PINS_PATH)
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
        tasks.append(run_merchant(m["merchant_id"], pool, lam, terminal_keys, customer_pins))

    log.info("simulating %d merchants, target %.1f tx/s combined", len(tasks), TARGET_TPS)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for merchant_id, result in zip(merchant_ids, results):
        if isinstance(result, Exception):
            log.error("merchant task for %s died: %s", merchant_id, result)


if __name__ == "__main__":
    asyncio.run(main())
