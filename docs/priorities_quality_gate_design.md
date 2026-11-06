# Priorities Quality Gate — Council v2 design

**Status:** DESIGN (not yet implemented)
**Trigger:** a nightly run can persist *empty* priority bundles. The Ingestion
Council detects them and FAILs the run, but its verdict gates nothing, so the
08:30 UTC Slack delivery silently skips those users, executives included. The
council's DM to the ops admins is the only symptom.

## What goes wrong (root-cause chain)

1. **Generation produced "successful" empty bundles.** Per-user pipeline
   failures are success-shaped:
   - `decision_maker.decide` calls `invoke_json(prompt, fallback={"priorities": []})`
     (`rubik_core/priorities/agents/decision_maker.py:191`) — an LLM
     failure/timeout is indistinguishable from "nothing is important today".
   - `orchestrator._run_channels` degrades crashed/timed-out channels to
     error digests and proceeds; if all channels fail, the decision-maker sees
     zero signals and emits zero priorities.
   - `batch.run_priorities_for_user` then persists the empty bundle with
     `succeeded=True`. Nothing downstream can tell it apart from a real
     "quiet day".
2. **The council is a reporter, not a gate.** `ingestion-council-nightly`
   (08:00 UTC) runs *between* generation (06:00) and Slack delivery (08:30) and
   its deterministic check `no empty priority bundles`
   (`rubik_core/ingestion_council/evidence.py`) caught the 5 empty bundles —
   but the verdict is only persisted + DM'd to the ops admins. Delivery never
   reads it.
3. **Delivery silently drops empty bundles.** `deliver_slack_priorities`
   (`backend/agents/slack_delivery.py`): `if not items: continue` — no
   counter, no name, no alert. An exec with an empty bundle just vanishes from
   the morning send.

Underlying fragility: an un-sharded sequential batch leaves zero headroom, so
per-user Vertex slowness turns into channel timeouts → empty bundles.

## Intent (what the council was supposed to be)

A **quality checker that runs before the priorities are delivered**, whose job
is to make sure what each user receives is genuinely the most important set
for *their* day — and to force repair when it isn't. The ingestion-run audit
(transcripts, EOS, corpus health) is a secondary ops report, not the product.

## Target pipeline

```
06:00 UTC  priorities-batch (sharded)            → per-user judged bundles
07:30 UTC  QUALITY GATE (council v2)            → per-user verdict + repair
08:30 UTC  slack delivery                       → sends only PASSED bundles;
                                                  names anyone it couldn't serve
```

### Stage 1 — deterministic per-bundle checks (free, no LLM)

Run inside the gate for every active user's bundle:

| Check | Fail condition |
|---|---|
| bundle exists | no `daily_briefing` HomeCard for today |
| non-empty | `metadata_json.priorities == []` |
| generation health | audit trail contains channel `TIMEOUT`/`[ERROR]` lines covering **all** channels, or `decision_maker → 0 priorities` while signals existed |
| temporal sanity | any priority anchored strictly in the past (backstop on `_drop_past_anchored`) |
| dedup | two priorities share `entity_id` + near-identical titles |

A deterministic FAIL routes the user to **repair** (Stage 3) — it never
reaches the LLM judges and never blocks other users.

### Stage 2 — LLM quality judgment (per-user, single judge + spot-check)

For bundles that pass Stage 1, one judge call per user (not the 5-auditor
panel — that stays at run level) scoring against the evidence the channels
collected: *is the #1 card plausibly this user's most important thing today*
(calendar imminence, deal risk, exec asks), are `why_it_matters`/`Next:`
grounded in the cited signals, is anything obviously more important missing
from the covered signals. Output: `PASS / WARN / FAIL + reasons`, persisted on
the verdict row. WARN delivers (annotated in the verdict); FAIL routes to
repair. The existing 5-auditor + chairman panel runs once over the *aggregate*
(coverage %, empty count, repair count) and owns the ops-admin DM.

### Stage 3 — repair, then never-silent delivery

- FAILed user → one targeted regeneration (`run_priorities_for_user`,
  same path as `/api/jobs/priorities/run-all {"target_email": …}`) → re-check.
- Still failing → user goes on the verdict's `undeliverable` list. Slack
  delivery **skips them loudly**: `users_skipped_empty` counter + emails in
  `SlackDeliveryResult`, and the gate DMs the ops admins the named list ("Jordan Park
  got no briefing today because X") instead of a generic FAIL.
- Delivery reads the gate verdict for today; if the gate never ran, it
  delivers as today (fail-open) but flags `gate_missing` in its result.

### Code changes (by home)

- `rubik_core/priorities/agents/decision_maker.py` — distinguish LLM failure
  from empty decision: `invoke_json` failure raises / returns a sentinel;
  `run_priorities_for_user` marks the run `succeeded=False` when the LLM
  failed *and* signals existed, so nothing persists an empty bundle as
  success. `bundle_to_metadata` gains `generation_status: ok|degraded`
  (degraded = any channel error/timeout).
- `rubik_core/ingestion_council/` — new run kind `priorities_quality` with the
  per-user evidence gatherer (Stage 1) + per-user judge (Stage 2); chairman
  aggregates. Verdict rows gain `per_user` JSON (verdict, reasons,
  repaired, undeliverable).
- `backend/agents/ingestion_council.py` — gate entrypoint
  (`python -m backend.agents.ingestion_council --scope priorities-gate`),
  scheduled 07:30 UTC; runs synchronously (Cloud Run background-task
  throttling) or as a Cloud Run Job if per-user judging pushes wall-clock
  past request limits (~20 users × 1 judge call ≈ minutes — start synchronous,
  promote to Job on evidence, the same path Deep Prep took).
- `backend/agents/slack_delivery.py` — verdict-aware delivery + loud skip
  (counter, names, ops-admin alert). Keep the `emails` targeted re-send.
- Scheduler: add `priorities-quality-gate` (07:30 UTC). The existing
  `ingestion-council-nightly` (08:00) narrows to transcript/EOS/corpus ops QA.

### Non-goals

- No blocking of the *in-app* panel — the gate protects outbound Slack
  delivery; the app already lazy-regenerates.
- No 5-auditor panel per user (cost); one judge per user, panel per run.
- The ops-QA council (transcripts, EOS, weekly) keeps its current shape.

### Rollout

1. Fix the success-shaped empty bundle + loud skip (small PR, immediate value).
2. Gate with Stage 1 deterministic checks + repair loop, delivery wired to it.
3. Stage 2 LLM judge + per-user verdicts, behind `CUBIE_PRIORITIES_GATE_ENABLED`
   (default off → dark-launch: judge + log, don't block, compare a week of
   verdicts against user feedback, then flip).

### Regression tests

- empty bundle from LLM-failure → `succeeded=False`, no HomeCard persisted.
- gate repairs an empty bundle (mock regen) and delivery sends the repaired one.
- undeliverable user appears in `SlackDeliveryResult` + ops-admin alert payload.
- deliberate "quiet day" (zero signals, LLM healthy) still delivers nothing but
  is a PASS with reason `no_signals`, not a FAIL (don't spam repair).
