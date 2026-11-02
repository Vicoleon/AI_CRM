"""Licensed LinkedIn-data API source (Proxycurl-compatible) — optional.

Inert unless ``LINKEDIN_DATA_API_KEY`` is set, which is the normal state: the
feature ships on grounded public-web search. Configure a key when you want the
higher-fidelity version — full position history with dates and the actual text
of recent posts rather than whatever Google happened to index.

Why a licensed API and not a scraper: this is the only way to get structured
LinkedIn data with a contract behind it. Scraping linkedin.com from the app
would violate their terms, break on every markup change, and get the egress IP
blocked — and Cubie is single-tenant, so "blocked" means blocked for the whole
company at once.

The wire shape targeted is Proxycurl's (``/api/v2/linkedin`` for the profile,
``/api/v2/linkedin/person/posts`` for activity) because it is the most widely
cloned; ``LINKEDIN_DATA_API_BASE`` re-points it at any provider that matches.
Responses are handed on as ``structured`` evidence — this module does no
interpretation, so a provider swap cannot change what the persona read says.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from rubik_core.people import config
from rubik_core.people.schemas import RawEvidence
from rubik_core.people.sources.base import PersonQuery

logger = logging.getLogger(__name__)


class LinkedInApiSource:
    """Structured profile + posts from a licensed provider."""

    name = "linkedin_api"

    def fetch(self, query: PersonQuery) -> RawEvidence:
        key = config.linkedin_api_key()
        if not key:
            return RawEvidence(provider=self.name)

        base = config.linkedin_api_base()
        timeout = config.api_timeout_s()
        headers = {"Authorization": f"Bearer {key}"}

        with httpx.Client(timeout=timeout, headers=headers) as client:
            profile_url = self._resolve_profile_url(client, base, query)
            if not profile_url:
                # Not an error — plenty of people have no findable public
                # profile. Returning empty lets the service fall through to
                # grounded search rather than reporting a failure.
                return RawEvidence(provider=self.name)

            profile = self._get_json(
                client,
                f"{base}/api/v2/linkedin",
                {"url": profile_url, "use_cache": "if-present"},
            )
            posts = self._get_json(
                client,
                f"{base}/api/v2/linkedin/person/posts",
                {"linkedin_profile_url": profile_url},
            )

        structured: dict[str, Any] = {}
        if profile:
            structured["profile"] = profile
        if posts:
            structured["posts"] = posts
        if not structured:
            return RawEvidence(provider=self.name)

        structured["profile_url"] = profile_url
        return RawEvidence(
            citations=[profile_url],
            provider=self.name,
            structured=structured,
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _resolve_profile_url(
        self, client: httpx.Client, base: str, query: PersonQuery
    ) -> str:
        """Find the person's profile URL from their work email, then their name.

        Email lookup first because it is exact; the name+company fallback exists
        because a personal-domain address (a broker on gmail) still names a real
        person we can identify from the invite.
        """
        resolved = self._get_json(
            client,
            f"{base}/api/v2/linkedin/profile/resolve/email",
            {"email": query.email, "lookup_depth": "deep"},
        )
        url = str((resolved or {}).get("linkedin_profile_url") or "").strip()
        if url:
            return url

        name = query.display_name()
        if not name:
            return ""
        by_name = self._get_json(
            client,
            f"{base}/api/v2/linkedin/profile/resolve",
            {
                "company_domain": query.domain,
                "first_name": name.split()[0],
                "last_name": name.split()[-1] if len(name.split()) > 1 else "",
            },
        )
        return str((by_name or {}).get("url") or "").strip()

    def _get_json(
        self, client: httpx.Client, url: str, params: dict[str, str]
    ) -> dict[str, Any] | None:
        """One GET. Returns None on any non-2xx or transport error.

        Fail-soft per call: a 404 on posts (profile exists, no public activity)
        must still yield the profile, and an out-of-credits 402 must degrade to
        grounded search rather than blank the attendee.
        """
        try:
            resp = client.get(url, params={k: v for k, v in params.items() if v})
            if resp.status_code >= 300:
                logger.info(
                    "linkedin api %s → %s for params=%s",
                    url.rsplit("/", 1)[-1],
                    resp.status_code,
                    sorted(params),
                )
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("linkedin api %s failed: %s", url.rsplit("/", 1)[-1], exc)
            return None
