"""
Acquirer gateway.

Sits between the merchant-simulator and the issuer-simulator: accepts an
ISO 8583 authorization request over TCP from a merchant, persists it,
translates any PIN block from the sending terminal's ZPK to the owning
issuer's *transit* ZPK -- the zone key for this interchange link, a
different key from the one that issuer uses to protect its own PIN
store at rest (see services/issuer_simulator/app.py and
scripts/generate_data.py's generate_keys()) -- forwards the request on
to the issuer-simulator over a connection borrowed from a
services.common.pool.ConnectionPool (reused across many transactions
rather than opened fresh each time), waits for the (mutated) response,
persists it, and returns it to the merchant. Every stage is reported to
the dashboard as a best-effort event POST so the web UI can show the
live flow; a dashboard outage never blocks the authorization path
itself.

The merchant-simulator pools and reuses its connections to us the same
way (see services/merchant_simulator/app.py), so handle_merchant() below
loops reading further requests off each connection rather than handling
one and closing -- symmetric with how forward_to_issuer() below expects
the issuer-simulator to behave towards *this* process.

PIN translation happens here in plain Python because this is a software
simulation harness -- in a production acquiring host this step runs
inside an HSM, which never releases the clear PIN block outside its own
boundary. See services/common/crypto.py and README.md for more on this.

CONCURRENCY_LIMIT bounds how many transactions this gateway will process
at once (from accepting the connection through to sending the response).
Without it, the merchant-simulator's fire-and-forget sends have no cap on
how many can be in flight simultaneously; under sustained high offered
load, a small rise in per-transaction latency lets more and more pile up
before any of them finish, which drives latency up further -- a feedback
loop that eventually exhausts the DB connection pool no matter how large
it is (found the hard way: raising the pool from 80 to 200 connections
just delayed the same collapse by half an hour instead of preventing it).
The semaphore below is the actual fix: once CONCURRENCY_LIMIT
transactions are being processed, a new one simply waits for a slot
-- consuming nothing from the DB pool until then -- so offered load
self-limits to what the gateway can actually sustain instead of growing
without bound. This also means one gateway replica can never demand more
DB connections than CONCURRENCY_LIMIT at once, so podman-compose.yml
sizes services/gateway/db.py's pool, and Postgres's own max_connections,
against CONCURRENCY_LIMIT times the number of gateway replicas.

This gateway is designed to run as multiple replicas (see
podman-compose.yml's gateway-1/gateway-2) for horizontal scaling: nothing
here is stateful across a single transaction's handling, so any number of
identical gateway processes can run side by side, each independently
polling/serving/persisting, with the merchant-simulator spreading its
connections across all of them (ISSUER_HOSTS below is the same idea
applied to scaling the issuer-simulator tier instead).
"""
import asyncio
import json
import os
import random
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from services.common import iso8583, reference
from services.common.crypto import key_from_hex, translate_pin_block
from services.common.pool import ConnectionPool
from services.common.util import load_json, parse_host_list, serve_until_signal, setup_logging, wait_for_files
from services.gateway import db

log = setup_logging("gateway")

SEED_DIR = os.environ.get("SEED_DIR", "/data")
KEYS_PATH = os.path.join(SEED_DIR, "keys", "keys.json")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8583"))
# One or more "host:port" issuer-simulator replicas, comma-separated (e.g.
# "issuer-simulator-1:8584,issuer-simulator-2:8584") -- falls back to the
# single ISSUER_HOST/ISSUER_PORT pair when only one issuer is running.
ISSUER_HOST = os.environ.get("ISSUER_HOST", "issuer-simulator")
ISSUER_PORT = int(os.environ.get("ISSUER_PORT", "8584"))
ISSUER_HOSTS = parse_host_list(os.environ.get("ISSUER_HOSTS", f"{ISSUER_HOST}:{ISSUER_PORT}"), ISSUER_PORT)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")

