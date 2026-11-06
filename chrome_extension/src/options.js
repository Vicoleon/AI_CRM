import { CUBIE_ORIGINS, HAS_CONTEXT_MENU, HAS_OMNIBOX, getPrefs, getState, setPrefs } from './store.js';

// The production address is the deployment's own (scripts/pack_chrome_extension.py
// --origin) and is shown as its host; only the development one gets a label.
const ORIGIN_LABELS = {
  'http://localhost:5173': 'Local development (localhost:5173)',
};

const $ = (id) => document.getElementById(id);
const originSelect = $('origin');

for (const origin of CUBIE_ORIGINS) {
  originSelect.append(new Option(ORIGIN_LABELS[origin] || new URL(origin).host, origin));
}
// The Web Store build allows one address only: nothing to choose, so no picker.
originSelect.closest('label').hidden = CUBIE_ORIGINS.length < 2;
// ...and has neither shortcut, so the section only lists what this build has.
$('shortcut-omnibox').hidden = !HAS_OMNIBOX;
$('shortcut-menu').hidden = !HAS_CONTEXT_MENU;
$('shortcuts').hidden = !HAS_OMNIBOX && !HAS_CONTEXT_MENU;

async function draw() {
  const [state, prefs] = await Promise.all([getState(), getPrefs()]);
  const connected = !!state.auth?.token;
  const name = state.glance?.data?.user?.first_name;
  const where = new URL(state.auth?.origin || prefs.origin).host;

  $('status').textContent = connected
    ? `Connected${name ? ` as ${name}` : ''} to ${where}.`
    : state.glanceError?.kind === 'reconnect'
      ? 'This browser was disconnected. Connect again to see your day on the new tab.'
      : 'Not connected yet.';
  $('connect-row').hidden = connected;
  $('signout').hidden = !connected;
  originSelect.value = state.auth?.origin || prefs.origin;

  for (const radio of document.querySelectorAll('input[name="density"]')) {
    radio.checked = radio.value === prefs.density;
  }
  $('blur').checked = !!prefs.blur;
  $('badge').checked = !!prefs.badge;
}

originSelect.addEventListener('change', () => setPrefs({ origin: originSelect.value }));

$('connect').addEventListener('click', async () => {
  const { origin } = await setPrefs({ origin: originSelect.value });
  chrome.tabs.create({ url: new URL('/chrome/connect', origin).toString() });
});

$('signout').addEventListener('click', async () => {
  $('signout').disabled = true;
  await chrome.runtime.sendMessage({ type: 'signout' });
  $('signout').disabled = false;
});

for (const radio of document.querySelectorAll('input[name="density"]')) {
  radio.addEventListener('change', () => setPrefs({ density: radio.value }));
}
$('blur').addEventListener('change', (e) => setPrefs({ blur: e.target.checked }));
$('badge').addEventListener('change', (e) => setPrefs({ badge: e.target.checked }));

chrome.storage.onChanged.addListener(draw);
draw();
