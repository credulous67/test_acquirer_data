"""
Dashboard: the only place simulation control state (merchant/issuer
pause, rate multiplier) lives, the ingestion point for lifecycle events
the gateway reports (it's the one component that actually sees every
hop, so it's the sole telemetry source), and the realtime web UI over
both.

Merchant and issuer pause are independent: pausing the merchant stops
new transactions being *originated*; pausing the issuer leaves the
gateway accepting and forwarding requests as normal, but the
issuer-simulator holds each connection open without responding. That
models an issuer that's up but unresponsive at the message level, not a
network-level outage -- so it deliberately does not trigger the
acquirer's Stand-In Processing (STIP) path a real "issuer unreachable"
condition would. The effect is authorizations piling up at the gateway
with a received/forwarded timestamp but no response, which is exactly
what pausing the issuer is for.
"""
import asyncio
import os
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def loop():
        while True:
            await asyncio.sleep(1.0)
            await broadcast(current_stats())
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

RECENT_EVENTS = deque(maxlen=500)
COMPLETED_TIMESTAMPS = deque(maxlen=5000)  # for a rolling TPS window
APPROVED_TIMESTAMPS = deque(maxlen=5000)  # same, split by outcome -- for the stacked chart
DECLINED_TIMESTAMPS = deque(maxlen=5000)
DURATIONS_MS = deque(maxlen=5000)  # end-to-end (received -> completed) latency
COUNTS = {"approved": 0, "declined": 0}
OUTSTANDING = 0  # received but not yet completed -- what "piles up" when the issuer is paused

CONTROL = {"merchant_paused": False, "issuer_paused": False, "rate_multiplier": 1.0}

_clients: set[WebSocket] = set()


class RateBody(BaseModel):
    multiplier: float


def current_stats():
    now = time.time()
    window = 5.0
    recent = [t for t in COMPLETED_TIMESTAMPS if now - t <= window]
    tps = len(recent) / window
    window_approved = len([t for t in APPROVED_TIMESTAMPS if now - t <= window])
    window_declined = len([t for t in DECLINED_TIMESTAMPS if now - t <= window])
    tps_approved = window_approved / window
    tps_declined = window_declined / window
    window_total = window_approved + window_declined
    # windowed (last 5s) approve/decline split, for the stacked-to-100% chart --
    # deliberately not the same as approve_pct/decline_pct below, which are
    # lifetime cumulative and would look like a flat, barely-moving line
    window_approve_pct = (window_approved / window_total * 100) if window_total else 0.0
    window_decline_pct = (window_declined / window_total * 100) if window_total else 0.0
    total = COUNTS["approved"] + COUNTS["declined"]
    approve_pct = (COUNTS["approved"] / total * 100) if total else 0.0
    decline_pct = (COUNTS["declined"] / total * 100) if total else 0.0
    avg_latency_ms = (sum(DURATIONS_MS) / len(DURATIONS_MS)) if DURATIONS_MS else 0.0
    return {
        "type": "stats",
        "tps": round(tps, 2),
        "tps_approved": round(tps_approved, 2),
        "tps_declined": round(tps_declined, 2),
        "window_approve_pct": round(window_approve_pct, 1),
        "window_decline_pct": round(window_decline_pct, 1),
        "approved": COUNTS["approved"],
        "declined": COUNTS["declined"],
        "approve_pct": round(approve_pct, 1),
        "decline_pct": round(decline_pct, 1),
        "outstanding": OUTSTANDING,
        "avg_latency_ms": round(avg_latency_ms, 1),
    }


async def broadcast(message: dict):
    dead = []
    for ws in _clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@app.post("/events")
async def post_event(request: Request):
    global OUTSTANDING
    event = await request.json()
    RECENT_EVENTS.append(event)

    if event.get("stage") == "received":
        OUTSTANDING += 1
    elif event.get("stage") == "completed":
        OUTSTANDING = max(0, OUTSTANDING - 1)
        COMPLETED_TIMESTAMPS.append(time.time())
        if event.get("duration_ms") is not None:
            DURATIONS_MS.append(event["duration_ms"])
        if event.get("response_status") == "APPROVED":
            COUNTS["approved"] += 1
            APPROVED_TIMESTAMPS.append(time.time())
        else:
            COUNTS["declined"] += 1
            DECLINED_TIMESTAMPS.append(time.time())

    await broadcast({"type": "event", **event})
    return {"ok": True}


@app.get("/api/stats")
async def get_stats():
    """Plain REST view of current_stats(), so tooling (e.g.
    scripts/graceful_shutdown.sh) can poll `outstanding` with curl instead
    of needing a WebSocket client."""
    return current_stats()


@app.get("/api/control")
async def get_control():
    return CONTROL


@app.post("/api/control/merchant/pause")
async def pause_merchant():
    CONTROL["merchant_paused"] = True
    return CONTROL


@app.post("/api/control/merchant/resume")
async def resume_merchant():
    CONTROL["merchant_paused"] = False
    return CONTROL


@app.post("/api/control/issuer/pause")
async def pause_issuer():
    CONTROL["issuer_paused"] = True
    return CONTROL


@app.post("/api/control/issuer/resume")
async def resume_issuer():
    CONTROL["issuer_paused"] = False
    return CONTROL


@app.post("/api/control/rate")
async def set_rate(body: RateBody):
    CONTROL["rate_multiplier"] = max(0.1, min(10.0, body.multiplier))
    return CONTROL


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    # current_stats() has its own "type": "stats" key, so it must be
    # spread in *before* the "stats_init" override below, not after --
    # otherwise it silently wins and the client never sees "stats_init".
    await websocket.send_json({**current_stats(), "type": "stats_init", "control": CONTROL})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


app.mount("/static", StaticFiles(directory="services/dashboard/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("services/dashboard/static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
