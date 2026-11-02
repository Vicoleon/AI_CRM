# Proposal Generator

Draft the short client proposal a prospect asks for on a sales call, from the
call itself — in Cubie, from the Sales Calls wrap-up.

## Why

AEs were pasting call transcripts plus pricing and scope notes into a chat
model and asking for a one-page PDF proposal, so a prospect could gauge
interest right after the call and before an SOW is written. The usual fixes on
every draft: fit to one page, add a *Desired Outcomes* section with the goals
the prospect stated, and trim scope the model invented. It took three or four
rounds, and not everything is confirmed on the call. The ask: a form to confirm
the important facts (pricing, specifics) before generating, and a chat attached
to the tool for revisions.

Cubie already detects the moment (the wrap-up's `sow_request` item) and starts
the SOW clock in the Cockpit. This turns the detection into a draft.

## What it does

1. **Brief.** From the wrap-up, *Draft proposal*. Cubie reads the transcript
   and the HubSpot company / contact / deal and fills a structured brief:
   prepared for, service title, engagement summary, scope items (only what was
   discussed), desired outcomes (only what the prospect said, each with the
   verbatim quote and line), pricing lines (only figures that were said), notes,
   timing, open questions. The AE corrects and **confirms**. Nothing is
   generated before that.
2. **Generate.** A one-page proposal — Executive Summary, Scope of Work,
   Desired Outcomes & Key Deliverables, Pricing & Investment, notes, closing —
   as branded PDF or editable Word. The model writes prose keyed to the brief's
   item ids; it cannot add a service or a number (see *The fence*).
3. **Revise.** The AE types an instruction; Cubie appends a new version with a
   one-sentence change summary. Every version is kept and downloadable.

Cubie never sends the document. The AE downloads it and sends it.

## The fence

* `rubik_core/proposals/brief.py::normalize_content` keeps only `scope` /
  `outcomes` entries whose id exists in the brief, fills a missing body from
  the brief's own words, and resets `notes` to the brief's when the count
  differs. Pricing is not in the content at all.
* `render.py` prints the pricing table from the **version's brief snapshot**.
* `normalize_brief` verifies each outcome quote against the transcript: an
  unverifiable one is dropped on extraction, kept-but-unmarked on an AE save
  or AE-instructed revision.
* Every transcript-reading prompt carries the UNTRUSTED INPUT clause the CRM
  Sync and Call Capture prompts use.

## The company's templates and catalog

Templates are each workspace's own data, written by its admins in
**Admin → Company setup** (`/api/company/proposal-templates`). A new workspace
has none, and its proposals are built from the AI-written sections alone until
an admin adds one.

* A template is a named service write-up (`body_md`) plus, optionally, the
  names of rows in the company's product and service catalog that price it
  (`catalog_item_names`). The catalog is the CSV an admin uploads on the same
  tab (`/api/company/catalog/import`): name, short description, category,
  price and unit (both optional), and a link.
* The brief carries `template = {key}`. The brief prompt lists the active
  templates and suggests one from the call; the AE changes it on the form
  (`components/calls/ProposalTemplatePicker.tsx`, which shows a plain empty
  state when the company has no templates yet). A revision keeps the AE's
  choice whatever the editor returns.
* The tailored content and the fence are unchanged. The template's body prints
  after the desired outcomes and before pricing, in the admin's words; the
  writer is told its headings (so it does not repeat them) and never sees or
  edits the copy.
* **Prices never come from prose.** A template's catalog rows become its
  `price_options`, the catalog's list prices verbatim, pinned by
  `test_the_brief_form_listing_offers_active_templates_with_list_prices` in
  `test_proposal_templates.py`. A chip on the form adds one as a pricing line
  with a blank proposal price. A figure an admin typed into `body_md` prints as
  written, the way any of their own copy does.
* A proposal that already names a template keeps printing its write-up after
  an admin stops offering it on new proposals. Once the template is deleted,
  the proposal renders from its AI-written sections alone.
* The letterhead (name, legal name, address, website) comes from the company
  profile on the same tab (`rubik_core/proposals/brand.py`). With no profile
  saved, it says nothing about the company rather than something wrong.

## Where things live

| Piece | Path |
| --- | --- |
| Tables | `rubik_core/database/proposals.py`, in the baseline migration; `ProposalTemplate` and the catalog in `rubik_core/database/models.py` |
| Shapes + validators | `rubik_core/proposals/brief.py` |
| Prompts (steward / writer / editor) | `rubik_core/proposals/prompts.py` |
| Steps + model calls | `rubik_core/proposals/service.py` |
| PDF / Word | `rubik_core/proposals/render.py`, letterhead in `brand.py` |
| Templates | `rubik_core/proposals/templates.py`, `backend/routers/company.py`, `frontend/src/pages/admin/CompanyTab.tsx`, `components/calls/ProposalTemplatePicker.tsx` |
| Catalog | `rubik_core/company/catalog.py` |
| API | `backend/routers/proposals.py` (`/api/proposals/...`) |
| UI | `frontend/src/pages/sales/Proposal.tsx`, `components/calls/DraftProposalCta.tsx` |
| Gates | registry key `proposal_generator`; `PROPOSALS_ENABLED` |
| Cost | AI Cost category `proposal_generator` |
| Help | `frontend/src/pages/help/topics/Proposals.tsx`, `rubik_core/help/content.py` |
| Tests | `backend/tests/test_proposals.py`, `test_proposal_templates.py` |

## Rollout

* Ships with `PROPOSALS_ENABLED=true` in the Service settings applied by
  `scripts/platform/deploy.sh --env <private-env>`.
  Default roster: everyone in the workspace (access follows the work, so the
  Exec-team flag adds no one). The wrap-up route also needs Sales Call
  Capture. Admins narrow either in Admin → Feature Access.
* Model: PRO by default (`PROPOSALS_MODEL` to pin). Roughly one PRO call per
  step; a few dollars per proposal.

## Not in this release

* Attaching the proposal to HubSpot or detecting that it was sent (the
  Cockpit's SOW-sent detector owns that).
* Full SOW or contract language.
* Starting a proposal from anywhere but a captured call.

**Fathom:** a Fathom-recorded call becomes a captured call through
`rubik_core/call_capture/fathom_attach.py`, so an AE's Fathom calls reach the
wrap-up and the Draft proposal button without being started from Today's
Calls. See docs/ENGINEERING_NOTES.md, "Fathom recordings become captured calls".
