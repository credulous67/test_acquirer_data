"""
Issuer simulator.

A single process stands in for every simulated card issuer (one per card
network -- see services/common/reference.issuer_id_for_network). For each
authorization request forwarded by the gateway it: verifies the PIN when
one was presented, verifies the CVV2 for card-not-present transactions,
checks the card hasn't expired, and otherwise picks a response code from
the same weighted distribution the old static generator used.

CVV2 verification is a plain string comparison against the card record's
own `cvv` field (never encrypted, unlike the PIN) -- realistic, since CVV
data travels in the clear within the authorization message itself in
real systems too (protected only by the transport, e.g. TLS, which this
project doesn't model), whereas a PIN specifically requires field-level
encryption end-to-end even over an already-encrypted transport. That
asymmetry is itself part of what this project models.

PIN verification decrypts two *different* keys independently rather than
one shared key: the incoming, gateway-translated PIN block under this
issuer's *transit* ZPK (the zone key for the gateway<->issuer link), and
the card's at-rest PIN block under a separate *storage* ZPK (see
scripts/generate_data.py's generate_keys()) -- then compares the two
recovered PIN digit strings. This is a "local PIN check", one of the two
verification styles real issuers use (the other, PVV-based verification,
needs a separate PIN Verification Key this project doesn't model). Using
one key for both would mean the interchange zone and the at-rest vault
were silently the same trust boundary, which they must not be.

Pausing the issuer (via the dashboard) does not refuse or drop
connections -- it holds each one open, past its normal random processing
delay, until resumed. That models an issuer that's up but unresponsive
at the message level rather than a network-level outage, so it
deliberately does not trigger the acquirer's Stand-In Processing (STIP)
path a real "issuer unreachable" condition would; the visible effect is
authorizations piling up at the gateway with no response.
"""
import asyncio
import os
import random
from datetime import datetime, timezone

import httpx

from services.common import iso8583, reference
from services.common.control import ControlPoller
from services.common.crypto import iso4_decode_pin_block, key_from_hex
from services.common.util import load_json, serve_until_signal, setup_logging, wait_for_files

log = setup_logging("issuer-simulator")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
CARDS_PATH = os.path.join(SEED_DIR, "seed", "cards.json")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

ISSUER_HOST = os.environ.get("ISSUER_HOST", "0.0.0.0")
ISSUER_PORT = int(os.environ.get("ISSUER_PORT", "8584"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")

CARDS_BY_PAN = {}
ISSUER_TRANSIT_KEYS = {}
ISSUER_STORAGE_KEYS = {}
# Modest headroom over httpx's own default (max_keepalive_connections=20)
# for the same reason as the gateway's own _http client (see
# services/gateway/app.py) -- this process's control-plane polling is
# cached (see ControlPoller) so its call volume is far lower, but there's
# no reason to leave it exposed to the same failure mode.
_http = httpx.AsyncClient(timeout=2.0, limits=httpx.Limits(max_connections=100, max_keepalive_connections=50))
_control = ControlPoller(_http, DASHBOARD_URL)


def is_expired(expiry_yymm: str) -> bool:
    """expiry_yymm is ISO 8583 field 14 format: YYMM."""
    year = 2000 + int(expiry_yymm[:2])
    month = int(expiry_yymm[2:])
    end_of_month = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= end_of_month


def decide_response(req: dict) -> tuple[str, str | None]:
    pan = req["pan"]
    card = CARDS_BY_PAN.get(pan)
    if card is None:
        return "14", None

    if is_expired(req["expiry_date"]):
        return "54", None

    extra = req.get("extra_json", {})
    if extra.get("pin_present"):
        transit_key = ISSUER_TRANSIT_KEYS[card["issuer_id"]]
        storage_key = ISSUER_STORAGE_KEYS[card["issuer_id"]]
        try:
            entered_pin = iso4_decode_pin_block(req["pin_block"], pan, transit_key)
            real_pin = iso4_decode_pin_block(bytes.fromhex(card["pin_block_at_rest_hex"]), pan, storage_key)
        except (ValueError, KeyError):
            return "55", None
        if entered_pin != real_pin:
            return "55", None

    if extra.get("cvv_present"):
        entered_cvv = extra.get("cvv")
        if not entered_cvv or entered_cvv != card["cvv"]:
            return "82", None

    code, _desc, status = random.choices(
        reference.GENERIC_RESPONSE_CODES, weights=reference.GENERIC_RESPONSE_WEIGHTS, k=1
    )[0]
    if status == "APPROVED":
        auth_code = f"{random.randint(0, 999999):06d}"
        return code, auth_code
    return code, None


async def handle_gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """The gateway pools and reuses its connections to us (see
    services/gateway/app.py's IssuerConnectionPool) rather than opening a
    fresh one per transaction, so one connection here now carries many
    requests over its lifetime -- loop reading them until the gateway
    closes it (pool teardown, or gateway shutdown), instead of handling
    exactly one and closing."""
    try:
        while True:
            try:
                mti, req = await iso8583.read_message(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break

            while (await _control.get()).get("issuer_paused"):
                await asyncio.sleep(1.0)

            # simulated issuer host processing time, with an occasional slow one
            delay = random.uniform(0.1, 0.8)
            if random.random() < 0.05:
                delay += random.uniform(1.0, 2.0)
            await asyncio.sleep(delay)

            response_code, auth_code = decide_response(req)

            try:
                await iso8583.write_message(writer, iso8583.MTI_AUTH_RESPONSE, {
                    "transaction_id": req.get("transaction_id"),
                    "response_code": response_code,
                    "auth_code": auth_code,
                })
            except (ConnectionResetError, BrokenPipeError):
                break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    await wait_for_files([CARDS_PATH, KEYS_PATH])
    cards = load_json(CARDS_PATH)
    CARDS_BY_PAN.update({c["pan"]: c for c in cards})
    keys = load_json(KEYS_PATH)
    ISSUER_TRANSIT_KEYS.update({iid: key_from_hex(k) for iid, k in keys["issuers_transit"].items()})
    ISSUER_STORAGE_KEYS.update({iid: key_from_hex(k) for iid, k in keys["issuers_storage"].items()})
    log.info("loaded %d cards across %d issuers", len(CARDS_BY_PAN), len(ISSUER_TRANSIT_KEYS))

    server = await asyncio.start_server(handle_gateway, ISSUER_HOST, ISSUER_PORT)
    log.info("issuer-simulator listening on %s:%s", ISSUER_HOST, ISSUER_PORT)
    # Note: if issuer_paused is set when a shutdown signal arrives, any
    # in-flight connections are blocked in decide_response()'s pause-wait
    # loop and won't drain within the grace period -- that's expected
    # (they're being held open on purpose) and scripts/graceful_shutdown.sh
    # accounts for it with its own timeout rather than waiting forever.
    await serve_until_signal(server, log)


if __name__ == "__main__":
    asyncio.run(main())
