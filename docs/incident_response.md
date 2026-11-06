# Cubie — Incident Response Runbook

_SOC 2 CC7.4 / CC7.5. Companion to `docs/deployment_rollback.md` (the mechanical
"how to revert" steps). This document is the "who, when, and how we decide"._

Last reviewed: 2026-07-22

## Contacts & roles

| Role | Person | Contact | Responsibility |
|------|--------|---------|----------------|
| **Incident Commander / Primary technical** | `<name>` | `<email>` | Owns the response, coordinates the fix, makes the call to roll back. |
| **Secondary technical** | `<name>` | `<email>` | Backup responder; steps in when the IC is unavailable. |
| **Business / executive escalation** | `<name>` | `<email>` | Looped in on Sev-1 and any incident with business, data-privacy, or user-trust impact; owns external/leadership communication. |

Automated monitoring alerts (uptime, 5xx, DB load, scheduler/batch failures)
are delivered to the alert addresses `scripts/setup_monitoring.sh` is given.

## Severity levels

| Sev | Definition | Examples | Target response |
|-----|------------|----------|-----------------|
| **Sev-1** | Service down, data loss/exposure, or a security breach | Site unreachable; database corruption; leaked credentials; confidential data exposed to the wrong users | Immediate. Notify IC **and** the business escalation contact. |
| **Sev-2** | Major feature broken or degraded for many users; no data risk | Chat failing; morning briefings not generating; login errors for a subset | Same business day. IC + secondary. |
| **Sev-3** | Minor / cosmetic / single-user; workaround exists | One integration flaky; a non-blocking error in logs | Next working day. Tracked as normal work. |

## Response workflow

1. **Detect** — from a monitoring alert, a user report, or an engineer.
2. **Declare & classify** — the first responder assigns a severity and becomes
   acting IC until the IC picks it up.
3. **Notify** — Sev-1: message the IC and the business escalation contact
   immediately. Sev-2: the IC (+ the secondary if the IC is out). Sev-3: log it, no page.
4. **Mitigate** — stabilize first, root-cause later. Fastest levers:
   - Roll back the Cloud Run revision (`docs/deployment_rollback.md`).
   - Flip the relevant feature flag off.
   - For a bad migration, follow the alembic-downgrade steps in the rollback doc.
5. **Communicate** — for user-impacting Sev-1/2, the business escalation contact coordinates any message
   to staff/leadership. Keep a running timeline in the incident thread.
6. **Resolve & verify** — confirm via `/health` (git SHA, session backend) and the
   relevant monitoring alert clearing.
7. **Post-incident review** — within 3 business days for any Sev-1/2 (template below).

## Security incidents (special handling)

For a **suspected credential compromise or data exposure**, in addition to the above:

1. **Contain** — rotate the affected secret immediately (Secret Manager), and
   revoke the exposed token/key at its source (Google, Fathom, Slack, HubSpot).
2. **Assess scope** — what could the credential access, and for how long? Check
   `audit_logs`, `login_events`, and Cloud Logging.
3. **Notify the business escalation contact** regardless of confirmed impact — data-privacy incidents
   are a business decision, not just a technical one.
4. **Document** — record what was exposed, when, the containment actions, and the
   remediation, in the post-incident review.

## Post-incident review template

```
Incident: <short title>            Severity: Sev-_
Detected: <when/how>               Resolved: <when>
Impact: <who/what was affected, for how long>

Timeline:
  <time> — <event>

Root cause:
  <what actually caused it>

What went well / what didn't:

Follow-up actions (owner, due date):
  - [ ] ...
```

## Notes

- This is an internal-tool posture. As Cubie's user base or external exposure
  grows, revisit response targets, on-call rotation, and formal SLAs.
- Escalation contacts should be re-confirmed whenever the team changes.
