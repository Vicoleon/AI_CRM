# What's New — the reader of the Cubie changelog

Design: `docs/superpowers/specs/2026-10-27-cubie-platform-design.md` §4.6.1.

> This file is the design contract the code was built against and its runbook.
> If the code and this file disagree, fix one of them in the same change.

## 1. What it does

RubikSoft writes each release note about Cubie once; every workspace reads it.
In a workspace, people see the notes in the **What's new drawer** (opened from
the sidebar button or the Daily Briefing's header button), a one-line
**spotlight bar** on the Daily Briefing, and — when an admin opts in — **one
Slack post each weekday morning** listing the notes published since the last
post. Each person sees only the notes about features they can use.

Nothing in a workspace drafts, approves or edits a note. The approval flow,
the approver Slack buttons and the drafting from the System Team Knowledge
Base were removed on 2026-10-27 (Phase 1 of the Cubie plan).

Phase 1 ships no changelog yet: the source is empty and the drawer says
"No updates yet." Phase 3 adds the control-database source and the authoring
screen in the Platform area.

## 2. Rules it keeps

- **Filtered to what the reader can use.** A note with no `feature_key` is for
  everyone who has What's New. A note about a feature reaches a person only
  while that feature runs here (its env kill switch is not off) and Feature
  Access lets them use it (`rubik_core/whats_new/audience.py`). A key the
  registry no longer knows hides the note; it never raises.
- **Published means published.** A note dated in the future waits; a source
  that fails reads as an empty drawer, never a 500.
- **Links stay inside the app.** "Try it" only follows a path inside Cubie
  (`changelog.app_path`): a full address, a scheme, `//host` or a backslash is
  dropped.
- **People, never who.** An open (from the Slack link or "Try it") is stored
  as a keyed hash of the entry id and the email, never the email, once per
  person per note per source. No API returns who opened what.
- **Seen is per person, per browser** (`frontend/src/components/whatsNew/memory.ts`).
- **Posting is opt-in and fails closed.** The morning post ships off; a
  settings read failure reads as off.

## 3. The changelog seam (`rubik_core/whats_new/changelog.py`)

- `ChangelogEntry` — frozen dataclass: `id: str` (a short token,
  `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`), `title`, `body`, `feature_key: str | None`,
  `path: str | None`, `published_at: datetime` (naive UTC; an aware value is
  converted).
- `ChangelogSource` — protocol: `list_published() -> list[ChangelogEntry]`.
- `EmptyChangelogSource` — Phase 1.
- `get_changelog_source()` — what every reader calls; also the FastAPI
  dependency of the What's New router, so tests swap it with
  `app.dependency_overrides`. Phase 3 returns the control-database source here.

## 4. Data

- `whats_new_opens` (`rubik_core/database/whats_new.py`): `entry_id`, `source`
  (`slack` | `try_it`), `department` (slug at open time), `viewer_key`
  (HMAC-SHA256 hex), `created_at`. UNIQUE `(entry_id, viewer_key, source)` is
  the `ON CONFLICT DO NOTHING` target.
- `workspace_settings`:
  - `whats_new` — the admin's choices: `slack_enabled`, `channel_name`,
    `channel_id`, `bot_in_channel`, `channel_archived`, `tease_enabled`,
    `updated_by`, `updated_at`. Older keys (approvers, approver DMs) are
    ignored on read.
  - `whats_new_digest` — the morning post's mark: `{"at", "entry_id"}` of the
    last note a post carried.
  - `whats_new_open_key` — the HMAC key for `viewer_key`, generated once
    (`OPEN_KEY_ROW` in `rubik_core/whats_new/stats.py`).
- The approval-era tables (`whats_new_items`, `whats_new_clicks`) are gone: no
  model maps them and the baseline migration does not create them.

## 5. HTTP contract

Reader router `backend/routers/whats_new.py` (prefix `/whats-new`, mounted on
`app` at `/api`, outside the `core` parent; `require_whats_new` on the router:
kill switch off → 404 "What's New is not switched on.", off the roster → 403):

