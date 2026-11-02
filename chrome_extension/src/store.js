// Shared by the service worker and every extension page.
//
// chrome.storage.local holds what belongs to THIS browser: the connection key
// (`auth`), the last new-tab payload (`glance`), the last refresh problem
// (`glanceError`) and "Hide for today" (`hiddenOn`). chrome.storage.sync holds
// the person's preferences, so they follow them to another computer.

// The Cubie addresses this extension may talk to: exactly its
// host_permissions, so the manifest is the one list. The Web Store build
// (scripts/pack_chrome_extension.py --origin) swaps the development address
// there for the deployment's one public origin, and nowhere else. The first
// entry is the default.
export const CUBIE_ORIGINS = chrome.runtime
  .getManifest()
  .host_permissions.map((pattern) => new URL(pattern).origin);
export const DEFAULT_ORIGIN = CUBIE_ORIGINS[0];

// The "cubie …" address-bar keyword and the right-click "Ask Cubie about…"
// exist only in builds whose manifest declares them (not the Web Store build).
const MANIFEST = chrome.runtime.getManifest();
export const HAS_OMNIBOX = !!MANIFEST.omnibox;
export const HAS_CONTEXT_MENU = (MANIFEST.permissions || []).includes('contextMenus');

export const DEFAULT_PREFS = {
  // full: headline, promises and priorities. compact: headline and promises.
  // minimal: one line under the search box.
  density: 'full',
  // The number on the toolbar icon (promises past due).
  badge: true,
  // Blur every line until the pointer is over it (screen sharing).
  blur: false,
  // Where "Connect" sends the person before they are connected.
  origin: DEFAULT_ORIGIN,
};

// A new tab older than this asks the service worker for a fresh copy.
export const STALE_MS = 10 * 60 * 1000;

export async function getPrefs() {
  const { prefs } = await chrome.storage.sync.get('prefs');
  const merged = { ...DEFAULT_PREFS, ...(prefs || {}) };
  // A saved address this build may not talk to (preferences sync between
  // computers) falls back to the default instead of a page Chrome blocks.
  if (!CUBIE_ORIGINS.includes(merged.origin)) merged.origin = DEFAULT_ORIGIN;
  return merged;
}

export async function setPrefs(patch) {
  const prefs = { ...(await getPrefs()), ...patch };
  await chrome.storage.sync.set({ prefs });
  return prefs;
}

export async function getState() {
  return chrome.storage.local.get(['auth', 'glance', 'glanceError', 'hiddenOn']);
}

// The person's local calendar day, YYYY-MM-DD.
export function localDay(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// A Cubie path ("/todos") or an absolute link the server sent (a Gmail
// thread, a Meet recording) → a URL we are willing to open. Anything that is
// not http(s) — a javascript: URL in a bad row, say — opens Cubie instead.
export function resolveHref(href, origin) {
  try {
    const url = new URL(href || '/', origin);
    if (url.protocol === 'https:' || url.protocol === 'http:') return url.toString();
  } catch {
    // fall through
  }
  return new URL('/', origin).toString();
}

// The chat page reads a question from the fragment (#ask=), which, unlike a
// query string, never reaches the server or its request logs.
export function askUrl(origin, question) {
  return `${new URL('/chat', origin)}#ask=${encodeURIComponent(question)}`;
}

// Cubie sends naive UTC timestamps ("2026-10-15T11:04:00"); a bare string
// would be read as local time and be hours off.
export function parseInstant(value) {
  if (!value) return null;
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}
