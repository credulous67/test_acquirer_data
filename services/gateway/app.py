"""
Acquirer gateway.

Sits between the merchant-simulator and the issuer-simulator: accepts an
ISO 8583 authorization request over TCP from a merchant, persists it,
translates any PIN block from the sending terminal's ZPK to the owning
issuer's ZPK, forwards the request on to the issuer-simulator over its
own TCP connection, waits for the (mutated) response, persists it, and
returns it to the merchant. Every stage is reported to the dashboard as a
best-effort event POST so the web UI can show the live flow; a dashboard
outage never blocks the authorization path itself.

PIN translation happens here in plain Python because this is a software
simulation harness -- in a production acquiring host this step runs
inside an HSM, which never releases the clear PIN block outside its own
boundary. See services/common/crypto.py and README.md for more on this.
"""
import asyncio
import os
import random
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from services.common import iso8583, reference
from services.common.crypto import key_from_hex, translate_pin_block
from services.common.util import load_json, serve_until_signal, setup_logging, wait_for_files
from services.gateway import db

log = setup_logging("gateway")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8583"))
ISSUER_HOST = os.environ.get("ISSUER_HOST", "issuer-simulator")
ISSUER_PORT = int(os.environ.get("ISSUER_PORT", "8584"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")

TERMINAL_KEYS = {}
ISSUER_KEYS = {}
_http = httpx.AsyncClient(timeout=2.0)


async def report_event(stage: str, **fields):
    payload = {"stage": stage, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        await _http.post(f"{DASHBOARD_URL}/events", json=payload)
    except Exception as exc:  # dashboard is observability-only, never fatal
        log.debug("event post failed (%s): %s", stage, exc)


def now():
    return datetime.now(timezone.utc)


async def forward_to_issuer(mti: str, values: dict):
    reader, writer = await asyncio.open_connection(ISSUER_HOST, ISSUER_PORT)
    try:
        await iso8583.write_message(writer, mti, values)
        return await iso8583.read_message(reader)
    finally:
        writer.close()
        await writer.wait_closed()


async def handle_merchant(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Thin wrapper that guarantees the merchant always gets *some*
    response and the connection is always cleaned up, even if something
    inside _handle_merchant blows up (a DB hiccup, the issuer being
    unreachable in a way the inner handler didn't anticipate, etc) --
    a bug in one transaction's handling must never look like a dropped
    connection to the merchant, and must never take down the gateway's
    event loop. It also guarantees a "completed" event always follows a
    "received" one, even on failure, so the dashboard's outstanding-auth
    count never drifts from reality."""
    try:
        mti, req = await iso8583.read_message(reader)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        writer.close()
        return
    txn_id = req.get("transaction_id")
    received_at = now()
    extra = req.get("extra_json", {})

    try:
        await _handle_merchant(mti, req, writer, txn_id, received_at)
    except Exception as exc:
        log.exception("unhandled error processing transaction %s: %s", txn_id, exc)
        completed_at = now()
        duration_ms = round((completed_at - received_at).total_seconds() * 1000)
        try:
            await db.update_response(txn_id, {
                "status": "COMPLETED", "response_code": "96", "response_status": "DECLINED",
                "completed_at": completed_at, "duration_ms": duration_ms,
            })
        except Exception:
            pass
        await report_event(
            "completed", transaction_id=txn_id, merchant_id=req.get("merchant_id"),
            terminal_id=req.get("terminal_id"), card_network=extra.get("card_network"),
            amount=str(req.get("amount_minor_units", "0")), currency=extra.get("currency_code_alpha"),
            response_code="96", response_status="DECLINED", duration_ms=duration_ms,
        )
        try:
            await iso8583.write_message(writer, iso8583.MTI_AUTH_RESPONSE, {
                "transaction_id": txn_id or "", "response_code": "96",
            })
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_merchant(mti: str, req: dict, writer: asyncio.StreamWriter, txn_id: str, received_at):
    extra = req.get("extra_json", {})
    card_network = extra.get("card_network")
    issuer_id = reference.issuer_id_for_network(card_network)
    pin_present = bool(extra.get("pin_present"))

    # simulated gateway ingest/validation processing time
    await asyncio.sleep(random.uniform(0.02, 0.15))

    await db.insert_request({
        "transaction_id": txn_id,
        "merchant_id": req["merchant_id"],
        "terminal_id": req["terminal_id"],
        "pan": req["pan"],
        "expiry_date": req["expiry_date"],
        "card_network": card_network,
        "issuer_id": issuer_id,
        "transaction_type": extra.get("transaction_type"),
        "amount": Decimal(req["amount_minor_units"]) / Decimal(100),
        "currency_code_numeric": req["currency_code_numeric"],
        "currency_code_alpha": extra.get("currency_code_alpha"),
        "mcc": req.get("mcc"),
        "pos_entry_mode": iso8583.POS_ENTRY_MODE_NAMES.get(req.get("pos_entry_mode"), req.get("pos_entry_mode")),
        "pin_present": pin_present,
        "stan": req.get("stan"),
        "retrieval_reference_number": req.get("retrieval_reference_number"),
        "acquirer_id": req.get("acquirer_id"),
        "status": "PENDING",
        "received_at": received_at,
    })
    await report_event(
        "received", transaction_id=txn_id, merchant_id=req["merchant_id"],
        terminal_id=req["terminal_id"], card_network=card_network,
        amount=str(req["amount_minor_units"]), currency=extra.get("currency_code_alpha"),
    )

    out_values = dict(req)
    if pin_present and "pin_block" in req:
        terminal_key = TERMINAL_KEYS[req["terminal_id"]]
        issuer_key = ISSUER_KEYS[issuer_id]
        out_values["pin_block"] = translate_pin_block(req["pin_block"], req["pan"], terminal_key, issuer_key)

    forwarded_at = now()
    await db.update_response(txn_id, {"forwarded_at": forwarded_at})
    await report_event("forwarded_to_issuer", transaction_id=txn_id)

    try:
        _resp_mti, resp = await forward_to_issuer(mti, out_values)
    except (ConnectionRefusedError, OSError) as exc:
        log.warning("issuer unreachable for %s: %s", txn_id, exc)
        resp = {"transaction_id": txn_id, "response_code": "91"}

    response_received_at = now()
    # simulated gateway response-handling processing time
    await asyncio.sleep(random.uniform(0.02, 0.1))

    response_code = resp.get("response_code", "96")
    _desc, response_status = reference.RESPONSE_CODE_MAP.get(response_code, ("Unknown", "DECLINED"))
    auth_code = resp.get("auth_code")

    completed_at = now()
    duration_ms = round((completed_at - received_at).total_seconds() * 1000)
    await db.update_response(txn_id, {
        "status": "COMPLETED",
        "response_code": response_code,
        "response_status": response_status,
        "auth_code": auth_code,
        "response_received_at": response_received_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
    })
    await report_event(
        "completed", transaction_id=txn_id, merchant_id=req["merchant_id"],
        terminal_id=req["terminal_id"], card_network=card_network,
        amount=str(req["amount_minor_units"]), currency=extra.get("currency_code_alpha"),
        response_code=response_code, response_status=response_status,
        duration_ms=duration_ms,
    )

    await iso8583.write_message(writer, iso8583.MTI_AUTH_RESPONSE, {
        "transaction_id": txn_id,
        "response_code": response_code,
        "auth_code": auth_code,
    })


async def main():
    await wait_for_files([KEYS_PATH])
    keys = load_json(KEYS_PATH)
    TERMINAL_KEYS.update({tid: key_from_hex(k) for tid, k in keys["terminals"].items()})
    ISSUER_KEYS.update({iid: key_from_hex(k) for iid, k in keys["issuers"].items()})

    await db.init_db()

    server = await asyncio.start_server(handle_merchant, GATEWAY_HOST, GATEWAY_PORT)
    log.info("gateway listening on %s:%s", GATEWAY_HOST, GATEWAY_PORT)
    await serve_until_signal(server, log)


if __name__ == "__main__":
    asyncio.run(main())
