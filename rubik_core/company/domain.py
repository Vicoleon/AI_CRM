"""Company email domains from the active workspace's control rows.

The domain used for sign-in and internal-vs-external checks is platform-owned
data. Workspace admins cannot change it through their tenant settings. Reads
are cached briefly by workspace slug so one company's domain cannot answer
another company's request; control-domain writes explicitly clear the cache.
"""

from __future__ import annotations

import threading
import time

from rubik_core.tenancy.context import current_workspace

CACHE_TTL_SECONDS = 60.0
_CACHE: dict[str, tuple[float, tuple[str, ...], str]] = {}
_LOCK = threading.Lock()
_GLOBAL_EPOCH = 0
_SLUG_EPOCH: dict[str, int] = {}


def _version(slug: str) -> tuple[int, int]:
    return _GLOBAL_EPOCH, _SLUG_EPOCH.get(slug, 0)


def fold_domain(raw: object) -> str:
    """Return a bare lowercase domain, or an empty string for invalid input."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower().lstrip("@").strip()


def clear_cache(slug: str | None = None) -> None:
    """Invalidate one workspace or the whole process cache."""
    global _GLOBAL_EPOCH
    with _LOCK:
        if slug is None:
            _GLOBAL_EPOCH += 1
            _CACHE.clear()
            _SLUG_EPOCH.clear()
        else:
            _SLUG_EPOCH[slug] = _SLUG_EPOCH.get(slug, 0) + 1
            _CACHE.pop(slug, None)


def _domains(slug: str) -> tuple[tuple[str, ...], str]:
    from rubik_core.control.repository import domains_for_workspace

    for _ in range(3):
        now = time.monotonic()
        with _LOCK:
            hit = _CACHE.get(slug)
            if hit is not None and hit[0] > now:
                return hit[1], hit[2]
            version = _version(slug)
        rows = domains_for_workspace(slug)
        domains = tuple(fold_domain(domain) for domain, _ in rows)
        primary = next((fold_domain(domain) for domain, main in rows if main), "")
        with _LOCK:
            if _version(slug) == version:
                _CACHE[slug] = (time.monotonic() + CACHE_TTL_SECONDS, domains, primary)
                return domains, primary
    raise RuntimeError("Workspace domains changed during lookup")


def workspace_domains() -> frozenset[str]:
    """All verified domains assigned to the current active workspace."""
    domains, _ = _domains(current_workspace())
    return frozenset(domains)


def workspace_domain() -> str:
    """The current workspace's primary domain, or empty if none is saved."""
    _, primary = _domains(current_workspace())
    return primary
