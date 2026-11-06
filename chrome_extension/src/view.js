// The glue both the new tab and the popup use: read storage, draw, redraw on
// change, ask the service worker for a fresh copy when ours is old.

import { render } from './render.js';
import { DEFAULT_ORIGIN, STALE_MS, getPrefs, getState, localDay } from './store.js';

// `onDraw(ctx)` runs after every draw, for the page parts outside `root`.
export function mount(root, layout, onDraw = () => {}) {
  let state = {};
  let prefs = {};

  const ctx = {
    layout,
    get state() {
      return state;
    },
    get prefs() {
      return prefs;
    },
    get origin() {
      return state.auth?.origin || prefs.origin || DEFAULT_ORIGIN;
    },
    rerender: () => {
      render(root, ctx);
      onDraw(ctx);
    },
    markDone: (kind, id) => chrome.runtime.sendMessage({ type: 'done', kind, id }),
    hideToday: () => chrome.storage.local.set({ hiddenOn: localDay() }),
    showToday: () => chrome.storage.local.remove('hiddenOn'),
  };

  async function load() {
    [state, prefs] = await Promise.all([getState(), getPrefs()]);
    ctx.rerender();
    return state;
  }

  chrome.storage.onChanged.addListener(() => {
    load();
  });

  return load().then((s) => {
    const fetchedAt = s.glance?.fetchedAt || 0;
    if (s.auth?.token && Date.now() - fetchedAt > STALE_MS) {
      chrome.runtime.sendMessage({ type: 'refresh' });
    }
    return ctx;
  });
}
