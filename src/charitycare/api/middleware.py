"""Rate limiting and privacy-preserving access logging (design sections 2b and 11).

``AccessLogMiddleware`` logs a fixed allowlist of keys per request: method,
route *template* (``/v1/facilities/{facility_id}``, never the raw path or
query string), status, duration and whether the request was rate limited.
Request and response bodies are never read or logged.

``RateLimitMiddleware`` is a per-client sliding one-minute window applied to
``/v1/*``: 60 requests/minute anonymous, a higher limit for ``X-API-Key``
holders. In-memory, single process; a multi-worker deployment would move the
counters to the shared cache.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from charitycare.api.settings import ApiSettings

access_log = logging.getLogger("charitycare.api.access")

LOG_ALLOWLIST: tuple[str, ...] = ("method", "route", "status", "duration_ms", "rate_limited", "keyed")


def route_template(request: Request) -> str:
    """The matched route's path template (set in the scope by the router), so ids in the URL
    are logged only as placeholders. Call after the request was handled."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return "<static>" if request.url.path.startswith("/assets/") else "<unmatched>"


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        record = {
            "method": request.method,
            "route": route_template(request),
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "rate_limited": response.status_code == 429,
            "keyed": bool(request.headers.get("x-api-key")),
        }
        assert set(record) <= set(LOG_ALLOWLIST)
        access_log.info("request", extra={"ccc": record})
        return response


class SlidingWindow:
    """Thread-safe per-key sliding window counter."""

    def __init__(self, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> tuple[bool, int, float]:
        """Record a hit; returns (allowed, remaining, seconds until a slot frees)."""
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= limit:
                return False, 0, max(0.0, self.window_s - (now - q[0]))
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory: drop idle keys
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]:
                    del self._hits[k]
            return True, limit - len(q), 0.0


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: ApiSettings, window: SlidingWindow | None = None, prefix: str = "/v1"):
        super().__init__(app)
        self.settings = settings
        self.window = window or SlidingWindow()
        self.prefix = prefix

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith(self.prefix):
            return await call_next(request)
        key = request.headers.get("x-api-key")
        if key and key in self.settings.api_keys:
            bucket, limit = f"key:{key}", self.settings.keyed_rate_limit_per_min
        else:
            client = request.client.host if request.client else "unknown"
            bucket, limit = f"ip:{client}", self.settings.rate_limit_per_min
        ok, remaining, retry = self.window.hit(bucket, limit)
        headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining)}
        if not ok:
            headers["Retry-After"] = str(max(1, int(retry + 0.999)))
            return JSONResponse({"detail": "rate limit exceeded", "limit_per_minute": limit}, status_code=429, headers=headers)
        response = await call_next(request)
        response.headers.update(headers)
        return response
