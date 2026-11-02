// Draws the Cubie part of the new tab and of the toolbar popup.
//
// Everything from the server goes in as textContent, never HTML, and every
// link goes through resolveHref (http/https only).

import { localDay, parseInstant, resolveHref } from './store.js';

const UNDO_MS = 5000;

// key -> timeout id, for lines ticked off and still inside their Undo window.
// Module-level so a re-render during the window (a refresh landing) keeps them.
const pending = new Map();

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function whenLabel(item) {
  if (item.days_late > 0) return `${plural(item.days_late, 'day')} late`;
  if (item.days_late === 0) return 'due today';
  if (item.overdue) return 'not done yet';
  return '';
}

function timeLabel(value) {
  const d = parseInstant(value);
  return d ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
}

function openLink(href, origin, ctx) {
  const url = resolveHref(href, origin);
  if (ctx.layout === 'popup') chrome.tabs.create({ url });
  else window.location.href = url;
}

function doneButton(key, label, ctx, send) {
  const isPending = pending.has(key);
  return el(
    'button',
    {
      class: `check${isPending ? ' is-done' : ''}`,
      type: 'button',
      'aria-label': isPending ? `Undo: ${label}` : `Mark done: ${label}`,
      title: isPending ? 'Undo' : 'Mark done',
      onclick: () => {
        if (pending.has(key)) {
          clearTimeout(pending.get(key));
          pending.delete(key);
        } else {
          pending.set(
            key,
            setTimeout(() => {
              pending.delete(key);
              send();
            }, UNDO_MS),
          );
        }
        ctx.rerender();
      },
    },
    isPending ? 'Undo' : '',
  );
}

function row({ key, title, meta, when, whenClass, dot, why, href, done }, ctx) {
  const isPending = pending.has(key);
  return el(
    'li',
    { class: `row${isPending ? ' is-pending' : ''}` },
    done || el('span', { class: 'check-spacer', 'aria-hidden': 'true' }),
    el(
      'a',
      {
        class: 'row-main',
        href: resolveHref(href, ctx.origin),
        // The popup closes on navigation; its links open a tab instead.
        target: ctx.layout === 'popup' ? '_blank' : null,
        rel: ctx.layout === 'popup' ? 'noopener' : null,
      },
      dot ? el('span', { class: `dot dot-${dot}`, 'aria-hidden': 'true' }) : null,
      el(
        'span',
        { class: 'row-text' },
        el('span', { class: 'row-title' }, title, meta ? el('span', { class: 'row-meta' }, ` · ${meta}`) : null),
        why ? el('span', { class: 'row-why' }, why) : null,
      ),
      when ? el('span', { class: `row-when ${whenClass || ''}` }, when) : null,
    ),
  );
}

function promiseRows(items, ctx) {
  return items.map((item) => {
    const key = `${item.kind}:${item.id}`;
    const meta = [item.to, item.source].filter(Boolean).join(' · ');
    return row(
      {
        key,
        title: item.title,
        meta,
        when: whenLabel(item),
        whenClass: item.overdue ? 'is-late' : '',
        href: item.href,
        done: item.can_done
          ? doneButton(key, item.title, ctx, () => ctx.markDone(item.kind, item.id))
          : null,
      },
      ctx,
    );
  });
}

function priorityRows(items, ctx) {
  return items.map((item) => {
    const key = `priority:${item.id}`;
    return row(
      {
        key,
        title: item.title,
        dot: item.urgency,
        why: ctx.prefs.density === 'full' && ctx.layout === 'page' ? item.why : '',
        href: item.href,
        done: item.can_done
          ? doneButton(key, item.title, ctx, () => ctx.markDone('priority', item.id))
          : null,
      },
      ctx,
    );
  });
}

function section(label, children) {
  return el('section', { class: 'block' }, el('h2', { class: 'block-label' }, label), children);
}

function message(text, action) {
  return el('div', { class: 'message' }, el('p', {}, text), action || null);
}

function button(label, onclick, cls = 'btn') {
  return el('button', { class: cls, type: 'button', onclick }, label);
}

function link(label, onclick) {
  return el('button', { class: 'link', type: 'button', onclick }, label);
}