| Method and path | Returns |
|---|---|
| `GET /api/whats-new` | `{items: [{id, title, body, path, feature_label, published_at}], tease_enabled}`; newest first, at most 60. `?item=<id>` (a Slack link) adds that note when it is older than the 60 but the person may see it |
| `POST /api/whats-new/{entry_id}/open` | `{"source": "slack" \| "try_it"}` → 204; 404 "This update is no longer available." for an unknown, malformed or hidden note; never counted while an admin views as someone else |

Admin router `backend/routers/whats_new_admin.py` (prefix `/admin/whats-new`,
`admin_required` per endpoint, audit action `WHATS_NEW_SETTINGS` on save):
`GET` / `PUT /api/admin/whats-new/settings` with `{slack_enabled, channel,
tease_enabled}`. A `ValueError` from `settings.save()` is a 400 whose detail is
the admin's sentence ("Invite Cubie to #announcements first: type /invite
@Cubie in that channel."). Saving with posting off always works. Extra fields
from an older client are ignored.

Scheduler: `POST /api/agents/whats-new/digest` (`X-Scheduler-Key` or an
admin). Kill switch off → 200 `{"status": "disabled"}`.
`?dry_run=true` returns the exact text and records nothing.

## 6. The morning post (`rubik_core/whats_new/digest.py::post_digest`)

- **Due:** notes after the mark (ordered by `(published_at, id)`), not in the
  future, about a feature that runs here. Oldest first, at most 12 and fewer
  when the text would pass 3,900 characters; the rest wait for the next post.
- **First run:** with no mark yet, the run sets it to now and posts nothing,
  so a workspace is never flooded with old notes.
- **One post at a time:** a Postgres advisory lock for the whole run; the
  loser answers `busy`.
- **Outcomes:** `posted` moves the mark to the last note carried.
  `not_in_channel`, `channel_archived` and `bot_unavailable` keep the mark (the
  notes go out once that is fixed) and stamp what was learned on the
  settings. Any other error or a timeout is `failed` and moves the mark:
  `chat.postMessage` is not idempotent, so a note is lost rather than posted
  twice.
- **Message:** "One new thing in Cubie:" / "N new things in Cubie:", then
  `• *<title>*: <body on one line, cut at 280> <link|See it>`; every piece of
  note text is escaped (`& < >`). The link is
  `<FRONTEND_URL>/whats-new/<id>?src=slack`; without a public `FRONTEND_URL`
  there is no link.
- Phase 3 moves the post onto the workspace's messaging connector.

## 7. Frontend (`frontend/src/components/whatsNew/`)

- `WhatsNewProvider` loads the feed once per page load (and when the full
  drawer opens); `showButton` once the feed has loaded, empty or not.
- `WhatsNewDrawer`: full (modal, on the shared dialog stack) or peek (the
  weekly tease, non-modal, closes after ~5 s). One list, newest first; "No
  updates yet." when empty; "Mark all as seen"; "Try it" is an in-app link
  that records a `try_it` open.
- `SpotlightBar` (Daily Briefing): "<newest unseen title>, plus N more".
- `pages/WhatsNewLink.tsx`: `/whats-new/:itemId` counts a Slack arrival,
  opens the drawer on the note and goes home. `returnTo.ts` remembers that
  link across sign-in.
- Admin → What's New (`pages/admin/WhatsNewTab.tsx`): the posting switch and
  channel, and the tease switch.

## 8. Runbook

- **Switch on:** `WHATS_NEW_ENABLED=true` on the Service. Who sees the drawer:
  Admin → Feature Access (`whats_new`, everyone by default).
- **Morning post:** create the channel in Slack, `/invite @Cubie`, then turn
  posting on in Admin → What's New. The Cloud Scheduler job
  (`whats-new-digest`, weekday mornings, `POST /api/agents/whats-new/digest`
  with the scheduler key) is created outside the repo.
- **Check what would go out:** `POST /api/agents/whats-new/digest?dry_run=true`.
