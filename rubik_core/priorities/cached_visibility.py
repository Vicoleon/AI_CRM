"""Safe read path for persisted AI Priorities cards.

The stored Markdown and headline were written under an older CRM assignment.
Only structured cards can be checked against today's HubSpot ownership before
they are shown again. Legacy cards without usable provenance are omitted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rubik_core.priorities.models import (
    EOS_PRIORITY_KINDS,
    ChannelKind,
    EntityKind,
    PriorityKind,
    is_eos_priority_card,
)


def verified_bundle_deals(
    metadata: Any,
    *,
    visible_deals: Callable[[set[str]], set[str]] | None = None,
) -> set[str] | None:
    """Live proof for the entire v2 LLM context; None means unavailable.

    A model can copy a deal's facts into a card tagged Gmail. Card-level tags
    therefore cannot authorize its prose. The server-stamped bundle scope is
    the unit we prove before returning any stored card text or action target.
    """
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("schema_version")) is not int
    ):
        return None
    if metadata["schema_version"] != 2:
        return None
    marker = metadata.get("crm_provenance")
    if not isinstance(marker, dict) or marker.get("unprovable") is not False:
        return None
    deal_ids = marker.get("deal_ids")
    if (
        not isinstance(deal_ids, list)
        or any(not isinstance(value, str) or not value.strip() for value in deal_ids)
        or deal_ids != sorted(set(deal_ids))
    ):
        return None
    priorities = metadata.get("priorities")
    if not isinstance(priorities, list) or any(
        not isinstance(row, dict) for row in priorities
    ):
        return None
    try:
        from rubik_core.tenancy.context import current_workspace

        if marker.get("workspace") != current_workspace():
            return None
    except Exception:  # noqa: BLE001 - missing workspace scope hides prose
        return None

    stamped = set(deal_ids)
    for row in priorities:
        try:
            required = crm_deal_ids(row)
        except (TypeError, ValueError):
            return None
        if required is not None and (not required or not required <= stamped):
            return None
    if not stamped:
        return set()
    if visible_deals is None:
        from backend.routers.agents import visible_hubspot_deal_ids

        visible_deals = visible_hubspot_deal_ids
    try:
        live = visible_deals(stamped)
    except Exception:  # noqa: BLE001 - a portal outage hides the whole bundle
        return None
    if not isinstance(live, (set, frozenset)) or not stamped <= live:
        return None
    return stamped


def cached_bundle_visible(metadata: Any) -> bool:
    """Public whole-bundle gate for any persisted HomeCard prose reader."""
    return verified_bundle_deals(metadata) is not None


def _provenance_known(priority: dict[str, Any]) -> bool:
    """Require enough structured origin data to reject ambiguous old prose."""
    channels = priority.get("source_channels")
    if "source_channels" in priority and not isinstance(channels, list):
        return False
    links = priority.get("source_links")
    if "source_links" in priority and not (
        isinstance(links, list) and all(isinstance(link, str) for link in links)
    ):
        return False
    if isinstance(channels, list) and channels:
        return all(
            isinstance(channel, str) and channel in ChannelKind._value2member_map_
            for channel in channels
        )
    entity = priority.get("entity_kind")
    # These ids are native to non-CRM systems. A person or company without
    # channels could have come from HubSpot and is deliberately not trusted.
    return entity in {
        EntityKind.EVENT.value,
        EntityKind.THREAD.value,
        EntityKind.DOCUMENT.value,
        EntityKind.TRANSCRIPT.value,
        EntityKind.OFFBOARD_ITEM.value,
        EntityKind.PROMISE.value,
        EntityKind.ROCK.value,
        EntityKind.ISSUE.value,
        EntityKind.TODO.value,
        EntityKind.MILESTONE.value,
    }


def crm_deal_ids(priority: dict[str, Any]) -> set[str] | None:
    """Deal ids to prove, None for a clearly non-CRM card, empty if unprovable."""
    channels = {
        str(value).casefold() for value in priority.get("source_channels") or []
    }
    links = priority.get("source_links") or []
    crm = (
        priority.get("entity_kind") == EntityKind.DEAL.value
        or "hubspot" in channels
        or any("hubspot.com" in str(link).casefold() for link in links)
    )
    if not crm:
        return None
    if priority.get("entity_kind") != EntityKind.DEAL.value or not priority.get(
        "entity_id"
    ):
        return set()
    ids = {str(priority["entity_id"])}
    ids.update(
        str(value) for value in priority.get("covered_entity_ids") or [] if value
    )
    return ids


def visible_cached_priorities(
    priorities: list[Any],
    *,
    visible_deals: Callable[[set[str]], set[str]] | None = None,
    eos_enabled: bool = True,
    check_inherited: bool = True,
) -> list[dict[str, Any]]:
    """Return only cards with trustworthy provenance and live CRM ownership."""
    candidates = [
        row
        for row in priorities
        if isinstance(row, dict)
        and _provenance_known(row)
        and (eos_enabled or _classified_core(row))
    ]
    inherited = {
        str(row.get("entity_id") or "")
        for row in candidates
        if row.get("entity_kind") == EntityKind.OFFBOARD_ITEM.value
        or "inherited"
        in {str(channel).casefold() for channel in row.get("source_channels") or []}
    }
    inherited_visible: set[str] = set()
    if not check_inherited:
        # The offboarding store itself reads old HomeCards to verify its
        # history items. Never recurse from that verification into the store.
        candidates = [
            row
            for row in candidates
            if row.get("entity_kind") != EntityKind.OFFBOARD_ITEM.value
            and "inherited"
            not in {
                str(channel).casefold() for channel in row.get("source_channels") or []
            }
        ]
    elif inherited:
        try:
            from rubik_core.offboarding.store import visible_inherited_item_ids

            inherited_visible = visible_inherited_item_ids(inherited)
        except Exception:  # noqa: BLE001 - unproven inherited content stays hidden
            inherited_visible = set()
    requirements = [crm_deal_ids(row) for row in candidates]
    referenced = set().union(*(ids for ids in requirements if ids is not None))
    if referenced:
        if visible_deals is None:
            from backend.routers.agents import visible_hubspot_deal_ids

            visible_deals = visible_hubspot_deal_ids
        try:
            proved = visible_deals(referenced)
            visible = set(proved) if isinstance(proved, (set, frozenset)) else set()
        except Exception:  # noqa: BLE001 - a live proof failure hides CRM cards
            visible = set()
    else:
        visible = set()
    return [
        row
        for row, ids in zip(candidates, requirements)
        if (ids is None or (bool(ids) and ids <= visible))
        and (
            row.get("entity_kind") != EntityKind.OFFBOARD_ITEM.value
            and "inherited"
            not in {
                str(channel).casefold() for channel in row.get("source_channels") or []
            }
            or (
                row.get("entity_kind") == EntityKind.OFFBOARD_ITEM.value
                and str(row.get("entity_id") or "") in inherited_visible
            )
        )
    ]


def _classified_core(priority: dict[str, Any]) -> bool:
    """Preserve the offboarding history's strict EOS-off classification."""
    if is_eos_priority_card(priority):
        return False
    kind = priority.get("priority_kind")
    if (
        isinstance(kind, str)
        and kind in PriorityKind._value2member_map_
        and kind not in EOS_PRIORITY_KINDS
    ):
        return True
    entity = priority.get("entity_kind")
    if (
        isinstance(entity, str)
        and entity in EntityKind._value2member_map_
        and EntityKind._value2member_map_[entity]
        not in {
            EntityKind.TODO,
            EntityKind.MILESTONE,
            EntityKind.ROCK,
            EntityKind.ISSUE,
        }
    ):
        return True
    channels = priority.get("source_channels")
    return (
        isinstance(channels, list)
        and bool(channels)
        and all(
            isinstance(channel, str)
            and channel in ChannelKind._value2member_map_
            and channel != ChannelKind.EOS
            for channel in channels
        )
    )


