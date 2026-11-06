# Telemetry & Observability Conventions

Conventions every new FastAPI router must follow so traces, metrics, and
logs stay grep-able and dashboards remain stable. Pairs with the
existing [logging_conventions.md](logging_conventions.md).

OpenTelemetry is wired up by `setup_telemetry()` in
[`rubik_core/telemetry.py`](../rubik_core/telemetry.py) and invoked once
from `backend/main.py`. FastAPI is auto-instrumented, so every HTTP
request already produces a parent span — manual spans below are for
sub-operations (downstream API calls, LLM hops, DB-heavy stages) that
deserve their own timing.

## 1. Span Naming

Format: `<router>.<verb>` — short, lowercase, one dot.

| Surface | Example |
|---|---|
| Deals router | `deals.list`, `deals.brain_take`, `deals.advance_stage` |
| Chat router | `chat.send`, `chat.feedback`, `chat.share` |
| Notifications | `notifications.list`, `notifications.mark_read` |
| Search palette | `search.global`, `search.suggest` |

The `<router>` segment maps 1:1 to the FastAPI router file
(`backend/routers/<router>.py`). The `<verb>` is the action the
endpoint performs, not the HTTP method — `brain_take`, not `get`.

Sub-spans inside a request use the same prefix:
`deals.brain_take.hubspot_fetch`, `deals.brain_take.llm_summarize`.

## 2. Required Span Attributes

Every manually-opened span sets:

| Attribute | Required | Notes |
|---|---|---|
| `actor_email` | always | from `get_current_user`; redacted in logs as needed |
| `target_kind` | when scoped | e.g. `"deal"`, `"meeting"`, `"asset"` |
| `target_id` | when scoped | the record's stable ID (HubSpot dealId, doc_id, …) |
| `cache_hit` | for cached reads | `true` / `false` — drives Brain's-take refresh dashboards |

Do **not** put the user's name, raw query text, or LLM output into
attributes. Long strings inflate trace storage and PII leaks; route
those through `interaction_log` instead.

## 3. Counter / Metric Naming

Format: `<router>_<action>_total` (snake_case, `_total` suffix per
OpenTelemetry / Prometheus convention).

Examples:

- `deals_brain_take_requests_total`
- `deals_advance_stage_total`
- `chat_feedback_thumbs_down_total`
- `notifications_dismiss_total`

Histograms follow the same naming with the matching unit suffix:
`deals_brain_take_duration_seconds`.

## 4. When to Write to `interaction_log`

`rubik_core.database.models.InteractionLog` is the audit trail for
**LLM-facing actions**. Write a row whenever a user message, draft, or
agent run hits an LLM — chat, email draft, Brain's take refresh,
priorities pipeline, search re-rank, etc. The existing template in
[`backend/routers/chat.py`](../backend/routers/chat.py) (around the
`InteractionLog(...)` instantiation post-orchestrator) is the
reference.

Required fields:

- `user_email`
- `user_input` (the raw prompt or trigger context)
- `system_prompt` (the assembled system message, or a compact route
  description like `"Intent: X | Backend: Y"`)
- `llm_output` (the model's reply, or the structured output's JSON)
- token counts (`prompt_tokens`, `completion_tokens`, `total_tokens`)
- `duration_ms`
- `successful` + `error_message` on failure

Skip `interaction_log` for non-LLM calls — pure CRUD, HubSpot fetches,
calendar list reads. Those are covered by OpenTelemetry spans alone.

## 5. Admin Context

`set_admin_context(True/False)` from `rubik_core.telemetry` flips a
ContextVar that drives the admin-only JSON log formatter. The reset
middleware in `backend/main.py` already clears the flag between
requests; routers that elevate to admin (e.g. system jobs hitting an
internal endpoint) should call `set_admin_context(True)` at the top of
the handler and rely on the middleware to reset.

## 6. Cloud Trace Dashboards

When a new router lands, add its span names to the matching Cloud
Trace dashboard. Dashboards are provisioned with the rest of the
infrastructure (Phase 2's deploy scripts), never by hand.

## 7. Minimal Span Template

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


@router.get("/{deal_id}/brain-take")
async def brain_take(deal_id: str, user=Depends(get_current_user)):
    with tracer.start_as_current_span("deals.brain_take") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("target_kind", "deal")
        span.set_attribute("target_id", deal_id)

        cached = await load_cached(deal_id)
        span.set_attribute("cache_hit", cached is not None)
        if cached:
            return cached

        result = await compute_brain_take(deal_id, user)
        # ... InteractionLog row written here because compute_brain_take()
        # called an LLM ...
        return result
```

Keep manual spans to one per logical phase. A handler that does five
quick CRUD reads does not need five spans — the auto-instrumented
parent span is enough.
