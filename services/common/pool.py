"""
Bounded pool of persistent TCP connections to one downstream host.

Shared by every hop in this system that talks over a pooled TCP link
instead of paying a fresh handshake per message: the gateway's
connections to the issuer-simulator, and the merchant-simulator's
connections to the gateway (see services/gateway/app.py and
services/merchant_simulator/app.py). One instance covers exactly one
downstream host:port; a caller with more than one replica to spread
load across keeps one instance per replica and picks between them
itself (both callers do this with `random.choice`).

A connection is only ever mid-flight for one request at a time (no
pipelining/multiplexing): acquire it, do one full write+read round
trip, then release it. This only pays off if the *receiving* side
loops reading further messages off the same connection instead of
closing after one -- see the gateway's handle_merchant() and the
issuer-simulator's handle_gateway(), both of which do this specifically
so their callers' pools here actually get reuse.
"""
import asyncio


class ConnectionPool:
    def __init__(self, host: str, port: int, max_size: int):
        self._host = host
        self._port = port
        self._max_size = max_size
        self._idle: asyncio.Queue = asyncio.Queue()
        self._created = 0
        self._create_lock = asyncio.Lock()

    async def _connect(self):
        return await asyncio.open_connection(self._host, self._port)

    async def acquire(self):
        try:
            return self._idle.get_nowait()
        except asyncio.QueueEmpty:
            pass
        async with self._create_lock:
            if self._created < self._max_size:
                self._created += 1
                try:
                    return await self._connect()
                except Exception:
                    self._created -= 1
                    raise
        # at capacity -- wait for one to be returned rather than opening
        # an unbounded number of extra connections to the downstream host
        return await self._idle.get()

    async def release(self, reader, writer, healthy: bool):
        if healthy and not writer.is_closing():
            await self._idle.put((reader, writer))
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        async with self._create_lock:
            self._created -= 1