def safe_headline(priorities: list[dict[str, Any]]) -> str:
    """Rebuild the heading from a visible card, never from cached narrative."""
    if not priorities:
        return ""
    title = str(priorities[0].get("title") or "").strip()
    return f"Top priority: {title}" if title else ""


def render_cached_briefing(
    metadata: Any,
    date_str: str,
    *,
    next_week: bool = False,
    eos_enabled: bool = True,
    compact: bool = False,
) -> str | None:
    """Rebuild a safe Markdown briefing; never return stored freeform Markdown."""
    verified = verified_bundle_deals(metadata)
    if verified is None:
        return None
    stored = metadata["priorities"]
    visible = visible_cached_priorities(
        stored,
        eos_enabled=eos_enabled,
        visible_deals=lambda ids: ids & verified,
    )
    if not visible and stored:
        return None
    if compact:
        return (
            "\n".join(
                "- " + " ".join(str(row.get("title") or "Priority").split())
                for row in visible
            )
            or None
        )

    title = (
        "### AI Priorities — Next week look-ahead"
        if next_week
        else f"### AI Priorities — {date_str}"
    )
    lines = [title, ""]
    if not visible:
        lines.append("No high-priority items surfaced today across your channels.")
        return "\n".join(lines)
    for index, row in enumerate(visible, 1):
        label = str(row.get("title") or "Priority").strip()
        lines.append(f"{index}. {label}")
        why = str(row.get("why_it_matters") or "").strip()
        action = str(row.get("suggested_action") or "").strip()
        if why:
            lines.append(f"   - {why}")
        if action:
            lines.append(f"   - Next step: {action}")
        lines.append("")
    return "\n".join(lines).rstrip()
