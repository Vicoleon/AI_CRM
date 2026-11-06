// The service worker: keeps the new tab's copy fresh, owns the key, draws the
// toolbar number, and answers the address bar ("cubie …") and the right-click
// menu.
//
// Quiet by design: it never opens a tab, never shows a notification, never
// plays a sound. The only thing it changes outside the new tab is the number
// on its own icon, and the person can turn that off.

import { CUBIE_ORIGINS, HAS_CONTEXT_MENU, HAS_OMNIBOX, askUrl, getPrefs } from './store.js';

const REFRESH_ALARM = 'refresh';
const REFRESH_MINUTES = 15;
const FETCH_TIMEOUT_MS = 15000;
const BADGE_COLOR = '#7C3AED';
const MENU_ASK = 'ask-cubie';

// ── Talking to Cubie ─────────────────────────────────────────────────────────

async function call(auth, path, init = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    return await fetch(new URL(`/api/browser-extension${path}`, auth.origin), {
      ...init,
      signal: ctrl.signal,
      headers: {
        Authorization: `Bearer ${auth.token}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      },
    });
  } finally {
    clearTimeout(timer);
  }
}

function errorFor(status) {
  if (status === 401) return 'reconnect';
  if (status === 403) return 'not_on';
  if (status === 404) return 'off';
  return 'offline';
}

let inFlight = null;

// One refresh at a time: a burst of new tabs shares the same request.
function refresh() {
  inFlight ??= doRefresh().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

async function doRefresh() {
  const { auth } = await chrome.storage.local.get('auth');
  if (!auth?.token) {
    await updateBadge();
    return { ok: false, error: 'not_connected' };
  }
  let res;
  try {
    res = await call(auth, '/glance');
  } catch {
    await chrome.storage.local.set({ glanceError: { kind: 'offline', at: Date.now() } });
    return { ok: false, error: 'offline' };
  }
  if (!res.ok) {
    const kind = errorFor(res.status);
    const patch = { glanceError: { kind, at: Date.now() } };
    if (kind === 'reconnect') {
      // The key is dead (disconnected in Settings, expired, offboarded).
      // Forget it, keep the address so Connect goes to the same Cubie.
      patch.auth = { origin: auth.origin };
      await chrome.storage.local.remove('glance');
    }
    await chrome.storage.local.set(patch);
    await updateBadge();
    return { ok: false, error: kind };
  }
  const data = await res.json();
  await chrome.storage.local.set({ glance: { data, fetchedAt: Date.now() } });
  await chrome.storage.local.remove('glanceError');
  await updateBadge();
  return { ok: true, firstName: data?.user?.first_name };
}

async function updateBadge() {
  const [{ auth, glance }, prefs] = await Promise.all([
    chrome.storage.local.get(['auth', 'glance']),
    getPrefs(),
  ]);
  const n = auth?.token ? glance?.data?.counts?.overdue || 0 : 0;
  const show = prefs.badge && n > 0;
  await chrome.action.setBadgeBackgroundColor({ color: BADGE_COLOR });
  await chrome.action.setBadgeText({ text: show ? (n > 9 ? '9+' : String(n)) : '' });
  await chrome.action.setTitle({
    title: n > 0 ? `Cubie: ${n} ${n === 1 ? 'promise' : 'promises'} past due` : 'Cubie',
  });
}

// ── Messages from the pages and the Connect bridge ───────────────────────────

async function onConnect(msg, sender) {
  // Only the bridge, running on a Cubie page this extension trusts.
  if (sender.id !== chrome.runtime.id || !CUBIE_ORIGINS.includes(sender.origin)) {
    return { ok: false, error: 'This page cannot connect Cubie for Chrome.' };
  }
  if (typeof msg.token !== 'string' || !msg.token.startsWith('cbx_')) {
    return { ok: false, error: 'Cubie sent a key this extension does not recognise.' };
  }
  await chrome.storage.local.set({
    auth: {
      token: msg.token,
      origin: sender.origin,
      connectionId: msg.connectionId,
      connectedAt: Date.now(),
    },
  });
  await chrome.storage.local.remove(['glance', 'glanceError', 'hiddenOn']);
  const result = await refresh();
  if (!result.ok) {
    // The Connect page deletes a key the extension did not confirm, so keep
    // ours in step: forget it and let the person press Connect again.
    await chrome.storage.local.set({ auth: { origin: sender.origin } });
    return { ok: false, error: 'Cubie did not answer. Reload this page and try again.' };
  }
  return result;
}

async function onDone(msg) {
  const { auth } = await chrome.storage.local.get('auth');
  if (!auth?.token) return { ok: false, error: 'not_connected' };
  let res;
  try {
    res = await call(auth, '/done', {
      method: 'POST',
      body: JSON.stringify({ kind: msg.kind, id: msg.id }),
    });
  } catch {
    return { ok: false, error: 'offline' };
  }
  // 404: this morning's card was rebuilt since the tab loaded. The refresh
  // shows the current list; nothing else to do.
  await refresh();
  return { ok: res.ok || res.status === 404, error: res.ok ? undefined : errorFor(res.status) };
}

async function onSignOut() {
  const { auth } = await chrome.storage.local.get('auth');
  if (auth?.token) {
    try {
      await call(auth, '/disconnect', { method: 'POST' });
    } catch {
      // Signing out locally still works; the key lapses or is removed in Settings.
    }
  }
  await chrome.storage.local.set({ auth: { origin: auth?.origin } });
  await chrome.storage.local.remove(['glance', 'glanceError']);
  await updateBadge();
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handlers = {
    connect: () => onConnect(msg, sender),
    refresh: () => refresh(),
    done: () => onDone(msg),
    signout: () => onSignOut(),
  };
  const handler = handlers[msg?.type];
  if (!handler) return false;
  // Everything but connect comes from the extension's own pages (new tab,
  // popup, options), never from the bridge running inside a Cubie page.
  const fromOwnPage =
    sender.id === chrome.runtime.id && (sender.url || '').startsWith(chrome.runtime.getURL(''));
  if (msg.type !== 'connect' && !fromOwnPage) return false;
  handler().then(sendResponse, () => sendResponse({ ok: false, error: 'failed' }));
  return true; // answer asynchronously
});

// ── Address bar and right-click ──────────────────────────────────────────────

async function cubieOrigin() {
  const [{ auth }, prefs] = await Promise.all([chrome.storage.local.get('auth'), getPrefs()]);
  return auth?.origin || prefs.origin;
}

async function openAsk(question, disposition = 'newForegroundTab') {
  const url = askUrl(await cubieOrigin(), question);
  if (disposition === 'currentTab') {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id) return chrome.tabs.update(tab.id, { url });
  }
  return chrome.tabs.create({ url, active: disposition !== 'newBackgroundTab' });
}

// Both shortcuts are left out of the Web Store build (scripts/
// pack_chrome_extension.py): the store's rules for a new-tab extension allow
// the new tab and a toolbar button, nothing else. Their APIs then do not exist,
// so every use is behind these checks.
if (HAS_OMNIBOX) {
  chrome.omnibox.setDefaultSuggestion({ description: 'Ask Cubie: %s' });
  chrome.omnibox.onInputEntered.addListener((text, disposition) => {
    const question = text.trim();
    if (question) openAsk(question, disposition);
  });
}

if (HAS_CONTEXT_MENU) {
  chrome.contextMenus.onClicked.addListener((info) => {
    if (info.menuItemId !== MENU_ASK || !info.selectionText) return;
    const selection = info.selectionText.trim().slice(0, 500);
    openAsk(`What do we know about this? "${selection}"`);
  });
}

// ── Lifecycle ────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: REFRESH_MINUTES, delayInMinutes: 1 });
  if (HAS_CONTEXT_MENU) {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: MENU_ASK,
        title: 'Ask Cubie about "%s"',
        contexts: ['selection'],
      });
    });
  }
  refresh();
});

chrome.runtime.onStartup.addListener(() => {
  refresh();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REFRESH_ALARM) refresh();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes.prefs) {
    updateBadge();
  }
});
