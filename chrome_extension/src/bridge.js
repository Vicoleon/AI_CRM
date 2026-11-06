// Runs on Cubie's own pages only (manifest content_scripts.matches).
//
// Two jobs:
// 1. Tell the page the extension is here, so /chrome/connect can offer
//    "Connect this browser" instead of install steps, and pass the connection
//    id this browser already holds so a reconnect retires the old key.
// 2. Carry the new key from the Connect page to the service worker. The page
//    posts it to its own window; only a message from this same page and origin
//    is accepted, and the answer goes back the same way.
//
// The page's Cubie session (in its localStorage) is never read.
//
// The protocol is shared with frontend/src/api/browserExtension.ts: the page
// speaks as 'cubie-web', this script as 'cubie-for-chrome'. Both change together.

(() => {
  const root = document.documentElement;
  root.dataset.cubieForChrome = chrome.runtime.getManifest().version;
  root.dataset.cubieForChromeConnection = '';
  chrome.storage.local.get('auth').then(({ auth }) => {
    root.dataset.cubieForChromeConnection = auth?.connectionId || '';
  });

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    const data = event.data;
    if (!data || data.source !== 'cubie-web' || data.type !== 'cubie-for-chrome:connect') return;
    if (typeof data.token !== 'string' || typeof data.connectionId !== 'string') return;

    const reply = (payload) =>
      window.postMessage(
        { source: 'cubie-for-chrome', type: 'cubie-for-chrome:connected', ...payload },
        window.location.origin,
      );

    chrome.runtime
      .sendMessage({ type: 'connect', token: data.token, connectionId: data.connectionId })
      .then((res) => {
        if (res?.ok) root.dataset.cubieForChromeConnection = data.connectionId;
        reply({ ok: !!res?.ok, error: res?.error, firstName: res?.firstName });
      })
      .catch(() => reply({ ok: false, error: 'The extension did not answer.' }));
  });
})();
