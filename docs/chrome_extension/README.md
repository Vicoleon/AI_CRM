# Cubie for Chrome — the new tab extension

A Chrome extension that shows a person's day on every new tab: first the
promises they made and have not kept (overdue L10 To-Dos, My To-Dos past their
date, the promises Cubie heard them make), then this morning's top priorities
and the AI headline. Each line opens the right Cubie page; a ✓ ticks it off.

The directive was "not intrusive", so: no notifications, no pop-ups, no sounds,
no tab opened on install. The only thing it changes outside its own new tab is
the number on its own toolbar icon (promises past due), which can be turned off.
The new tab can shrink to promises only or one line, blur until hovered (screen
sharing), or hide until tomorrow.

## Pieces

| Piece | Where |
|---|---|
| The extension (MV3, plain JS, no build step) | `chrome_extension/` |
| API: connect / list / disconnect browsers, glance, done, sign out | `backend/routers/browser_extension.py` (mounted outside `core`) |
| The new tab's payload, one read | `backend/services/browser_glance.py` |
| Per-browser keys (hash only) | `rubik_core/browser_extension/connections.py`, table `browser_connections` |
| Connect page, Settings card | `frontend/src/pages/ChromeConnect.tsx` (`/chrome/connect`), `frontend/src/pages/settings/CubieForChromeCard.tsx` |
| Gate | Admin → Feature Access → *Cubie for Chrome* (`chrome_extension`, everyone by default) + `CHROME_EXTENSION_ENABLED` |

## How sign-in works (and why it is not the Cubie session)

The Cubie session JWT lives in the Cubie tab's `localStorage`, expires after
five hours with no refresh, and Google sign-in refuses to run on a
`chrome-extension://` page. So the extension gets its own key:

```
new tab "Connect to Cubie" ──► Cubie /chrome/connect (normal signed-in page, consent card)
   POST /api/browser-extension/connections ──► {token: "cbx_<workspace>.<random>"}   (shown once; only its SHA-256 is stored)
   page window.postMessage(token) ──► chrome_extension/src/bridge.js (content script, Cubie origins only)
   bridge ──► service worker: stores {token, origin}, fetches /glance, answers {ok}
```

- The key carries a platform-signed workspace slug. The server verifies that
  signature and checks the workspace is active before opening its database.
  Its full SHA-256 is stored only in that workspace's database. The key opens
  only `/api/browser-extension/glance`, `/done` and `/disconnect`.
  `backend/auth.py` refuses a `cbx_` key everywhere else **before** the Google
  path, which would otherwise post it to Google's userinfo endpoint.
- Each connection records the Google-verified hosted domain from the Cubie
  sign-in session. Every extension call checks that this domain still belongs
  to the workspace, so removing a domain revokes its keys, including for
  people whose email uses an alias domain. Old `atlx_` keys require reconnecting.
- Rotating `CUBIE_SESSION_SECRET` also invalidates all extension keys.
- Each call re-applies the department lockout and the roster, so moving
  someone off the roster takes effect on the next refresh (403).
- A key lapses after `CHROME_EXTENSION_TOKEN_TTL_DAYS` (90), is revoked by
  Disconnect (Settings → General or the extension's options), is replaced when
  the same browser connects again, and is revoked for every browser by the
  offboard step. At most five live keys per person.
- The postMessage hop is visible to other extensions with content scripts on the
  Cubie page. Such an extension could already read the full Cubie session from
  `localStorage`, so the narrower, revocable key adds no exposure.
- The service worker accepts `connect` only from the bridge on a listed Cubie
  origin, and every other message only from the extension's own pages.

## Cost

No model is ever called from these routes. `/glance` reads this morning's stored
priorities card through `home._load_today_priorities` (the read path with the
done / dismissed / snoozed / deferred filters) and **never** starts a run. The
home page's `POST /api/homefeed/priorities` does start the 3–5 minute pipeline
on a miss; the extension must never call it. A miss shows "not ready yet" with
a link to Cubie, whose home page makes the card the normal way.

The service worker refreshes every 15 minutes and when a new tab finds its copy
older than 10 minutes; a burst of tabs shares one request.

## Installing it (by hand, for now)

1. In Chrome, open `chrome://extensions` and turn on **Developer mode**.
2. **Load unpacked** → pick the `chrome_extension` folder of this repo.
3. Open a new tab → **Connect to Cubie** → sign in if asked → **Connect this browser**.

People at a client company do not have this repo. Until the extension is on the
Chrome Web Store, their Cubie admin hands them the package built for their Cubie
address (`scripts/pack_chrome_extension.py --origin https://<CUBIE_PUBLIC_HOST>`,
below), which they unzip and load the same way. The in-app steps
(`/chrome/connect`) and Help say exactly that.

Chrome will ask once whether to keep the new tab page the extension set.
Choosing "Change back" disables the whole extension (Chrome's rule for new-tab
overrides). A force-installed extension (below) is not asked.

The development manifest lets the extension talk to `localhost:5173` only.
That list lives in one place, the manifest's `host_permissions` (the extension
reads its addresses from its own manifest, `src/store.js`); keep the content
script's `matches` the same. All workspaces use one public Cubie origin; the
signed key selects their workspace. The Web Store build swaps the development
origin for that public origin, `--origin https://<CUBIE_PUBLIC_HOST>` or
`CUBIE_PUBLIC_ORIGIN` (`scripts/pack_chrome_extension.py`).

## Rolling it out

People who are not technical cannot install it themselves: on Windows and
macOS Chrome blocks extensions that do not come from the Chrome Web Store, and
"Load unpacked" needs Developer mode. So it is published on the Web Store as
**Public (unlisted)**, never Private (Private can't be changed later and would
lock every other company out), and pushed to people by each Workspace admin;
each person then only presses Connect.

- The upload: `python3 scripts/pack_chrome_extension.py --origin https://<CUBIE_PUBLIC_HOST>`
  → `dist/cubie-for-chrome-<version>.zip`.
- The listing, privacy answers, images and the publish / force-install steps
  belong in the go-live runbook (Phase 2); none is published yet.
- The public privacy policy the listing links to:
  https://<CUBIE_PUBLIC_HOST>/chrome-privacy.html (`frontend/public/chrome-privacy.html`).

Being installed (Google) and being on the roster (Admin → Feature Access →
*Cubie for Chrome*) are separate; both are needed.

## Verified

- `backend/tests/test_browser_extension.py` (keys, gates, glance contents, ✓
  routing).
- The real unpacked extension driven in Chromium against a stand-in Cubie
  (Connect handoff, refresh, badge, render in light and dark, Undo, compact,
  one line, blur, hide for today, popup, options, a revoked key): 32 of 32
  checks passed on 2026-10-15. Not yet run against a deployed Cubie.

## Not built yet (ideas, roughly by value)

- A heads-up before a meeting that has a prep brief. Needs a cheap "today's
  meetings" read: the Daily Briefing (`POST /api/homefeed/generate`) runs a
  model on every call, so it cannot be reused from a new tab.
- "Save as a promise" from selected text (feeds My To-Dos, whose rows are typed
  by hand today).
- A meeting-safe mode that hides details while the calendar says you are in a
  call, instead of the manual blur.
