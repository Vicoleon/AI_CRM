"""Probe Vertex for which Gemini ids actually serve this project.

    # validate every id an admin can pick, plus the three pinned tiers
    python -m rubik_core.scripts.probe_models

    # …and hunt for ids newer than what we run (the "is there a Pro 3.x yet?" run)
    python -m rubik_core.scripts.probe_models --sweep

    # …and prove a candidate serves a real turn, can search, and isn't flaky
    python -m rubik_core.scripts.probe_models --sweep --workload --grounding --trials 5

    # probe a NAMED model the sweep can't guess (e.g. a launch email's id)
    python -m rubik_core.scripts.probe_models --ids gemini-4-argon --workload --grounding --trials 3

**Why this exists.** Every model decision in this repo has been settled by
probing Vertex, and each time the probe was rebuilt from scratch in a shell —
2026-06-25 (preview ids 429ing), 2026-08-12 (the 3.x upgrade), 2026-09-08 (3.8
Flash), 2026-09-17 (is there a Pro 3.x? — no). The findings landed in comments
in ``config.py`` but the instrument did not land anywhere, so the next question
costs the same hour and, worse, risks being answered with a *weaker* probe than
last time. This is that instrument.

``rubik_core.models.check_configured_models`` is the liveness check for the
three PINNED tiers and is wired to ``/api/health/models``. It deliberately does
not answer "what else could we pin?", which is this script's job.

**Four ways a naive probe lies, all of them learned the hard way:**

1. *No negative control.* If every id returns an error for an unrelated reason
   (wrong project, dead ADC, endpoint change), a list of 404s reads exactly like
   "Google has not shipped it". This script always probes made-up ids and
   **exits 2 without reporting anything** if one of them answers — the run is
   not evidence of anything at that point.
2. *A ping is not a turn.* ``max_output_tokens=1`` on "ping" proves an id
   resolves, not that it serves real work. ``--workload`` sends a genuine
   multi-paragraph prompt instead.
3. *A single 429 is not a verdict.* 2026-09-17: ``gemini-3.7-flash`` 429'd once
   on a grounded call, then went 15/15 across ping, workload and grounded. That
   is the documented transient blip ``CHAT_LLM_RETRY_ATTEMPTS`` covers, not a
   starved preview pool — but one call could not tell them apart, and treating
   it as starved would have wrongly excluded a usable model. ``--trials N``
   re-runs only the FAILING ids, so the discrimination is cheap.
4. *A trivial prompt is not a grounding test.* Asked for the capital of France,
   every 3.x model answers from memory and emits no grounding metadata — which
   reads as "cannot ground" and is false. ``--grounding`` uses a prompt that
   cannot be answered without a live search, and looks for real
   ``web_search_queries``.

**Why it builds its own client** rather than ``models.get_genai_client()``: that
one wraps every call in SDK-native backoff on transient 429/503 and logs to the
AI-Cost ledger. Backoff would hide precisely the signal being measured, and a
diagnostic should not bill itself to a feature. This client is raw and
uninstrumented on purpose.

**A sweep can only find what it can spell.** ``--sweep`` bumps the minor version
of the pinned tiers, so it reaches ``gemini-3.9-flash`` but can never find a
named model like ``gemini-4-argon`` (2026-10-14). ``--ids`` probes ids given on
the command line under the same rules, and adds a negative control per id from
the same naming family (``gemini-9.9-argon``), so its 404s mean the same thing.

``--workload`` sends the thinking knob production would send for that id
(``gemini_tuning.thinking_config_for``), so a 200 there is a 200 on the call
shape the app actually makes. A bare call would have missed that a drop-in
Argon 400s on every request when handed the 2.5-style ``thinking_budget``.

Cost is negligible by default — one ping per id, capped at one output token.
``--workload``/``--grounding``/``--trials`` cost real tokens; they are opt-in.

Exit codes: ``0`` nothing to do · ``1`` action needed (an offered id is dead, or
a newer id serves) · ``2`` probe untrustworthy, findings suppressed.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("probe_models")

# The SDK logs an INFO line per HTTP call plus an "AFC is enabled" banner, which
# at this script's volume buries the table it exists to print. The per-id status
# column already reports what those lines would tell us.
for _noisy in ("google_genai", "google_genai.models", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

#: Cannot be answered without a live search — see lie #4 in the module docstring.
GROUNDING_PROMPT = (
    "Using Google Search, report one news headline published this week about "
    "artificial intelligence regulation, and give its source URL."
)

#: A real turn's worth of work — see lie #2.
WORKLOAD_PROMPT = (
    "Summarize in 120 words why vendor oversight matters for a mid-size "
    "company, then list three risks as bullets."
)

#: Ids that must NOT exist. If any of these serves, the run proves nothing —
#: see lie #1. Deliberately shaped like real ids so a prefix-matching proxy or
#: a permissive alias resolver would be caught rather than waved through.
NEGATIVE_CONTROLS: tuple[str, ...] = (
    "gemini-9.9-pro",
    "gemini-9.9-flash",
    # Was "gemini-4.7-flash-lite" until a Gemini 4 family shipped (2026-10), at
    # which point a 4.x id stopped being obviously fake.
    "gemini-9.9-flash-lite",
)

_VERSION_RE = re.compile(r"^gemini-(\d+)(?:\.(\d+))?-(.+)$")


def _classify(msg: str) -> str:
    """Bucket a Vertex error the same way ``check_configured_models`` does."""
    if "404" in msg or "NOT_FOUND" in msg:
        return "not_found"  # id deprecated, rotated, or never shipped
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return "quota_exhausted"  # starved pool, or a transient blip — see --trials
    if "400" in msg or "INVALID_ARGUMENT" in msg:
        return "bad_request"  # id is fine, the call shape isn't
    if "403" in msg or "PERMISSION_DENIED" in msg:
        return "permission_denied"
    return "error"


def _family_controls(ids: list[str]) -> list[str]:
    """A made-up sibling for each requested id: same name, impossible version.

    ``gemini-4-argon`` → ``gemini-9.9-argon``. The fixed controls above are all
    pro/flash/flash-lite, so they say nothing about how Vertex answers an
    unknown id in a NEW naming family; this one does. If it answers 200, a 200
    on the real id proves nothing either.
    """
    out: list[str] = []
    for model_id in ids:
        m = _VERSION_RE.match(model_id)
        if m:
            ctl = f"gemini-9.9-{m.group(3)}"
        elif model_id.startswith("gemini-"):
            ctl = f"gemini-9.9-{model_id[len('gemini-') :]}"
        else:
            ctl = f"{model_id}-9.9-control"
        if ctl not in ids and ctl not in out:
            out.append(ctl)
    return out


def _sweep_candidates(pinned: dict[str, str], depth: int) -> list[str]:
    """Ids newer than what we pin, derived FROM the pins so the grid never goes
    stale.

    The ceiling is the highest version pinned across ALL tiers, plus ``depth``
    minor bumps — deliberately not each tier's own pin plus ``depth``. That
    distinction is the whole point of the sweep: as of 2026-09 FLASH is on 3.8
    while PRO is stranded on 2.5, and a per-tier bump of six would ask about
    ``gemini-3.1-pro`` and stop, never reaching ``gemini-3.8-pro`` — the one id
    anyone actually wants to know about. Levelling every suffix up to the
    frontier asks "has Google shipped a Pro at the generation Flash is already
    on?", which is the question a stranded tier raises.

    Google's versions are not dense — 3.5/3.6/3.7/3.8 Flash all exist while
    3.2-3.4 never shipped — so a grid is the only honest way to ask, and the
    404s it produces are expected output, not noise.
    """
    parsed: dict[str, tuple[int, int]] = {}
    for model_id in pinned.values():
        m = _VERSION_RE.match(model_id)
        if m:
            parsed[m.group(3)] = (int(m.group(1)), int(m.group(2) or 0))
    if not parsed:
        return []

    def bump(v: tuple[int, int]) -> tuple[int, int]:
        major, minor = v
        return (major + 1, 0) if minor >= 9 else (major, minor + 1)

    ceiling = max(parsed.values())
    for _ in range(max(0, depth)):
        ceiling = bump(ceiling)

    out: list[str] = []
    for suffix, version in parsed.items():
        cur = bump(version)
        while cur <= ceiling:
            out.append(f"gemini-{cur[0]}.{cur[1]}-{suffix}")
            cur = bump(cur)
        out.append(f"gemini-{suffix}-latest")
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


def _probe(
    client,
    model_id: str,
    *,
    prompt: str,
    grounded: bool,
    max_tokens: int,
    thinking=None,
) -> dict:
    from google.genai import types

    cfg = types.GenerateContentConfig(max_output_tokens=max_tokens)
    if grounded:
        cfg.tools = [types.Tool(google_search=types.GoogleSearch())]
    if thinking is not None:
        cfg.thinking_config = thinking
    started = time.monotonic()
    try:
        res = client.models.generate_content(
            model=model_id, contents=prompt, config=cfg
        )
    except Exception as e:  # noqa: BLE001 — a diagnostic must never raise
        return {
            "status": _classify(str(e)),
            "error": str(e)[:180].replace("\n", " "),
            "secs": round(time.monotonic() - started, 1),
        }

    usage = getattr(res, "usage_metadata", None)
    traffic = getattr(usage, "traffic_type", None)
    row = {
        "status": "ok",
        # Reported RAW, never defaulted to model_id: the point is catching a
        # "-latest" alias Google has repointed, and defaulting would make "no
        # answer" indistinguishable from "no drift" (same rule as models.py).
        "served": getattr(res, "model_version", None),
        # ON_DEMAND is the GA bucket. A preview id rides shared quota and 429s
        # under light load, which is why only GA ids reach a dropdown.
        "traffic": str(traffic).replace("TrafficType.", "") if traffic else None,
        "secs": round(time.monotonic() - started, 1),
        "out_tokens": getattr(usage, "candidates_token_count", None),
        "thought_tokens": getattr(usage, "thoughts_token_count", None),
    }
    if grounded:
        cand = (res.candidates or [None])[0]
        meta = getattr(cand, "grounding_metadata", None) if cand else None
        row["searched"] = bool(getattr(meta, "web_search_queries", None))
    return row


def _describe_thinking(thinking) -> str:
    """The knob a workload call sent, in the health check's words."""
    if thinking is None:
        return "thinking=default"
    if thinking.thinking_level is not None:
        return f"thinking=level={thinking.thinking_level.value.lower()}"
    return f"thinking=budget={thinking.thinking_budget}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="also probe ids newer than the pinned tiers",
    )
    ap.add_argument(
        "--sweep-depth",
        type=int,
        default=6,
        help="minor-version bumps per tier (default 6)",
    )
    ap.add_argument(
        "--workload",
        action="store_true",
        help="send a real prompt instead of a 1-token ping",
    )
    ap.add_argument(
        "--grounding",
        action="store_true",
        help="also test Google Search grounding (serving ids only)",
    )
    ap.add_argument(
        "--trials",
        type=int,
        default=1,
        metavar="N",
        help="re-run FAILING ids up to N times to tell a transient "
        "429 from a starved pool (default 1 = no retry)",
    )
    ap.add_argument(
        "--ids",
        nargs="+",
        metavar="ID",
        default=[],
        help="probe these ids INSTEAD of the offered catalog — for a named "
        "model --sweep cannot spell (e.g. gemini-4-argon). Adds a same-family "
        "negative control per id",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    from google import genai

    from rubik_core.agent_runtime.chat_config import CHAT_MODEL_CATALOG
    from rubik_core.config import settings
    from rubik_core.gemini_tuning import split_model_choice, thinking_config_for
    from rubik_core.priorities.agents.base import _MODEL_KEY_MAP
    from rubik_core.priorities.settings_store import PROVIDER_MODEL_CATALOG
    from rubik_core.sales.model_config import (
        GROUNDING_CAPABLE_MODELS,
        SALES_MODEL_CATALOG,
        SALES_MODEL_ROLES,
        allowed_models_for,
    )

    # A git worktree has no .env of its own, so this resolves to None there and
    # the SDK fails several frames later with something that reads like an auth
    # problem. Say the actual thing instead.
    if not settings.GCP_PROJECT_ID:
        logger.error(
            "GCP_PROJECT_ID is unset, so there is no project to probe.\n"
            "  GCP_PROJECT_ID=<project> python -m rubik_core.scripts.probe_models\n"
            "(Worktrees do not inherit the parent checkout's .env — this is "
            "usually why.)"
        )
        return 2

    # Raw and uninstrumented — see the module docstring.
    client = genai.Client(
        vertexai=True,
        project=settings.GCP_PROJECT_ID,
        location=settings.VERTEX_LLM_LOCATION,
    )

    pinned = {
        "LITE": settings.VERTEX_LLM_MODEL_LITE,
        "FLASH": settings.VERTEX_LLM_MODEL_FLASH,
        "PRO": settings.VERTEX_LLM_MODEL_PRO,
    }
    # The AI-Priorities dropdown offers tier ALIASES ("gemini-pro") that resolve
    # through these same three settings, so checking the pins covers it too.
    # Per-role choices (Deep Prep) and the AI-Priorities dropdown can offer an
    # id outside both catalogs, sometimes with a pinned thinking level
    # ("gemini-4-argon@high"). Probe the id; tier aliases resolve to the pins.
    choices = {m for role in SALES_MODEL_ROLES for m in allowed_models_for(role.key)}
    choices |= {m for m in PROVIDER_MODEL_CATALOG["vertex"] if m not in _MODEL_KEY_MAP}
    catalog = sorted(
        set(CHAT_MODEL_CATALOG)
        | set(SALES_MODEL_CATALOG)
        | set(pinned.values())
        | {split_model_choice(m)[0] for m in choices}
    )
    requested = list(dict.fromkeys(args.ids))
    # --ids answers "does THIS id serve?", so the catalog health check is noise
    # in that run; a plain run (or --sweep) still covers it.
    offered = [] if requested else catalog
    sweep = _sweep_candidates(pinned, args.sweep_depth) if args.sweep else []
    sweep = [c for c in sweep if c not in catalog and c not in requested]
    controls = list(NEGATIVE_CONTROLS) + [
        c for c in _family_controls(requested) if c not in NEGATIVE_CONTROLS
    ]

    prompt = WORKLOAD_PROMPT if args.workload else "ping"
    # Room for the thinking the production knob buys on top of the ~120-word
    # answer; a ping stays at one token.
    max_tokens = 1024 if args.workload else 1

    def run(model_id: str) -> dict:
        thinking = thinking_config_for(model_id) if args.workload else None

        def once() -> dict:
            return _probe(
                client,
                model_id,
                prompt=prompt,
                grounded=False,
                max_tokens=max_tokens,
                thinking=thinking,
            )

        row = once()
        # Retry ONLY failures, and only retryable ones — a 404 will not become a
        # 200 on attempt two, and burning trials on it just makes the run slow.
        attempts = 1
        while row["status"] in {"quota_exhausted", "error"} and attempts < max(
            1, args.trials
        ):
            time.sleep(1.5)
            attempts += 1
            row = once()
        row["attempts"] = attempts
        row["thinking"] = _describe_thinking(thinking)
        return row

    logger.info(
        "project=%s location=%s  (%d offered, %d requested, %d sweep, %d controls)",
        settings.GCP_PROJECT_ID,
        settings.VERTEX_LLM_LOCATION,
        len(offered),
        len(requested),
        len(sweep),
        len(controls),
    )

    # ── Controls first: if these are wrong, nothing below is worth printing ──
    bad_controls = []
    for ctl in controls:
        if (
            _probe(client, ctl, prompt="ping", grounded=False, max_tokens=1)["status"]
            == "ok"
        ):
            bad_controls.append(ctl)
    if bad_controls:
        logger.error(
            "\nPROBE UNTRUSTWORTHY — made-up id(s) answered 200: %s\n"
            "Every result below would be meaningless, so none was collected. "
            "Check the project, ADC and endpoint before rerunning.",
            ", ".join(bad_controls),
        )
        return 2
    logger.info("controls 404 as expected: %s", ", ".join(controls))

    results: dict[str, dict] = {}
    for label, ids in (
        ("OFFERED / PINNED", offered),
        ("REQUESTED", requested),
        ("SWEEP", sweep),
    ):
        if not ids:
            continue
        logger.info("\n== %s ==", label)
        width = max(len(i) for i in ids) + 2
        for model_id in ids:
            row = results[model_id] = run(model_id)
            tier = next((t for t, m in pinned.items() if m == model_id), "")
            if row["status"] == "ok":
                detail = f"served={row['served']} traffic={row['traffic']}"
                if args.workload:
                    detail += (
                        f" {row['secs']}s out={row['out_tokens']}"
                        f" thoughts={row['thought_tokens']} {row['thinking']}"
                    )
            else:
                detail = row.get("error", "")[:78]
            note = f"  <- {tier} tier" if tier else ""
            tries = f"  ({row['attempts']} attempts)" if row["attempts"] > 1 else ""
            logger.info(
                "%-*s %-16s %s%s%s", width, model_id, row["status"], detail, note, tries
            )

    serving = [m for m, r in results.items() if r["status"] == "ok"]

    if args.grounding and serving:
        logger.info("\n== GROUNDING (search-forcing prompt) ==")
        for model_id in serving:
            g = _probe(
                client, model_id, prompt=GROUNDING_PROMPT, grounded=True, max_tokens=400
            )
            listed = model_id in GROUNDING_CAPABLE_MODELS
            can = g.get("searched") if g["status"] == "ok" else None
            # Only a catalog id can disagree with the catalog's grounding list;
            # a requested id was never on it.
            flag = (
                ""
                if can == listed or model_id not in catalog
                else "   <- DISAGREES WITH GROUNDING_CAPABLE_MODELS"
            )
            logger.info(
                "%-28s %-16s searched=%s listed=%s%s",
                model_id,
                g["status"],
                can,
                listed,
                flag,
            )
            results[model_id]["grounding"] = g

    # ── What a human should do about it ──────────────────────────────────────
    dead = [m for m in offered if results[m]["status"] != "ok"]
    fresh = [m for m in sweep if results[m]["status"] == "ok"]
    unserved = [m for m in requested if results[m]["status"] != "ok"]

    logger.info("\n== ACTIONS ==")
    if dead:
        logger.warning(
            "DEAD ID IN A DROPDOWN: %s — remove from the catalog in "
            "agent_runtime/chat_config.py and/or sales/model_config.py. An admin "
            "picking one fails every run of that role.",
            ", ".join(dead),
        )
    if fresh:
        logger.warning(
            "NEWER ID SERVES: %s — re-run with --workload --grounding --trials 5 "
            "before listing it; a ping is not a turn.",
            ", ".join(fresh),
        )
    for model_id in requested:
        if model_id in unserved:
            logger.warning(
                "REQUESTED ID DOES NOT SERVE: %s (%s)",
                model_id,
                results[model_id]["status"],
            )
        else:
            logger.info(
                "REQUESTED ID SERVES: %s (a liveness answer, not a quality one: "
                "a tier move needs a replay on real inputs).",
                model_id,
            )
    if not requested and not dead and not fresh:
        logger.info("Nothing to do: every offered id serves, and nothing newer exists.")

    if args.json:
        print(
            json.dumps(
                {
                    "pinned": pinned,
                    "controls": controls,
                    "results": results,
                    "dead": dead,
                    "newer": fresh,
                    "requested_unserved": unserved,
                },
                indent=2,
                default=str,
            )
        )
    return 1 if (dead or fresh or unserved) else 0


if __name__ == "__main__":
    sys.exit(main())
