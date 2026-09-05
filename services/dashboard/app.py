"""
Dashboard: the only place simulation control state (paused / rate
multiplier) lives, the ingestion point for lifecycle events the gateway
reports (it's the one component that actually sees every hop, so it's
the sole telemetry source), and the realtime web UI over both.
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
COUNTS = {"approved": 0, "declined": 0}

CONTROL = {"paused": False, "rate_multiplier": 1.0}

_clients: set[WebSocket] = set()


class RateBody(BaseModel):
    multiplier: float


def current_stats():
    now = time.time()
    window = 5.0
    recent = [t for t in COMPLETED_TIMESTAMPS if now - t <= window]
    tps = len(recent) / window
    total = COUNTS["approved"] + COUNTS["declined"]
    approve_pct = (COUNTS["approved"] / total * 100) if total else 0.0
    decline_pct = (COUNTS["declined"] / total * 100) if total else 0.0
    return {
        "type": "stats",
        "tps": round(tps, 2),
        "approved": COUNTS["approved"],
        "declined": COUNTS["declined"],
        "approve_pct": round(approve_pct, 1),
        "decline_pct": round(decline_pct, 1),
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
    event = await request.json()
    RECENT_EVENTS.append(event)

    if event.get("stage") == "completed":
        COMPLETED_TIMESTAMPS.append(time.time())
        if event.get("response_status") == "APPROVED":
            COUNTS["approved"] += 1
        else:
            COUNTS["declined"] += 1

    await broadcast({"type": "event", **event})
    return {"ok": True}


@app.get("/api/control")
async def get_control():
    return CONTROL


@app.post("/api/control/pause")
async def pause():
    CONTROL["paused"] = True
    return CONTROL


@app.post("/api/control/resume")
async def resume():
    CONTROL["paused"] = False
    return CONTROL


@app.post("/api/control/rate")
async def set_rate(body: RateBody):
    CONTROL["rate_multiplier"] = max(0.1, min(10.0, body.multiplier))
    return CONTROL


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    await websocket.send_json({"type": "stats_init", **current_stats(), "control": CONTROL})
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
