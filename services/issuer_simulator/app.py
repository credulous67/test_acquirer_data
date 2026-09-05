"""
Issuer simulator.

A single process stands in for every simulated card issuer (one per card
network -- see services/common/reference.issuer_id_for_network). For each
authorization request forwarded by the gateway it: verifies the PIN when
one was presented (by decrypting both the incoming, gateway-translated
PIN block and the card's own at-rest PIN block with that issuer's ZPK and
comparing the recovered digit strings -- a "local PIN check", one of the
two verification styles real issuers use, the other being PVV-based
verification which needs a PIN Verification Key this project doesn't
model), checks the card hasn't expired, and otherwise picks a response
code from the same weighted distribution the old static generator used.
"""
import asyncio
import os
import random
from datetime import datetime, timezone

from services.common import iso8583, reference
from services.common.crypto import iso4_decode_pin_block, key_from_hex
from services.common.util import load_json, setup_logging, wait_for_files

log = setup_logging("issuer-simulator")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
CARDS_PATH = os.path.join(SEED_DIR, "seed", "cards.json")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

ISSUER_HOST = os.environ.get("ISSUER_HOST", "0.0.0.0")
ISSUER_PORT = int(os.environ.get("ISSUER_PORT", "8584"))

CARDS_BY_PAN = {}
ISSUER_KEYS = {}


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
        issuer_key = ISSUER_KEYS[card["issuer_id"]]
        try:
            entered_pin = iso4_decode_pin_block(req["pin_block"], pan, issuer_key)
            real_pin = iso4_decode_pin_block(bytes.fromhex(card["pin_block_at_rest_hex"]), pan, issuer_key)
        except (ValueError, KeyError):
            return "55", None
        if entered_pin != real_pin:
            return "55", None

    code, _desc, status = random.choices(
        reference.GENERIC_RESPONSE_CODES, weights=reference.GENERIC_RESPONSE_WEIGHTS, k=1
    )[0]
    if status == "APPROVED":
        auth_code = f"{random.randint(0, 999999):06d}"
        return code, auth_code
    return code, None


async def handle_gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        mti, req = await iso8583.read_message(reader)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        writer.close()
        return

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
        pass
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
    ISSUER_KEYS.update({iid: key_from_hex(k) for iid, k in keys["issuers"].items()})
    log.info("loaded %d cards across %d issuers", len(CARDS_BY_PAN), len(ISSUER_KEYS))

    server = await asyncio.start_server(handle_gateway, ISSUER_HOST, ISSUER_PORT)
    log.info("issuer-simulator listening on %s:%s", ISSUER_HOST, ISSUER_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
