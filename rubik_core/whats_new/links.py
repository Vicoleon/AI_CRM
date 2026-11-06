"""Links back into the app from Slack (the morning post).

Guarded like ``rubik_core/xray/crm_link.research_link_url``: ``FRONTEND_URL``
defaults to ``http://localhost:5173``, and a process that does not carry the
real value (a Cloud Run Job inherits nothing from the Service) would otherwise
post dead localhost links to a whole channel. No usable base means no link,
never a wrong one: the post goes out without its link.
"""

from __future__ import annotations

from urllib.parse import quote

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def app_base() -> str | None:
    """The public app origin, or None when ``FRONTEND_URL`` isn't one."""
    from rubik_core.config import settings

    base = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return None
    host = base.split("://", 1)[1].split("/", 1)[0]
    host = host.rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":")[0]
    host = host.lower()
    if not host or host in _LOCAL_HOSTS or host.endswith(".local"):
        return None
    return base


def deep_link(entry_id: str, *, src: str | None = None) -> str | None:
    """``<base>/whats-new/<entry id>``, the reader's link to one release note
    (``src="slack"`` marks an arrival from the channel post, so it counts as
    "from Slack"). None when there is no public base to link to."""
    base = app_base()
    if base is None:
        return None
    path = f"/whats-new/{quote(entry_id, safe='')}"
    if src:
        path += f"?src={quote(src, safe='')}"
    return base + path
