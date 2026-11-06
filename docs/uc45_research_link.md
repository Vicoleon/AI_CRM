# UC-45 — Customer X-Ray research link in HubSpot

Every Fast Research and Deep Research run stamps the company's HubSpot record
with a link back to its Customer X-Ray page in Cubie.

**Why.** Sales lives in HubSpot, not in Cubie. Research that only exists behind
a Cubie URL nobody has is research nobody reads. A rep opening the account in
the CRM now sees that Cubie has studied this customer, and gets to the brief in
one click.

---

## The one manual step: create the property

This is the only part the code cannot do — Cubie holds no HubSpot *schema*
credentials, only object read/write. Until the property exists, every run logs
one warning and skips the write; nothing else breaks, and no deploy is needed
once you create it. Rollout order does not matter.

In HubSpot: **Settings → Properties → Company properties → Create property**

| Field | Value |
|---|---|
| Object type | Company |
| Group | *(any — "Company information" is fine)* |
| Label | `Cubie Research Link` |
| **Internal name** | **`cubie_research_link`** ← must match exactly (`RESEARCH_LINK_PROPERTY` in `rubik_core/retrieval/hubspot_write.py`) |
| Field type | Single-line text |
| Description | Link to this company's latest Customer X-Ray in Cubie. Written automatically by Cubie — do not edit by hand. |

The internal name is the contract. HubSpot derives it from the label, so check
it in the "Create property" dialog before saving — a label typo yields
`cubie_research_link_2` and the writes will silently 400 forever.

> **Single-line text is the right type, not a workaround.** HubSpot's own
> URL-bearing company properties — `website` and `linkedin_company_page` — are
> both `type: string`. There is no
> separate URL type to reach for. HubSpot renders a `http(s)://` value in a text
> property as a clickable link on the record.

---

## What gets written

```
{FRONTEND_URL}/customers/{hubspot_company_id}/xray
```

The link is **stable per company** — no version, no snapshot id. It always
renders the newest ready research, which has three consequences worth knowing:

1. **It cannot rot.** The property never points at a stale March brief.
2. **The write is idempotent.** The value only ever changes for a company's
   first brief, so the code reads before it writes and skips the PATCH when
   HubSpot already agrees — one write per company, not one per run.
3. **It is knowable before a Deep run starts.** That is what lets the write
   happen inside the request the user triggered, on the `cubie` Service.
   A versioned link would have to be written when the run *finishes* — inside
   the `cubie-xray-deep` Cloud Run Job declared in `deploy/jobs.yaml`. Keeping
   the link stable avoids a CRM write from that background job; verify its
   workspace connector permissions before rollout.

Who is allowed to *open* the link is unchanged: Cubie sign-in is limited to the
company's own domain, and Customer X-Ray is gated to the `customer_xray`
feature audience. A HubSpot user outside that audience sees Cubie's normal
access screen.

---

## When it does and doesn't write

| Situation | Stamped? |
|---|---|
| Fast Research completes with a brief | ✅ |
| Fast Research finds no CRM data (`failed` snapshot) | ❌ — would link to an empty page |
| Deep Research accepted (202) | ✅ — at request time, before the job runs |
| Deep Research rejected as already running (409) | ❌ — the in-flight run already stamped |
| Deep Research is accepted and then fails | ⚠️ stamped. The link resolves to the last good brief, or to "no X-Ray yet" if there was never one. Self-corrects on the next successful run. |
| HubSpot already holds the exact URL | ✅ no-op, no API write |
| Property not created yet / HubSpot down / write token unset | ❌ one log line, run unaffected |

Nothing here can fail a research run. The brief is the deliverable; the CRM
stamp is a courtesy.

---

## Code map

| Piece | Where |
|---|---|
| URL construction, read-before-write, all the skip rules | `rubik_core/xray/crm_link.py` |
| The gated PATCH (`update_company_research_link`) | `rubik_core/retrieval/hubspot_write.py` |
| Call sites (`_stamp_crm_link`, both run endpoints) | `backend/routers/xray.py` |
| Kill switch `XRAY_CRM_LINK_ENABLED` | `rubik_core/config.py` |
| Tests | `backend/tests/test_xray_crm_link.py` |

### Two fences, deliberately separate

`cubie_research_link` is **not** in `company_writable()`. That set is the
outer fence around what Sales Call Capture's transcript extractor may propose,
and every write behind it requires a human to click Approve on that specific
change. This property has no approval step — it carries no model output, just a
URL Cubie derives from its own config — so it gets its own one-element fence
(`RESEARCH_LINK_WRITABLE`). Keeping them apart is what stops "a HubSpot write
with no approval step" from becoming a precedent that spreads to properties
that need one.

### Turning it off

```bash
XRAY_CRM_LINK_ENABLED=false
```

Research still runs; HubSpot is not touched. Clearing `HUBSPOT_WRITE_TOKEN` has
the same effect on this path and every other write, and is the harder gate.
