"""API runtime settings (environment-driven, overridable in tests).

* ``CCC_RATE_LIMIT_PER_MIN``   anonymous limit per client IP (default 60, design section 2b)
* ``CCC_API_KEYS``             comma-separated keys; ``X-API-Key`` holders get ``CCC_KEYED_RATE_LIMIT_PER_MIN`` (600)
* ``CCC_STALE_AFTER_DAYS``     sunset window: responses say ``stale: true`` after this many days without verification (45)
* ``CCC_SUNSET``               ``1`` forces ``stale: true`` (maintenance stopped; section 17 sunset policy)
* ``CCC_CONSOLE_TOKEN``        maintainer token for the review console (console is closed without it
                               unless ``CCC_CONSOLE_OPEN=1`` for local development)
* ``CCC_PUBLIC_BASE_URL``      absolute base used in the Atom feed (default ``http://localhost:8000``)
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class ApiSettings:
    rate_limit_per_min: int = 60
    keyed_rate_limit_per_min: int = 600
    api_keys: frozenset[str] = frozenset()
    stale_after_days: int = 45
    sunset: bool = False
    console_token: str | None = None
    console_open: bool = False
    base_url: str = "http://localhost:8000"
    today: Callable[[], date] = field(default=date.today)

    @classmethod
    def from_env(cls, **overrides: object) -> ApiSettings:
        keys = frozenset(k.strip() for k in os.environ.get("CCC_API_KEYS", "").split(",") if k.strip())
        vals: dict[str, object] = {
            "rate_limit_per_min": _env_int("CCC_RATE_LIMIT_PER_MIN", 60),
            "keyed_rate_limit_per_min": _env_int("CCC_KEYED_RATE_LIMIT_PER_MIN", 600),
            "api_keys": keys,
            "stale_after_days": _env_int("CCC_STALE_AFTER_DAYS", 45),
            "sunset": os.environ.get("CCC_SUNSET", "") in {"1", "true", "yes"},
            "console_token": os.environ.get("CCC_CONSOLE_TOKEN") or None,
            "console_open": os.environ.get("CCC_CONSOLE_OPEN", "") in {"1", "true", "yes"},
            "base_url": os.environ.get("CCC_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        }
        vals.update(overrides)
        return cls(**vals)  # type: ignore[arg-type]
