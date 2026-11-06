# AI Priorities cost optimizations — future work

Requested by Jose on 2026-11-06 for later. These are two separate work items,
after the RubikSoft platform bootstrap. Implementation and post-deployment
measurements are deferred; no follow-up date is scheduled in this checkpoint.
The code observations below came from Jose's supplied specifications; verify
them against the branch and provider routes that exist when work begins.

## Shared preparation and boundaries

- Work from a clean checkout of the published repository, branching from a
  freshly fetched `origin/main`; do not assume local main is current or publish
  the private historical checkout's ancestry.
- Follow `AGENTS.md`, `CLAUDE.md` and `docs/ENGINEERING_NOTES.md` as applicable.
  Current package paths use `rubik_core/`.
- Capture a 14-day baseline from the intended workspace's `llm_usage_events`
  for `feature='ai_priorities'`, grouped by provider/model: calls,
  `prompt_tokens`, `cached_tokens`, `cost_usd`, cached/prompt ratio, and cost per
  priorities run. Preserve the exact measurement window and run denominator.
- Keep card output unchanged. Use captured runs in `rubik_core/priorities/replay.py`
  and `rubik_core/priorities/evals/` to compare baseline and candidate behavior.
- Preserve workspace isolation, owner-only personal-Gmail context, provider
  consent, provenance, and the `ai_priorities` cost attribution.
- Run `ruff check --fix . && ruff format .` and meaningful focused checks.
  Update Help in both its frontend topic and `rubik_core/help/content.py`.
  No tour or video: these changes have no user-visible interface.
- Never deploy Tuesday 08:00–12:00 America/Costa_Rica, or during any applicable
  workspace freeze and its 20-minute lead. Use the guarded release procedure.

## 1. Make Vertex prompt caching hit through prompt order

### Goal and eligibility gate

Reduce AI Priorities input-token cost by putting identical prompt prefixes
first so Vertex implicit caching can discount them. Do not introduce explicit
cache creation, storage fees or TTL lifecycle management in this work item.

Before building, resolve `gemini-pro` and `gemini-flash` through `_MODEL_KEY_MAP`
and current settings/routes. Verify the current official Vertex documentation
for each resolved model's implicit-cache availability, minimum prompt size and
TTL. **If implicit caching is unavailable, stop and report.** Explicit
`caches.create` is a separate proposal. Apply this change only to compatible
Vertex routes; do not assume another provider shares Vertex cache behavior.

### Supplied code observations to recheck

- Priorities model calls use `LLMJsonClient.invoke_json` in
  `rubik_core/priorities/agents/base.py`. The prompt is passed as one
  `contents=prompt` string, without a `system_instruction`.
- Prompts start with `TemporalContext.block()` from `priorities/temporal.py`.
  Its date, timezone, minute and PTO text vary by user/run, preventing a shared
  static prefix. A revision within one run reuses the temporal block.
- Temporal prepending was identified in the channel base, summarizer,
  decision-maker and critic. The decision-maker then places `_META_RULES_PROMPT`
  (two variants from `plate_kinds`), maximum priorities, reader, department
  playbook, plate, about-me, muted items, WGLL, memory, narrative, themes,
  entities, signals and the focus/revision/zero-repair tail.
- `batch_runner.py` assigns sorted email addresses round-robin to shards.
- `rubik_core/cost/usage.py` already reads `cached_content_token_count` into
  `llm_usage_events.cached_tokens`; pricing has `cached_input` rates.

### Proposed implementation

1. Record the shared baseline before editing prompts.
2. Split `TemporalContext.block()` into static text (including the past-is-past
   rule) and dynamic text (date, timezone, local time, PTO).
3. For summarizer, decision-maker and critic, use this layout:

   `static rules → department playbook → dynamic temporal → user context → run data → revision tail`

   User context includes reader, about-me, muted, WGLL and memory. Run data
   includes narrative, themes, entities and signals. **Temporal context must
   remain above the signals.** Preserve every rule and its meaning.
4. Keep deterministic shard assignment unchanged. Only after assigning each
   shard, order its users by `(department, email)` so shared playbooks are
   adjacent.
5. Consider channel-extractor prompt reordering only if their prompts exceed
   the verified cacheable minimum. Gmail's early user email could move below
   static instructions. Probably skip channels if the expected benefit is small.

### Acceptance

- The date-first layout previously fixed suggestions to prepare for meetings
  that had already happened (2026-06-25 incident). Replay captured runs before
  and after; compare judge scores and `_drop_past_anchored` drop counts.
  Any regression surfacing past events blocks merge.
