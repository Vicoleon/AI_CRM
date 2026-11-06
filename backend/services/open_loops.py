"""Open loops — one answer to "what am I waiting on?" across Gmail AND Slack.

Why this exists. An exec asked Cubie "what am I waiting on" and got
almost nothing back; they had to re-ask naming Gmail and Slack to get a list, and
two of the items on it had already been closed in the same thread days earlier.
Claude, asked the same bare question, produced a prioritised list with the
closed items dropped and a note on what it had skipped. Three gaps, one tool:

1. **Routing.** "Waiting on" named no source, and the coordinator had a rule for
   "summarize my unread" and "what did I miss on Slack" but none for open
   loops, so the model fell back to a single tool and answered from one corner
   of the user's day. This module sweeps every conversational source at once.
2. **Thread state.** The digest tools summarise a MESSAGE. An open loop is a
   property of a THREAD — the ask in message 1 may have been answered in
   message 4 — so every candidate here carries its last few messages in order
   and the model is told to judge the latest state, not the loudest message.
3. **Shape.** The answer is rendered HERE from a small JSON verdict per thread,
   not free-formed by the model: sections are fixed (needs you / waiting on
   them / skipped-and-why), every line links to the real thread, and the model
   can only refer to a candidate by a tag we handed it, so nothing can be
   invented. The "skipped" section is deliberate — it is what makes the list
   trustworthy, because the reader can see the closed items were considered.

Two gathering legs run concurrently. Gmail is the user's own mailbox via
Domain-Wide Delegation (``GmailClient``); Slack is the user's own OAuth
installation (``SlackService``). Each is best-effort and reports itself in the
footer: an unconnected Slack is a footer line, never an empty section
pretending to be a quiet week.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from rubik_core.cost.usage import attribute_to
from rubik_core.tools.addressing import classify_addressing, strongest_addressing

logger = logging.getLogger(__name__)

FEATURE = "open_loops"  # AI-Cost category — keep in step with admin._FEATURE_LABELS

DEFAULT_DAYS = 7
MAX_DAYS = 30
# Gmail: two lanes (inbox + sent) so "I asked and nobody answered" is found even
# when the thread has left the inbox. Each lane is a threads.list + one
# threads.get per thread, sequential inside GmailClient, so the cap is what
# keeps the sweep inside the chat turn's budget.
GMAIL_THREADS_PER_LANE = 20
SLACK_MENTION_LIMIT = 25
SLACK_DM_LIMIT = 20
SLACK_DM_HISTORY = 30
SLACK_CONCURRENCY = 6
# How much of a thread the model sees: the last N messages, each clipped.
TRANSCRIPT_TAIL = 8
MESSAGE_CHARS = 400

VALID_SOURCES = ("all", "gmail", "slack")
STATUSES = ("needs_me", "waiting_on_them", "resolved", "someone_else_has_it", "fyi")
PRIORITIES = ("high", "medium", "low")

_AUTOMATED_LOCAL_PARTS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "notifications",
    "notification",
    "newsletter",
    "bounces",
    "automated",
    "calendar-notification",
)

_SLACK_USER_TOKEN = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


# ─── Candidates ──────────────────────────────────────────────────────────────


@dataclass
class LoopCandidate:
    """One thread the model may rule on. ``tag`` is the ONLY handle it gets."""

    tag: str
    source: str  # "gmail" | "slack"
    title: str
    where: str  # "Gmail thread" | "#channel" | "DM with Alice"
    counterparty: str
    last_from_user: bool
    last_at: datetime | None
    transcript: str
    link: str | None = None
    addressing: str = "direct"
    unread: bool = False

    def prompt_block(self, now: datetime) -> str:
        age = ""
        if self.last_at is not None:
            hours = max(0.0, (now - self.last_at).total_seconds() / 3600)
            age = f"{hours / 24:.1f} days ago" if hours >= 24 else f"{hours:.0f}h ago"
        last_by = "the USER" if self.last_from_user else self.counterparty or "them"
        return (
            f"[{self.tag}] {self.source.upper()} — {self.where}\n"
            f"Title: {self.title}\n"
            f"Counterparty: {self.counterparty or '(unknown)'}\n"
            f"Last message by: {last_by} ({age or 'date unknown'})"
            + (f" — user was {self.addressing}" if self.source == "gmail" else "")
            + (" — UNREAD" if self.unread else "")
            + f"\nThread (oldest → newest):\n{self.transcript}"
        )


@dataclass
class SweepResult:
    days: int
    candidates: list[LoopCandidate] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    report_md: str = ""

    @property
    def by_tag(self) -> dict[str, LoopCandidate]:
        return {c.tag: c for c in self.candidates}


# ─── Gmail leg ───────────────────────────────────────────────────────────────


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (msg.get("payload") or {}).get("headers", [])
    }


def _msg_datetime(msg: dict[str, Any], headers: dict[str, str]) -> datetime | None:
    raw = msg.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    try:
        dt = parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_automated(from_email: str) -> bool:
    local = from_email.split("@", 1)[0].lower() if "@" in from_email else ""
    return any(p in local for p in _AUTOMATED_LOCAL_PARTS)


def _names(header_value: str) -> str:
    """'Alice <a@x>, Bob <b@y>' → 'Alice, Bob' (falls back to the address)."""
    out = []
    for part in header_value.split(","):
        name, addr = parseaddr(part.strip())
        label = name or addr
        if label:
            out.append(label)
    return ", ".join(out[:4]) + (", …" if len(out) > 4 else "")


def _fmt_when(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%b %d %H:%M") if dt else "?"


def build_gmail_candidates(
    threads: list[dict[str, Any]],
    user_email: str,
    *,
    now: datetime,
    tag_prefix: str = "G",
) -> list[LoopCandidate]:
    """Turn hydrated Gmail threads (``format=metadata``) into candidates.

    Pure: no I/O, so the filters are unit-testable. Drops distribution-list mail
    the user never took part in and threads where nobody but a robot wrote —
    a "waiting on" list is about people. Everything else is handed to the
    model with its last few messages in order, because whether the loop is
    still open is a judgement about the NEWEST message, not the first.
    """
    me = (user_email or "").strip().lower()
    out: list[LoopCandidate] = []
    n = 0
    for thread in threads:
        tid = thread.get("id") or ""
        raw = thread.get("messages") or []
        if not tid or not raw:
            continue
        rows = []
        for m in raw:
            h = _headers(m)
            _, addr = parseaddr(h.get("from", ""))
            rows.append((m, h, (addr or "").lower(), _msg_datetime(m, h)))
        rows.sort(key=lambda r: r[3] or datetime.min.replace(tzinfo=timezone.utc))

        user_wrote = any(addr == me for _, _, addr, _ in rows)
        others = [r for r in rows if r[2] != me]
        if not user_wrote:
            addressing = strongest_addressing(
                [classify_addressing(h, me) for _, h, _, _ in others]
            )
            if addressing == "list":
                continue  # bulk / distribution-list mail — not a loop with a person
            if others and all(_is_automated(addr) for _, _, addr, _ in others):
                continue  # only robots spoke
        else:
            addressing = "direct"

        last_msg, last_h, last_addr, last_at = rows[-1]
        last_from_user = last_addr == me
        # Who the loop is with: the newest human who is not the user; when the
        # user has spoken to an empty room, whoever they wrote to.
        counterparty = ""
        for _, h, addr, _ in reversed(rows):
            if addr != me and not _is_automated(addr):
                counterparty = _names(h.get("from", ""))
                break
        if not counterparty:
            counterparty = _names(last_h.get("to", "")) or "(unknown)"

        lines = []
        for m, h, addr, dt in rows[-TRANSCRIPT_TAIL:]:
            who = "USER" if addr == me else (_names(h.get("from", "")) or addr or "?")
            to = _names(h.get("to", ""))
            snippet = re.sub(r"\s+", " ", (m.get("snippet") or "")).strip()
            lines.append(
                f"- [{_fmt_when(dt)}] {who}"
                + (f" → {to}" if to else "")
                + f": {snippet[:MESSAGE_CHARS]}"
            )
        n += 1
        out.append(
            LoopCandidate(
                tag=f"{tag_prefix}{n}",
                source="gmail",
                title=(rows[0][1].get("subject") or "(no subject)").strip(),
                where="Gmail thread",
                counterparty=counterparty,
                last_from_user=last_from_user,
                last_at=last_at,
                transcript="\n".join(lines),
                link=f"https://mail.google.com/mail/u/0/#all/{tid}",
                addressing=addressing,
                unread="UNREAD" in (last_msg.get("labelIds") or []),
            )
        )
    return out


def gather_gmail(
    user_email: str, days: int, *, client: Any | None = None, now: datetime
) -> tuple[list[LoopCandidate], str]:
    """Inbox + Sent lanes over ``days``, de-duplicated by thread id.

    Returns the candidates and a footer fragment like ``"Gmail (23 threads)"``.
    Sync — the caller runs it in a worker thread beside the Slack leg.
    """
    if client is None:
        from rubik_core.tools.gmail import GmailClient

        client = GmailClient(user_email=user_email)
    lanes = (
        f"in:inbox newer_than:{days}d -in:chats",
        f"in:sent newer_than:{days}d -in:chats",
    )
    seen: dict[str, dict[str, Any]] = {}
    for query in lanes:
        for t in client.search_threads(query=query, max_results=GMAIL_THREADS_PER_LANE):
            tid = t.get("id")
            if tid and tid not in seen:
                seen[tid] = t
    candidates = build_gmail_candidates(list(seen.values()), user_email, now=now)
    return candidates, f"Gmail ({len(seen)} threads)"


# ─── Slack leg ───────────────────────────────────────────────────────────────


def _humanize_slack_text(
    text: str, me: str, resolve: Callable[[str], str] | None
) -> str:
    def _sub(m: re.Match[str]) -> str:
        uid = m.group(1)
        if uid == me:
            return "@you"
        name = resolve(uid) if resolve else ""
        return f"@{name}" if name and name != uid else "@someone"

    return re.sub(r"\s+", " ", _SLACK_USER_TOKEN.sub(_sub, text or "")).strip()


def _slack_transcript(
    msgs: list[Any], me: str, resolve: Callable[[str], str] | None
) -> str:
    lines = []
    for m in msgs[-TRANSCRIPT_TAIL:]:
        try:
            dt: datetime | None = datetime.fromtimestamp(
                float(m.ts.split(".")[0]), tz=timezone.utc
            )
        except (TypeError, ValueError, AttributeError):
            dt = None
        who = "USER" if (m.user_id and m.user_id == me) else (m.user_name or "?")
        lines.append(
            f"- [{_fmt_when(dt)}] {who}: "
            f"{_humanize_slack_text(m.text, me, resolve)[:MESSAGE_CHARS]}"
        )
    return "\n".join(lines)


def _slack_ts(m: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(m.ts), tz=timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None


async def gather_slack(
    user_email: str,
    days: int,
    *,
    svc: Any | None = None,
    now: datetime,
) -> tuple[list[LoopCandidate], str]:
    """@-mentions (each read to the end of its thread) plus DMs over ``days``.

    Raises ``SlackNotConnectedError`` so the caller can put a footer line in
    the report instead of an empty section. Threads are fetched in parallel
    under a small semaphore, the way the Slack digest does it.
    """
    from backend.services.slack_service import SlackNotConnectedError, SlackService

    if svc is None:
        svc = SlackService(user_email=user_email)
    if not svc.is_connected:  # a property on SlackService, not a method
        raise SlackNotConnectedError(user_email)

    since = now - timedelta(days=days)
    me = svc.slack_user_id()
    resolve = svc.resolve_user_name
    sem = asyncio.Semaphore(SLACK_CONCURRENCY)

    mentions = await asyncio.to_thread(svc.get_mentions, since, SLACK_MENTION_LIMIT)
    dms = await asyncio.to_thread(svc.get_dm_threads, SLACK_DM_LIMIT)

    # One candidate per THREAD, however many times the user was tagged in it.
    roots: dict[tuple[str, str], Any] = {}
    for m in mentions:
        key = (m.channel_id, m.thread_ts or m.ts)
        roots.setdefault(key, m)

    async def _thread(key: tuple[str, str], mention: Any):
        async with sem:
            replies = await asyncio.to_thread(svc.get_thread_replies, key[0], key[1])
        return mention, replies

    async def _dm(dm: dict[str, Any]):
        cid = dm.get("channel_id")
        if not cid:
            return dm, []
        async with sem:
            try:
                msgs = await asyncio.to_thread(
                    svc.get_channel_messages, cid, since, SLACK_DM_HISTORY, True
                )
            except Exception as e:  # noqa: BLE001 — one DM must not sink the sweep
                logger.info("open_loops: DM history failed for %s: %s", cid, e)
                msgs = []
        return dm, msgs

    thread_results, dm_results = await asyncio.gather(
        asyncio.gather(*[_thread(k, m) for k, m in roots.items()]),
        asyncio.gather(*[_dm(d) for d in dms]),
    )

    out: list[LoopCandidate] = []
    n = 0
    for mention, replies in thread_results:
        msgs = sorted(replies or [mention], key=lambda x: float(x.ts or 0))
        last = msgs[-1]
        n += 1
        channel = f"#{mention.channel_name}" if mention.channel_name else "a channel"
        out.append(
            LoopCandidate(
                tag=f"S{n}",
                source="slack",
                title=_humanize_slack_text(mention.text, me, resolve)[:120],
                where=channel,
                counterparty=mention.user_name or "(unknown)",
                last_from_user=bool(me) and last.user_id == me,
                last_at=_slack_ts(last),
                transcript=_slack_transcript(msgs, me, resolve),
                link=mention.app_link or mention.permalink,
            )
        )
    dm_count = 0
    for dm, msgs in dm_results:
        if not msgs:
            continue
        msgs = sorted(msgs, key=lambda x: float(x.ts or 0))
        last = msgs[-1]
        who = dm.get("counterparty_name") or (
            "a group" if dm.get("is_mpim") else "someone"
        )
        n += 1
        dm_count += 1
        out.append(
            LoopCandidate(
                tag=f"S{n}",
                source="slack",
                title=_humanize_slack_text(msgs[0].text, me, resolve)[:120],
                where=f"Group DM with {who}" if dm.get("is_mpim") else f"DM with {who}",
                counterparty=who,
                last_from_user=bool(me) and last.user_id == me,
                last_at=_slack_ts(last),
                transcript=_slack_transcript(msgs, me, resolve),
                link=last.app_link or last.permalink,
            )
        )
    return out, f"Slack ({len(roots)} mention threads, {dm_count} DMs)"


# ─── Synthesis ───────────────────────────────────────────────────────────────


def _prompt(
    candidates: list[LoopCandidate], *, user_first_name: str, days: int, now: datetime
) -> str:
    blocks = "\n\n".join(c.prompt_block(now) for c in candidates)
    return (
        "You are the chief of staff to a busy executive. Below are email threads and "
        "Slack conversations from the last "
        f"{days} days. Decide, for EACH one, whether it is still an open loop for the "
        "user — and if so, who is waiting on whom.\n\n"
        f"The user's first name is {user_first_name or '(unknown)'}. Messages they "
        "wrote are marked USER. Today is "
        f"{now.strftime('%A, %B %d, %Y')}.\n\n"
        "Read each thread to its NEWEST message and judge the CURRENT state:\n"
        "- needs_me — someone asked the user for something (a decision, an approval, a "
        "reply, a review) and the newest messages do not show the user doing it.\n"
        "- waiting_on_them — the user asked for or expects something, and nobody has "
        "come back with it yet.\n"
        "- resolved — the ask WAS met later in the thread ('done', 'sent', 'approved', "
        "'closed', 'all set', the file arrived, the question was answered). Say what "
        "closed it.\n"
        "- someone_else_has_it — another person has clearly picked it up and the user "
        "is not the one holding it up.\n"
        "- fyi — nothing was asked of anyone; announcements, receipts, notes, "
        "newsletters, automated reminders with no decision attached.\n\n"
        "Priority (only for needs_me and waiting_on_them): high = a named deadline "
        "within a few days, a client or executive waiting, or money/contracts; medium = "
        "a real ask with no hard date; low = optional, or a close-the-loop courtesy.\n\n"
        "Rules:\n"
        "- Use ONLY the tags given (like G3 or S2). Never invent a tag or a thread.\n"
        "- Quote concrete details — names, amounts, dates, deadlines — from the text. "
        "Do not guess a deadline that is not there.\n"
        "- A thread the user answered last is NOT needs_me. A thread someone else "
        "answered last is NOT waiting_on_them unless their answer still leaves the "
        "user's ask open.\n"
        "- When in doubt between resolved and open, prefer open, but say why.\n\n"
        'Return ONLY a JSON object with an "items" array, one object per tag, with keys:\n'
        "  tag (string), status (one of needs_me | waiting_on_them | resolved | "
        "someone_else_has_it | fyi), priority (high | medium | low, or null), "
        "headline (<= 12 words, the thing itself, e.g. 'Acme Corp renewal SOW'), "
        "detail (one sentence, <= 30 words: who wants what from whom, and any date), "
        "owner (the person the loop is with), due (a date or deadline phrase, or null), "
        "why (for resolved / someone_else_has_it / fyi: one short clause saying what "
        "closed it or who has it).\n\n"
        f"{blocks}"
    )


def _parse_verdicts(raw: str, allowed_tags: set[str]) -> list[dict[str, Any]]:
    raw = (raw or "").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = data.get("items") or data.get("verdicts") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        tag = str(d.get("tag") or "").strip().upper()
        if tag not in allowed_tags or tag in seen:
            continue  # unknown or duplicated tag — the model made it up
        status = str(d.get("status") or "").strip().lower()
        if status not in STATUSES:
            status = "fyi"
        priority = str(d.get("priority") or "").strip().lower()
        if priority not in PRIORITIES:
            priority = "medium"
        seen.add(tag)
        out.append(
            {
                "tag": tag,
                "status": status,
                "priority": priority,
                "headline": str(d.get("headline") or "").strip(),
                "detail": str(d.get("detail") or "").strip(),
                "owner": str(d.get("owner") or "").strip(),
                "due": str(d.get("due") or "").strip(),
                "why": str(d.get("why") or "").strip(),
            }
        )
    return out


@attribute_to(FEATURE)
async def synthesize(
    candidates: list[LoopCandidate],
    *,
    user_first_name: str,
    days: int,
    now: datetime,
) -> list[dict[str, Any]] | None:
    """One batched Flash call → a verdict per tag. ``None`` when the model
    call fails, so the renderer can fall back to a direction-only list."""
    if not candidates:
        return []
    try:
        from rubik_core.config import settings
        from rubik_core.gemini_tuning import generation_config_for
        from rubik_core.inference_workloads import run_text_async
        from rubik_core.models import get_genai_client

        prompt = _prompt(
            candidates, user_first_name=user_first_name, days=days, now=now
        )

        async def managed(route):
            response = await get_genai_client().aio.models.generate_content(
                model=route.model,
                contents=prompt,
                config=generation_config_for(route.model),
            )
            return response.text or ""

        raw = await run_text_async(
            "open_loops",
            prompt,
            default_model=settings.VERTEX_LLM_MODEL_FLASH,
            managed=managed,
            json_mode=True,
        )
        return _parse_verdicts(raw, {c.tag for c in candidates})
    except Exception:  # noqa: BLE001 — the report shows its direction-only fallback
        logger.warning("open_loops: synthesis failed")
        return None


# ─── Rendering ───────────────────────────────────────────────────────────────


def _link(c: LoopCandidate) -> str:
    label = "Slack" if c.source == "slack" else "Gmail"
    return f"[{label}]({c.link})" if c.link else label


def _line(v: dict[str, Any], c: LoopCandidate) -> str:
    head = v.get("headline") or c.title
    parts = [f"**{head}**"]
    if v.get("detail"):
        parts.append(v["detail"])
    meta = []
    if v.get("owner"):
        meta.append(f"with {v['owner']}")
    if v.get("due"):
        meta.append(f"due {v['due']}")
    tail = f" _({'; '.join(meta)})_" if meta else ""
    return f"- {' — '.join(parts)}{tail} · {_link(c)}"


def render_report(verdicts: list[dict[str, Any]] | None, sweep: SweepResult) -> str:
    """Fixed-shape markdown from the verdicts. Never lets the model free-form."""
    by_tag = sweep.by_tag
    scope = " + ".join(
        s
        for s in ("Gmail", "Slack")
        if any(c.source == s.lower() for c in sweep.candidates)
    )
    title = f"**What you're waiting on — last {sweep.days} days" + (
        f", {scope}**" if scope else "**"
    )
    lines: list[str] = [title, ""]

    if verdicts is None:
        # Model unavailable: direction-only split, honestly labelled.
        lines.append(
            "_AI ranking was unavailable, so this is the raw split by who spoke last._"
        )
        lines.append("")
        lines.append("### Threads where someone else spoke last (may need you)")
        theirs = [c for c in sweep.candidates if not c.last_from_user]
        lines += [
            f"- **{c.title}** — {c.where}, {c.counterparty} · {_link(c)}"
            for c in theirs
        ] or ["- _none_"]
        lines.append("")
        lines.append("### Threads where you spoke last (may be waiting on them)")
        mine = [c for c in sweep.candidates if c.last_from_user]
        lines += [
            f"- **{c.title}** — {c.where}, {c.counterparty} · {_link(c)}" for c in mine
        ] or ["- _none_"]
    else:
        needs = [v for v in verdicts if v["status"] == "needs_me"]
        waiting = [v for v in verdicts if v["status"] == "waiting_on_them"]
        skipped = [
            v for v in verdicts if v["status"] in ("resolved", "someone_else_has_it")
        ]
        fyi_count = sum(1 for v in verdicts if v["status"] == "fyi")
        rank = {p: i for i, p in enumerate(PRIORITIES)}

        lines.append("### Needs your action")
        if needs:
            for p in PRIORITIES:
                bucket = [v for v in needs if v["priority"] == p]
                if not bucket:
                    continue
                lines.append(f"**{p.title()}**")
                lines += [_line(v, by_tag[v["tag"]]) for v in bucket]
        else:
            lines.append("_Nothing is waiting on you in this window._")
        lines.append("")

        lines.append("### Waiting on others")
        if waiting:
            waiting.sort(key=lambda v: rank.get(v["priority"], 9))
            lines += [_line(v, by_tag[v["tag"]]) for v in waiting]
        else:
            lines.append("_Nothing outstanding from anyone else._")
        lines.append("")

        if skipped:
            lines.append("### Skipped — already resolved or in someone else's hands")
            for v in skipped:
                c = by_tag[v["tag"]]
                why = v.get("why") or (
                    "resolved in the thread"
                    if v["status"] == "resolved"
                    else "someone else has it"
                )
                lines.append(f"- {v.get('headline') or c.title} — {why} · {_link(c)}")
            lines.append("")
        if fyi_count:
            lines.append(
                f"_Also read {fyi_count} thread{'s' if fyi_count != 1 else ''} with no "
                "ask in them (announcements, receipts, notes)._"
            )
            lines.append("")

    footer = []
    if sweep.checked:
        footer.append("Checked: " + ", ".join(sweep.checked) + ".")
    if sweep.not_checked:
        footer.append("Not checked: " + ", ".join(sweep.not_checked) + ".")
    if footer:
        lines.append("_" + " ".join(footer) + "_")
    return "\n".join(lines).strip()


# ─── Entry point ─────────────────────────────────────────────────────────────


def _first_name(user_email: str) -> str:
    local = (user_email or "").split("@", 1)[0]
    return local.split(".")[0].split("_")[0].capitalize() if local else ""


async def run_open_loops_sweep(
    user_email: str,
    *,
    days: int = DEFAULT_DAYS,
    sources: str = "all",
    gmail_client: Any | None = None,
    slack_service: Any | None = None,
    now: datetime | None = None,
    synthesizer: Callable[..., Any] | None = None,
) -> SweepResult:
    """Gather (Gmail ∥ Slack) → one model verdict per thread → rendered report.

    ``sources`` narrows the sweep when the user named one ("what am I waiting on
    in email"). Every leg is best-effort: a failure or an unconnected Slack is
    reported in the footer and the other leg still answers.
    """
    from backend.services.slack_service import SlackNotConnectedError

    now = now or datetime.now(timezone.utc)
    try:
        days = int(days or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    days = max(1, min(days, MAX_DAYS))
    sources = (sources or "all").strip().lower()
    if sources not in VALID_SOURCES:
        sources = "all"
    sweep = SweepResult(days=days)

    async def _gmail():
        return await asyncio.to_thread(
            gather_gmail, user_email, days, client=gmail_client, now=now
        )

    legs: dict[str, Any] = {}
    if sources in ("all", "gmail"):
        legs["gmail"] = _gmail()
    if sources in ("all", "slack"):
        legs["slack"] = gather_slack(user_email, days, svc=slack_service, now=now)

    results = await asyncio.gather(*legs.values(), return_exceptions=True)
    for name, res in zip(legs.keys(), results):
        if isinstance(res, SlackNotConnectedError):
            sweep.not_checked.append(
                "Slack (not connected — connect it in Settings → Slack)"
            )
        elif isinstance(res, BaseException):
            logger.warning(
                "open_loops: %s leg failed for %s: %s", name, user_email, res
            )
            sweep.not_checked.append(f"{name.capitalize()} (temporarily unavailable)")
        else:
            cands, label = res
            sweep.candidates.extend(cands)
            sweep.checked.append(label)

    if not sweep.candidates:
        sweep.report_md = render_report([], sweep)
        return sweep

    synth = synthesizer or synthesize
    verdicts = await synth(
        sweep.candidates,
        user_first_name=_first_name(user_email),
        days=days,
        now=now,
    )
    sweep.report_md = render_report(verdicts, sweep)
    return sweep
