"""Cliente de diagnóstico: ni un método heredado puede emitir escrituras."""

from __future__ import annotations

from src.clients.kalshi_rest import KalshiRestClient


class ReadOnlyKalshiRestClient(KalshiRestClient):
    _PATHS = frozenset(
        {
            "/portfolio/balance",
            "/portfolio/positions",
            "/portfolio/orders",
            "/portfolio/fills",
            "/portfolio/settlements",
        }
    )

    async def _request(self, method, path, **kwargs):
        # El guard corre ANTES del cliente HTTP, firma, retries o throttle de red.
        if method != "GET" or path not in self._PATHS:
            raise PermissionError("El diagnóstico solo admite consultas GET de cartera.")
        return await super()._request(method, path, **kwargs)
