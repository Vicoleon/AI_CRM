"""A short per-process cache for Insights payloads (contract §3 ``refresh``).

A dashboard is a dozen narrow reads plus Python aggregation, cheap once but
wasteful to repeat every time someone flips between tabs, so a computed
payload stays warm for ten minutes. ``refresh=1`` bypasses (and replaces) the entry.

The key always starts with the VIEWER's email and carries their scope and
whether they may see people: the same dashboard and filters are a different
payload for a Sales manager than for the CEO, and serving one to the other
would leak a department (or a name) they may not see.

Only successes are cached; a failure must not stick for ten minutes. Per
process, so another Cloud Run instance may compute its own copy — harmless,
the numbers are read-only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

from rubik_core.tenancy.context import current_workspace

CACHE_TTL_SECONDS = 600
#: ``refresh=1`` is ignored for an entry younger than this. A refresh is a full
#: rebuild (a dozen queries); without a floor, a held-down Refresh button or a
#: script could keep the database busy rebuilding the same numbers.
MIN_REFRESH_AGE_SECONDS = 30
#: Upper bound on entries, so a long-lived instance cannot grow without limit.
_MAX_ENTRIES = 512

#: key → (expires_at, created_at, value), monotonic seconds.
_CACHE: dict[tuple[str, Hashable], tuple[float, float, Any]] = {}
_LOCK = threading.Lock()


def clear_cache(slug: str | None = None) -> None:
    with _LOCK:
        if slug is None:
            _CACHE.clear()
        else:
            for cache_key in [item for item in _CACHE if item[0] == slug]:
                _CACHE.pop(cache_key, None)


def cached(
    key: Hashable,
    compute: Callable[[], Any],
    *,
    refresh: bool = False,
    ttl: float = CACHE_TTL_SECONDS,
) -> tuple[Any, bool]:
    """``(value, served_from_cache)``. ``compute`` runs on a miss, or on a
    refresh of an entry at least ``MIN_REFRESH_AGE_SECONDS`` old; an exception
    propagates and nothing is stored."""
    cache_key = (current_workspace(), key)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None:
            expires, created, value = hit
            fresh = expires > now
            if fresh and (not refresh or now - created < MIN_REFRESH_AGE_SECONDS):
                return value, True
            if not fresh:
                _CACHE.pop(cache_key, None)
    value = compute()
    with _LOCK:
        if len(_CACHE) >= _MAX_ENTRIES:
            # Drop the entries closest to expiry; simple and good enough here.
            for stale in sorted(_CACHE, key=lambda k: _CACHE[k][0])[
                : _MAX_ENTRIES // 4
            ]:
                _CACHE.pop(stale, None)
        stamp = time.monotonic()
        _CACHE[cache_key] = (stamp + ttl, stamp, value)
    return value, False
