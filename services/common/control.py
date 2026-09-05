"""Shared client for polling the dashboard's simulation control state
(merchant/issuer pause, rate multiplier). Both the merchant-simulator and
the issuer-simulator need this; caching avoids hammering the dashboard
with a request before every single transaction."""
import asyncio

DEFAULT_STATE = {"merchant_paused": False, "issuer_paused": False, "rate_multiplier": 1.0}


class ControlPoller:
    def __init__(self, http_client, dashboard_url: str, cache_seconds: float = 2.0):
        self._http = http_client
        self._dashboard_url = dashboard_url
        self._cache_seconds = cache_seconds
        self._state = dict(DEFAULT_STATE)
        self._cached_at = 0.0

    async def get(self) -> dict:
        loop = asyncio.get_event_loop()
        if loop.time() - self._cached_at < self._cache_seconds:
            return self._state
        try:
            resp = await self._http.get(f"{self._dashboard_url}/api/control")
            self._state.update(resp.json())
        except Exception:
            pass  # keep serving the last known state; the dashboard is control-plane only
        self._cached_at = loop.time()
        return self._state
