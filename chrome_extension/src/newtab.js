import { askUrl } from './store.js';
import { mount } from './view.js';

function greeting(firstName) {
  const now = new Date();
  const h = now.getHours();
  const part = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
  const day = now.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  return firstName ? `${day} · ${part}, ${firstName}` : day;
}

const greetingEl = document.getElementById('greeting');
const form = document.getElementById('search');
const input = document.getElementById('q');
const askBtn = document.getElementById('ask');

function draw(ctx) {
  const data = ctx.state.glance?.data;
  const connected = !!ctx.state.auth?.token;
  greetingEl.textContent = greeting(connected ? data?.user?.first_name : '');
  askBtn.hidden = !(connected && data?.can_ask);
}

mount(document.getElementById('cubie'), 'page', draw).then((ctx) => {
  // Enter searches the web with the person's own default search engine.
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) chrome.search.query({ text, disposition: 'CURRENT_TAB' });
  });

  askBtn.addEventListener('click', () => {
    const text = input.value.trim();
    window.location.href = text ? askUrl(ctx.origin, text) : new URL('/chat', ctx.origin).toString();
  });
});
