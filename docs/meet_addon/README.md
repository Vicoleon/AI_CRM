# Cubie in Google Meet — the call checklist add-on

The per-product call checklist (the call scripts in
`rubik_core/call_capture/scripts.py`) already renders on the Today's Calls row.
This add-on puts the same checklist in Google Meet's side panel, so an AE on a
client call reads it beside the call instead of in another tab.

## The limit that shapes everything: an add-on cannot open itself

Google's Meet Add-ons SDK gives an add-on no way to launch when a meeting starts
or when a participant joins. The supported ways in are:

- the AE opens it from **Activities** (the shapes icon, bottom right) — one click;
- a screen-share banner, when the AE shares a page that calls
  `exposeToMeetWhenScreensharing()` (not our case);
- an install/open prompt when a link tied to the add-on is posted in Meet chat.
  Google's review team decides which links count, and a private add-on skips
  that review, so this is not something to build on.

There is no URL parameter, Calendar setting or API that opens an add-on for the
user (checked against the SDK reference and release notes, 30 Sep 2026). So the
design makes that one click land on the right call with **nothing to choose**:

1. The add-on is installed for the AEs by a Workspace admin, so it is already in
   Activities.
2. For people on the add-on's roster, Join on Today's Calls opens Meet with
   `authuser=<work email>`, so Meet opens on the account the add-on is installed
   on, and the row says where the checklist is (`frontend/src/meetAddon/joinLink.ts`).
3. The panel reads the meeting code from Meet (`getMeetingInfo()`) and asks Cubie
   which call that is. It opens on that client, with the products picked in
   Cubie, already laid out.

**Open decision:** a truly automatic open needs a managed Chrome extension
that clicks Activities → Cubie for the AE. It works only in Chrome profiles the
Workspace admin manages, and it breaks silently whenever Google changes Meet's
screen. Not built; see "If we want it automatic" below.

## How it works

```
Meet loads  /meet-addon/side-panel?meet_sdk=…      (backend/main.py serve_frontend)
  └─ frontend/src/meetAddon/main.tsx               (its own Vite entry, not the SPA)
       ├─ meetSession.ts   createAddonSession → createSidePanelClient → getMeetingInfo
       ├─ addonApi.ts      sign in (Google popup → POST /api/auth/session), own token key
       └─ GET /api/meet-addon/context?meeting_code=abc-defg-hij
              backend/routers/meet_addon.py
                1. today's calendar row with that code (calendar_scan.scan_day)
                   → captured? products ATTACHED to the call : products remembered for the client
                2. else the AE's call session on that code from the last 18h
                3. else "not one of your client calls" + every live checklist to pick from
```