- Update position assertions in `backend/tests/test_priorities_temporal.py`
  deliberately; retain strong checks on the TEMPORAL CONTEXT block and its
  position above signals.
- `test_channel_prompts_contain_no_judgment_words` stays green.
- Three days after an approved deployment, repeat the same measurement and
  report cached-token share and cost per run. Arrange that follow-up when the
  actual deployment date is known.

## 2. Stop re-extracting unchanged signals

### Goal and spend gate

Reduce Flash channel-extractor spend without changing card output. First use
the shared 14-day baseline to measure Flash's share of `ai_priorities` spend.
**If it is below approximately 15%, implement Part 1 only and report back.**

### Supplied code observations to recheck

- `BaseChannelAgent.run()` fetches signals, deterministically parses computed
  fields such as `idle_days`, builds the prompt, prepends temporal context,
  invokes Flash, merges allowed unset model attrs through `_merge_model_attrs`,
  then recomputes mentioned-date tense in `_normalize_mentioned_dates` while
  preferring absolute `resolved` dates.
- Only Gmail, Meet and Slack allow model-written attrs. Gmail's `thread_attrs`
  cover asks, request ownership, reader's last-message kind, mentioned dates and
  amounts. Meet covers commitments by the user and open questions. Slack covers
  mentioned dates.
- Calendar, Drive, HubSpot, EOS and Tickets have empty `model_attr_keys`; their
  Flash call contributes a one-sentence summary/legacy highlights used by the
  summarizer as narrative. Inherited and Promises already skip the LLM.
- Tickets overrides `run()` with an attr fence; preserve it.
- There is no persistent extraction cache. An in-process cache would not
  survive separate daily Cloud Run Job executions.

### Part 1: deterministic summaries for summary-only channels

- Add an environment flag in `rubik_core/config.py` for Calendar, Drive,
  HubSpot, EOS and Tickets to build a summary in code (counts and a few titles,
  following the Inherited channel's style), then skip the extractor LLM call.
- Keep the Tickets attr fence intact.
- A/B captured runs through replay/eval before enabling the flag; those
  summaries still feed the summarizer and can affect downstream behavior.

### Part 2: persistent per-signal cache for Gmail and Meet

Only proceed if the spend gate warrants it. Slack is optional because its
24-hour window seldom repeats.

- Fingerprint the stable content actually rendered for each signal, excluding
  time-relative computed fields such as `idle_days`. Verify the exact rendering
  first: Gmail candidates include thread ID, last-message ID and message count;
  Meet candidates include transcript-file ID and `modifiedTime`.
- Use a workspace-isolated cache keyed by user email, channel, signal ID,
  fingerprint, static extractor prompt-version hash and resolved model ID.
  Respect provider-route boundaries and existing personal-mail approval.
- Look up every signal and send only misses to `build_prompt` and the LLM.
  Merge cached patches through the **same** `_merge_model_attrs` path, then
  `_normalize_mentioned_dates`, so today's tense is recomputed. A cached patch
  must never replace a computed attr.
- Store the patch as emitted by the model. Absolute `resolved` dates remain
  stable; a missing `resolved` value reads as not-past under the existing path.
- Select the partial-hit summary strategy through replay/eval: misses plus an
  “N unchanged threads not shown” line, or an appropriate deterministic summary
  when there are zero misses.
- Add tenant table `priority_extraction_cache`: unique cache key, patch JSON,
  `created_at` and `last_hit_at`. Pick the next Alembic revision from freshly
  fetched origin/main after inspecting every worktree; keep a single head.
- Proposed retention is 35 days, longer than Gmail's 30-day sent window.
  **Ask Jose before building or running any cleanup or backfill on existing
  data. No deletion is authorized by this backlog.** Resolve the precise age
  basis and cleanup policy during that review.

### Invariants and acceptance

- A cache read/write failure falls back to the uncached path and never fails
  the priorities run, matching the contract in `priorities/memory.py`.
- Replay capture records hit/miss counts per channel so saved runs remain
  replayable; cover `test_priorities_replay_capture.py`.
- Emit a content-free per-channel audit line such as
  `gmail extraction cache -> 9 hit / 3 miss`.
- Test full hit, miss, partial hit, prompt-version invalidation, new-message
  invalidation, next-day tense recomputation, database-failure fallback and
  preservation of the Tickets fence.
- Replay/eval must match baseline within noise before enabling the behavior.
- Three days after an approved deployment, report Flash calls and tokens per
  run before/after and cache hit rate. Schedule the measurement only once the
  real deployment date is known.
