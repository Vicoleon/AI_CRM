"""Gmail communication-health channel agent.

Runs with the REQUESTING user's mailbox (same tool boundary as chat). Because
snapshots are shared, the extractor prompt forces aggregates + short excerpts
only — never full thread bodies — and every evidence line is stamped
``via <user email>``.
"""

from __future__ import annotations

import logging

from rubik_core.xray import prompts
from rubik_core.xray.agents.base import BaseXrayAgent
from rubik_core.xray.schemas import ChannelDigest

logger = logging.getLogger(__name__)


class GmailChannelAgent(BaseXrayAgent):
    channel = "gmail"

    def gather(self) -> str:
        domain = self.company_domain
        if not domain:
            return ""
        from rubik_core.tools.gmail import GmailClient  # lazy import

        client = GmailClient(user_email=self.requesting_user)
        threads = client.search_threads(
            query=f"from:@{domain} OR to:@{domain}", max_results=15
        )
        return _format_threads(threads or [])

    def build(self, evidence: str) -> tuple[str, str]:
        return prompts.build_gmail_prompt(
            evidence, self.company_name, self.requesting_user
        )

    def parse(self, data) -> ChannelDigest:
        digest = super().parse(data)
        # Stamp evidence with the mailbox it came from (shared-snapshot privacy).
        stamped = [f"{e} (via {self.requesting_user})" for e in digest.evidence]
        digest.evidence = stamped or [f"email analysis via {self.requesting_user}"]
        return digest


def _format_threads(threads: list[dict], *, max_chars: int = 3500) -> str:
    """Compact rendering of Gmail thread dicts (headers + snippet only)."""
    parts: list[str] = []
    for t in threads:
        msgs = t.get("messages", []) or []
        if not msgs:
            continue
        first, last = msgs[0], msgs[-1]

        def _hdr(m: dict, name: str) -> str:
            for h in m.get("payload", {}).get("headers", []) or []:
                if h.get("name") == name:
                    return h.get("value", "")
            return ""

        subject = _hdr(last, "Subject") or "(no subject)"
        parts.append(
            f"[{_hdr(first, 'Date')[:16]} → {_hdr(last, 'Date')[:16]}] "
            f"{len(msgs)} msg(s)\nSubject: {subject}\n"
            f"First from: {_hdr(first, 'From')}\nLast from: {_hdr(last, 'From')}\n"
            f"Latest snippet: {(last.get('snippet') or '')[:300]}"
        )
        if sum(len(p) for p in parts) > max_chars:
            break
    return "\n\n---\n\n".join(parts) if parts else ""