function footer(data, ctx) {
  const asOf = timeLabel(data?.priorities?.as_of);
  const stale =
    ctx.state.glanceError?.kind === 'offline' && ctx.state.glance?.fetchedAt
      ? `Couldn't reach Cubie. Showing the copy from ${new Date(ctx.state.glance.fetchedAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.`
      : '';
  return el(
    'footer',
    { class: 'foot' },
    el('span', { class: 'foot-note' }, stale || (asOf ? `Priorities as of ${asOf}` : '')),
    link('Open Cubie', () => openLink('/', ctx.origin, ctx)),
    ctx.layout === 'page' ? link('Hide for today', ctx.hideToday) : null,
    link('Settings', () => chrome.runtime.openOptionsPage()),
  );
}

function connectCard(ctx, text) {
  return message(
    text,
    button('Connect to Cubie', () => openLink('/chrome/connect', ctx.origin, ctx), 'btn btn-primary'),
  );
}

export function render(root, ctx) {
  const { state, prefs } = ctx;
  const data = state.glance?.data;
  const err = state.glanceError?.kind;
  root.replaceChildren();
  root.classList.toggle('is-blurred', !!prefs.blur);

  if (!state.auth?.token) {
    root.append(
      err === 'reconnect'
        ? connectCard(ctx, 'Cubie for Chrome was disconnected. Connect again to see your day here.')
        : connectCard(ctx, "See the promises you haven't kept yet and today's priorities, right here."),
    );
    return;
  }
  if (err === 'not_on' || err === 'off') {
    root.append(
      message(
        err === 'not_on'
          ? "Cubie for Chrome isn't switched on for your team yet."
          : 'Cubie for Chrome is switched off right now.',
        link('Open Cubie', () => openLink('/', ctx.origin, ctx)),
      ),
    );
    return;
  }
  if (!data) {
    root.append(message(err === 'offline' ? "Couldn't reach Cubie." : 'Loading your day…'));
    return;
  }
  if (ctx.layout === 'page' && state.hiddenOn === localDay()) {
    root.append(
      el(
        'p',
        { class: 'one-line' },
        'Cubie is hidden for today. ',
        link('Show it', ctx.showToday),
      ),
    );
    return;
  }

  const promises = data.promises?.items || [];
  const pri = data.priorities || {};
  const overdue = data.counts?.overdue || 0;

  if (prefs.density === 'minimal' && ctx.layout === 'page') {
    const parts = [];
    parts.push(overdue ? `${plural(overdue, 'promise')} past due` : 'Nothing past due');
    if (pri.status === 'ready') parts.push(`${plural(pri.items.length + (pri.more || 0), 'priority', 'priorities')} today`);
    root.append(
      el('p', { class: 'one-line' }, `${parts.join(' · ')}. `, link('Open Cubie', () => openLink('/', ctx.origin, ctx))),
    );
    return;
  }

  if (data.headline) root.append(el('p', { class: 'headline' }, data.headline));

  const promiseLabel = overdue ? `You said you would · ${overdue} past due` : 'You said you would';
  root.append(
    section(
      promiseLabel,
      promises.length
        ? el('ul', { class: 'list' }, promiseRows(promises, ctx))
        : el('p', { class: 'empty' }, 'Nothing past due.'),
    ),
  );
  if (data.promises?.more) {
    root.append(
      el('p', { class: 'more' }, link(`${data.promises.more} more in Cubie`, () => openLink('/todos', ctx.origin, ctx))),
    );
  }

  const showToday = prefs.density === 'full' || ctx.layout === 'popup';
  if (showToday && pri.status !== 'not_included') {
    let body;
    if (pri.status === 'ready' && pri.items.length) {
      body = el('ul', { class: 'list' }, priorityRows(pri.items, ctx));
    } else if (pri.status === 'ready') {
      body = el('p', { class: 'empty' }, pri.generated_count ? "You've cleared today's priorities." : 'Nothing pressing today.');
    } else if (pri.status === 'not_ready') {
      body = el(
        'p',
        { class: 'empty' },
        "This morning's priorities aren't ready yet. ",
        link('Open Cubie to see them', () => openLink('/', ctx.origin, ctx)),
      );
    } else {
      body = el('p', { class: 'empty' }, "Couldn't load today's priorities.");
    }
    root.append(section('Today', body));
    if (pri.more) {
      root.append(el('p', { class: 'more' }, link(`${pri.more} more in Cubie`, () => openLink('/', ctx.origin, ctx))));
    }
  }

  root.append(footer(data, ctx));
}