# Diagnostic-only: dump the first N raw ISO 8583 messages crossing the
# acquirer<->issuer interchange link (both directions -- the gateway is
# the one place that sees both) to a JSONL file. Off by default (limit 0);
# set ISO8583_DUMP_LIMIT to enable for a one-off capture.
ISO8583_DUMP_PATH = os.environ.get("ISO8583_DUMP_PATH", "/tmp/iso8583_dump.jsonl")
ISO8583_DUMP_LIMIT = int(os.environ.get("ISO8583_DUMP_LIMIT", "0"))

CONCURRENCY_LIMIT = int(os.environ.get("CONCURRENCY_LIMIT", "150"))

TERMINAL_KEYS = {}
ISSUER_TRANSIT_KEYS = {}
# report_event() below can have up to CONCURRENCY_LIMIT transactions each
# posting to the dashboard around the same time, but httpx's own default
# connection-pool limits (max_keepalive_connections=20) are far smaller
# than that -- found by measurement, not guesswork: under load this
# client showed 20 ESTABLISHED against 6062 TIME_WAIT to the dashboard,
# the 20 an exact match for that default. Requests beyond the keepalive
# cap still succeed, but their connection gets closed rather than kept
# alive, so a fresh handshake (and a spent one cycling through TIME_WAIT)
# happens next time instead of a reuse -- the same problem the pooling
# above solves for the ISO 8583 hops, just inside httpx's own pool
# instead of a hand-rolled one. Sizing both limits to CONCURRENCY_LIMIT
# (with headroom on the hard cap) lets every concurrent transaction's
# event posts actually get reused.
_http = httpx.AsyncClient(
    timeout=2.0,
    limits=httpx.Limits(max_connections=CONCURRENCY_LIMIT * 2, max_keepalive_connections=CONCURRENCY_LIMIT),
)
_dump_count = 0
_dump_lock = asyncio.Lock()
_concurrency = asyncio.Semaphore(CONCURRENCY_LIMIT)


