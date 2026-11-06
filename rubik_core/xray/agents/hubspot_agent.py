"""HubSpot relationship-history channel agent.

No new network I/O — it extracts from the ``CompanyBundle`` the service already
fetched: deal progression, products, engagement stats, stakeholder map.
"""

from __future__ import annotations

from rubik_core.xray import prompts
from rubik_core.xray.agents.base import BaseXrayAgent


class HubSpotChannelAgent(BaseXrayAgent):
    channel = "hubspot"

    def gather(self) -> str:
        b = self.bundle
        lines = [prompts.format_deals_block(b, limit=40)]
        counts = {k: len(v) for k, v in b.engagements.items()}
        if counts:
            lines.append(
                "ENGAGEMENT COUNTS: " + ", ".join(f"{k}={v}" for k, v in counts.items())
            )
        if b.contacts:
            lines.append("STAKEHOLDERS:")
            for c in b.contacts[:25]:
                name = (
                    f"{c.get('firstname', '')} {c.get('lastname', '')}".strip()
                    or c.get("email", "")
                )
                title = c.get("jobtitle", "")
                lines.append(f"  - {name} — {title} <{c.get('email', '')}>")
        return "\n".join(lines)

    def build(self, evidence: str) -> tuple[str, str]:
        return prompts.build_channel_prompt("hubspot", evidence, self.company_name)