- **A pure read.** No Meet writes, no session rows, no remembered picks. Ticks and
  products added in the panel stay in the panel (browser storage, keyed by meeting
  code and day, so a recurring series does not reopen with last week's ticks).
- **The panel never starts an activity.** `startActivity` would invite every
  participant, the client included.
- **Signing in happens inside the panel.** Browsers partition an embedded page's
  storage by the site around it, so the Cubie tab's session is invisible inside
  Meet. The panel runs the same Google popup + `/api/auth/session` exchange as the
  login page, under its own storage key.
- **Gates.** Admin → Feature Access → *Cubie in Google Meet* (`sales_meet_addon`;
  ships with the Sales Call Capture roster) decides who Cubie answers. The
  `MEET_ADDON_ENABLED` env switch decides whether `/api/meet-addon` exists at all
  (404 when off). The router sits under the copilot `core` parent. Today's Calls
  asks `GET /api/meet-addon/status` before it points anyone at Meet, so while the
  switch is off nobody on the roster sees the hint or the account-pinned Join link.

## Deploying it

Values: the deployment's own project `<GCP_PROJECT_ID>` (number
`<GCP_PROJECT_NUMBER>`, the same project as the deployment's OAuth client) and
public URL `https://<CUBIE_PUBLIC_HOST>`; `docs/meet_addon/deployment.json`
carries the same placeholders, so fill them in before step 4. The side panel
must be served from an origin that Cubie sign-in already works on (the OAuth
client's authorised JavaScript origins), or the Google popup refuses to open.

1. **Ship the code** through the guarded release path:
   `scripts/platform/deploy.sh --env <private-env> --dry-run`, then
   `scripts/platform/deploy.sh --env <private-env>` after approval and the
   workspace freeze check. Set
   `VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER=<GCP_PROJECT_NUMBER>` in that private
   environment; the platform build passes it to the Dockerfile. Without it,
   the panel says it cannot connect.
2. **Switch the API on** through the declarative Service settings in
   `rubik_core/platform/deployment.py`. The current release declaration sets
   `MEET_ADDON_ENABLED=true`; a changed value takes effect on the next guarded
   platform deploy. Set the audience in Admin → Feature Access.
3. **Enable the two Google APIs** in the project:
   `gcloud services enable appsmarket-component.googleapis.com gsuiteaddons.googleapis.com --project <GCP_PROJECT_ID>`
4. **Create the add-on deployment** from `docs/meet_addon/deployment.json`:
   `gcloud workspace-add-ons deployments create cubie-meet --deployment-file=docs/meet_addon/deployment.json --project <GCP_PROJECT_ID>`
   (`replace` instead of `create` to change it later.)
5. **Test-install it for yourself** and try a real call:
   `gcloud workspace-add-ons deployments install cubie-meet --project <GCP_PROJECT_ID>`
   Then in a Meet call: Activities → Cubie. Check the list under "First real call".
6. **Publish as Public (unlisted)** (Google Workspace Marketplace SDK → App
   configuration → Visibility). Never **Private**: visibility cannot be changed
   later, and a Private app can only be installed in the publisher's own
   Workspace, which locks every client out. Store listing needs the name, a
   short description, 128×128 and 32×32 icons
   (`frontend/public/cubie-meet-addon.png` is 256×256; scale it), and support,
   terms and privacy URLs.
7. **Workspace admin installs it** for the AEs (Admin console → Apps → Google
   Workspace Marketplace apps → install for the Sales organisational unit or a
   group). Until then the add-on is not in anyone's Activities.
8. **Check the roster** in Admin → Feature Access → *Cubie in Google Meet*. Both
   halves are needed: installed (Google) and on the roster (Cubie).

A PROD project deployment needs its own deployment, project number and URL.

## First real call — what to check

These cannot be verified outside a real Meet call:

- [ ] The side panel loads (no "refused to connect"). If it does not, Meet is
      framing it through an origin Cubie blocks; nothing in Cubie sets
      `frame-ancestors` today, so this should pass.
- [ ] "Sign in with Google" opens the popup from inside the panel. If the browser
      blocks it, the panel says so; the fix is allowing pop-ups for meet.google.com.
- [ ] After sign-in the panel names the right client and products for a call on
      Today's Calls, and says "not one of your client calls" for an internal one.
- [ ] Closing and reopening the panel keeps the ticks.
- [ ] Record `location.ancestorOrigins` from inside the panel. Once known, set
      `Content-Security-Policy: frame-ancestors` on `/meet-addon/*` to exactly that
      chain (hardening, deliberately not guessed before the first real load).

## If we want it automatic

A Chrome extension, force-installed by the Workspace admin for the Sales
organisational unit, can watch for Meet tabs that Cubie opened (a marker on the
Join link) and click Activities → Cubie once the AE is in the call. Costs and
risks, stated plainly:

- only works in Chrome profiles signed in with the managed company account;
- it drives Meet's own buttons, whose labels change with Google's releases and the
  user's language, so it breaks without warning and must fail visibly;
- it is a second thing to publish, install and maintain.

Recommendation: ship the one-click add-on first, and measure whether AEs open it
before paying for the extension.