async def report_event(stage: str, **fields):
    payload = {"stage": stage, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        await _http.post(f"{DASHBOARD_URL}/events", json=payload)
    except Exception as exc:  # dashboard is observability-only, never fatal
        log.debug("event post failed (%s): %s", stage, exc)


def now():
    return datetime.now(timezone.utc)


async def dump_message(direction: str, mti: str, values: dict):
    global _dump_count
    if ISO8583_DUMP_LIMIT <= 0:
        return
    async with _dump_lock:
        if _dump_count >= ISO8583_DUMP_LIMIT:
            return
        _dump_count += 1
        seq = _dump_count
    fields = {k: (v.hex() if isinstance(v, (bytes, bytearray)) else v) for k, v in values.items()}
    entry = {
        "seq": seq,
        "direction": direction,  # "acquirer_to_issuer" or "issuer_to_acquirer"
        "mti": mti,
        "raw_hex": iso8583.pack(mti, values).hex(),
        "fields": fields,
    }
    with open(ISO8583_DUMP_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    if seq >= ISO8583_DUMP_LIMIT:
        log.info("ISO8583 dump complete: %d messages written to %s", seq, ISO8583_DUMP_PATH)


# One ConnectionPool per issuer-simulator replica (see ISSUER_HOSTS
# above); forward_to_issuer() below picks which replica's pool to use
# per call, which is what actually spreads load across multiple issuer
# replicas. Each pool is sized to match CONCURRENCY_LIMIT: since that
# semaphore already caps how many transactions can simultaneously need
# an issuer connection, no single pool should ever have to make a
# caller wait for one -- its job is reuse, not a second independent
# throttle stacked on top of the first (which would just add another
# Little's-Law latency multiplier for no benefit).
_issuer_pools = [ConnectionPool(host, port, max_size=CONCURRENCY_LIMIT) for host, port in ISSUER_HOSTS]


async def forward_to_issuer(mti: str, values: dict):
    await dump_message("acquirer_to_issuer", mti, values)
    # Picking a random issuer replica per transaction (rather than sticky
    # per-terminal or round-robin) needs no shared state and spreads load
    # evenly over enough transactions; a replica that's down simply fails
    # its connect/read below and this transaction gets the same "91 issuer
    # unreachable" handling as a single-issuer outage would.
    pool = random.choice(_issuer_pools)
    reader, writer = await pool.acquire()
    healthy = True
    try:
        await iso8583.write_message(writer, mti, values)
        resp_mti, resp = await iso8583.read_message(reader)
        await dump_message("issuer_to_acquirer", resp_mti, resp)
        return resp_mti, resp
    except Exception:
        healthy = False
        raise
    finally:
        await pool.release(reader, writer, healthy)


async def handle_merchant(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """The merchant-simulator pools and reuses its connections to us (see
    services/merchant_simulator/app.py's ConnectionPool) rather than
    opening a fresh one per transaction, so one connection here now
    carries many transactions over its lifetime -- loop reading them
    until the merchant closes it (pool teardown, or merchant shutdown),
    instead of handling exactly one and closing.

    Also guarantees the merchant always gets *some* response and the
    connection is always eventually cleaned up, even if something
    inside _handle_merchant blows up (a DB hiccup, the issuer being
    unreachable in a way the inner handler didn't anticipate, etc) --
    a bug in one transaction's handling must never look like a dropped
    connection to the merchant, and must never take down the gateway's
    event loop. It also guarantees a "completed" event always follows a
    "received" one, even on failure, so the dashboard's outstanding-auth
    count never drifts from reality."""
    try:
        while True:
            try:
                mti, req = await iso8583.read_message(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            txn_id = req.get("transaction_id")
            received_at = now()
            extra = req.get("extra_json", {})

            # Everything from here on (DB writes, PIN translation, the
            # forward to the issuer and its response) counts against
            # CONCURRENCY_LIMIT -- see the module docstring. A transaction
            # waiting here for a free slot isn't touching the DB pool at
            # all yet, so the pool can never see more demand than the
            # gateway is actually admitting.
            async with _concurrency:
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
                    fallback_pos_entry_mode = iso8583.POS_ENTRY_MODE_NAMES.get(
                        req.get("pos_entry_mode"), req.get("pos_entry_mode")
                    )
                    await report_event(
                        "completed", transaction_id=txn_id, merchant_id=req.get("merchant_id"),
                        terminal_id=req.get("terminal_id"), card_network=extra.get("card_network"),
                        amount=str(req.get("amount_minor_units", "0")), currency=extra.get("currency_code_alpha"),
                        response_code="96", response_status="DECLINED",
                        response_desc=reference.RESPONSE_CODE_MAP.get("96", ("Unknown",))[0],
                        duration_ms=duration_ms,
                        auth_type=reference.AUTH_TYPE_LABELS.get(fallback_pos_entry_mode, fallback_pos_entry_mode),
                    )
                    try:
                        await iso8583.write_message(writer, iso8583.MTI_AUTH_RESPONSE, {
                            "transaction_id": txn_id or "", "response_code": "96",
                        })
                    except Exception:
                        # the connection itself is dead -- no point trying
                        # to read a next request off it
                        break
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
    pos_entry_mode = iso8583.POS_ENTRY_MODE_NAMES.get(req.get("pos_entry_mode"), req.get("pos_entry_mode"))
    auth_type = reference.AUTH_TYPE_LABELS.get(pos_entry_mode, pos_entry_mode)

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
        "pos_entry_mode": pos_entry_mode,
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
        issuer_transit_key = ISSUER_TRANSIT_KEYS[issuer_id]
        out_values["pin_block"] = translate_pin_block(req["pin_block"], req["pan"], terminal_key, issuer_transit_key)

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
    response_desc, response_status = reference.RESPONSE_CODE_MAP.get(response_code, ("Unknown", "DECLINED"))
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
        response_desc=response_desc, duration_ms=duration_ms, auth_type=auth_type,
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
    ISSUER_TRANSIT_KEYS.update({iid: key_from_hex(k) for iid, k in keys["issuers_transit"].items()})

    await db.init_db()

    server = await asyncio.start_server(handle_merchant, GATEWAY_HOST, GATEWAY_PORT)
    log.info(
        "gateway listening on %s:%s, concurrency limit %d, %d issuer replica(s)",
        GATEWAY_HOST, GATEWAY_PORT, CONCURRENCY_LIMIT, len(ISSUER_HOSTS),
    )
    await serve_until_signal(server, log)


if __name__ == "__main__":
    asyncio.run(main())
