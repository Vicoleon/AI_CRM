/*
 * Task 22 local browser acceptance. Copy this script into the Playwright MCP
 * workspace's allowed .playwright-mcp directory, then run
 * browser_run_code_unsafe with that copy as `filename`.
 * The app must be served in dev_mock_auth mode at 127.0.0.1:5174.
 * Google authorization and the Gmail/admin API edges are mocked here. The
 * backend's mailbox ownership and private manager-digest rules have their own
 * API tests; this browser check exercises the rendered owner journey.
 */
async (page) => {
  const origin = 'http://127.0.0.1:5174';
  const browser = page.context().browser();
  if (!browser) throw new Error('Playwright browser is unavailable');
  const context = await browser.newContext();
  const view = await context.newPage();
  const accounts = [];
  const inboxReads = [];
  const blockedIngest = [];
  const blockedMutations = [];
  let enabled = false;
  let savedToggles = 0;
  let oauthRedirects = 0;
  let connectStarts = 0;
  let oauthAvailable = true;

  const json = (route, value, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(value),
  });
  const requireVisible = async (locator, description) => {
    try { await locator.waitFor({ state: 'visible', timeout: 12000 }); }
    catch { throw new Error(`${description} was not visible at ${view.url()}; body=${(await view.locator('body').innerText()).slice(-1200)}`); }
  };
  const requireAbsent = async (locator, description) => {
    try { await locator.waitFor({ state: 'hidden', timeout: 12000 }); }
    catch { throw new Error(`${description} remained visible`); }
  };

  // Register these before the first navigation. Later exact mock routes take
  // precedence; every other API write is refused by the final catch-all.
  await view.route('**/api/**', async (route) => {
    const request = route.request();
    if (['GET', 'HEAD', 'OPTIONS'].includes(request.method())) return route.continue();
    blockedMutations.push(`${request.method()} ${new URL(request.url()).pathname}`);
    return json(route, { detail: 'Browser fixture blocked an unrelated write' }, 403);
  });
  await view.route(/\/api\/(?:ingest(?:\/|$)|fathom\/ingest(?:\/|$))/, async (route) => {
    blockedIngest.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    return json(route, { detail: 'Browser fixture disabled automatic ingestion' }, 403);
  });
  await view.route('**/api/users/me', async (route) => {
    const response = await route.fetch();
    const profile = await response.json();
    profile.features = { ...profile.features, page_tours: false, fathom_auto_ingest: false };
    profile.last_ingest = new Date().toISOString();
    await json(route, profile, response.status());
  });
  await view.route('**/api/users/workspace/settings', async (route) => {
    const request = route.request();
    if (request.method() === 'PATCH') {
      enabled = JSON.parse(request.postData() || '{}').multi_gmail_enabled === true;
      savedToggles += 1;
      return json(route, { multi_gmail_enabled: enabled });
    }
    const response = await route.fetch();
    const config = await response.json();
    config.settings = { ...config.settings, multi_gmail_enabled: enabled };
    return json(route, config, response.status());
  });
  await view.route('**/api/users/workspace/mappings', async (route) => {
    if (route.request().method() === 'PUT') return json(route, {});
    return route.continue();
  });
  await view.route(/\/api\/email\/accounts(?:\/[^?]*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith('/connect') && request.method() === 'POST') {
      if (!enabled) return json(route, { detail: 'Disabled by admin' }, 403);
      connectStarts += 1;
      return json(route, { authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?mock=cubie' });
    }
    if (request.method() === 'DELETE') {
      const id = pathname.split('/').pop();
      const index = accounts.findIndex(account => account.id === id);
      if (index < 0) return json(route, { detail: 'Missing' }, 404);
      accounts.splice(index, 1);
      return route.fulfill({ status: 204, body: '' });
    }
    if (request.method() === 'GET') {
      return json(route, {
        enabled,
        oauth_available: oauthAvailable,
        default_account: 'dev@acme.example',
        accounts,
        ai_provider: 'vertex_managed',
        ai_model: 'gemini-2.5-flash',
        ai_provider_fingerprint: 'mock-route-v1',
      });
    }
    return json(route, { detail: 'Unexpected account request' }, 405);
  });
  await view.route('https://accounts.google.com/**', async (route) => {
    oauthRedirects += 1;
    if (accounts.length < connectStarts) {
      const id = `extra-${connectStarts}`;
      accounts.push({ id, email: `private${connectStarts}@example.test`, status: 'connected' });
    }
    await route.fulfill({
      status: 302,
      headers: { location: `${origin}/settings?tab=email&gmail_connection=connected` },
      body: '',
    });
  });
  await view.route('**/api/email/inbox?*', async (route) => {
    const id = new URL(route.request().url()).searchParams.get('account_id');
    inboxReads.push(id);
    const label = id === 'extra-1' ? 'First' : id === 'extra-2' ? 'Second' : 'Wrong';
    return json(route, {
      account_id: id,
      items: [{
        thread_id: 'same-thread-id',
        subject: `${label} private mailbox message`,
        sender: 'Fixture <sender@example.test>',
        snippet: 'Only the selected mailbox should show this.',
        date: '2026-11-02T12:00:00Z',
        is_unread: true,
      }],
    });
  });
  await view.route('**/api/homefeed/priorities', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const priorities = enabled ? accounts.map((account, index) => ({
      id: `mock-card-${index}`,
      title: `Follow up in ${account.email}`,
      why_it_matters: 'The owner has a reply to make.',
      urgency: 'medium',
      source_channels: ['gmail'],
      source_links: [],
      suggested_action: 'Review the message',
      entity_kind: 'thread',
      entity_id: `mock-thread-${index}`,
      attrs: {
        gmail_mailbox: account.email,
        gmail_account_id: account.id,
        personal_gmail: true,
        private_gmail_context: true,
      },
    })) : [];
    return json(route, {
      generated_at: '2026-11-02T12:00:00Z',
      headline: '',
      priorities,
      revision_count: 0,
      last_critique_score: null,
    });
  });

  try {
    await view.goto(`${origin}/login`);
    const enter = view.getByRole('button', { name: 'Enter local preview' });
    await requireVisible(enter, 'Local preview sign-in');
    await enter.click();
    await requireVisible(view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Admin' }), 'Admin navigation');

    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Admin' }).click();
    await view.getByRole('navigation', { name: 'Administration' }).getByRole('link', { name: 'Google Workspace' }).click();
    const policy = view.getByRole('switch', { name: 'Allow additional Gmail accounts' });
    await requireVisible(policy, 'Admin Gmail policy');
    if (await policy.getAttribute('aria-checked') !== 'false') throw new Error('Gmail policy did not start off');
    await policy.click();
    await view.getByRole('button', { name: 'Save Settings' }).click();
    if (!enabled || savedToggles !== 1) throw new Error('Admin enable was not saved');

    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Settings' }).click();
    await view.getByRole('navigation', { name: 'Settings' }).getByRole('link', { name: 'Email Preferences' }).click();
    await requireVisible(view.getByRole('button', { name: 'Connect another Gmail' }), 'Connect button');
    for (let number = 1; number <= 2; number += 1) {
      await view.getByRole('button', { name: 'Connect another Gmail' }).click();
      await requireVisible(view.getByText(`private${number}@example.test`, { exact: true }), `Connected account ${number}`);
    }
    if (connectStarts !== 2 || accounts.length !== 2) throw new Error(`Two mock OAuth connections were not completed: starts=${connectStarts}, redirects=${oauthRedirects}, accounts=${accounts.length}, url=${view.url()}`);

    oauthAvailable = false;
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Work' }).click();
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Settings' }).click();
    await view.getByRole('navigation', { name: 'Settings' }).getByRole('link', { name: 'Email Preferences' }).click();
    await requireVisible(view.getByText('Your admin needs to finish Google Gmail connection setup', { exact: false }), 'Missing OAuth setup explanation');
    if (!await view.getByRole('button', { name: 'Connect another Gmail' }).isDisabled()) throw new Error('Connect stayed enabled without OAuth setup');

    oauthAvailable = true;
    accounts[1].status = 'reauth_required';
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Work' }).click();
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Settings' }).click();
    await view.getByRole('navigation', { name: 'Settings' }).getByRole('link', { name: 'Email Preferences' }).click();
    await requireVisible(view.getByText('Google access needs reconnection.', { exact: false }), 'Revoked-token reconnect guidance');
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Work' }).click();
    let mailbox = view.getByLabel('Mailbox');
    await requireVisible(mailbox, 'Productivity mailbox selector with revoked account');
    if (await mailbox.locator('option[value="extra-2"]').count() !== 0) throw new Error('Revoked account remained selectable');
    accounts[1].status = 'connected';
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Settings' }).click();
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Work' }).click();

    mailbox = view.getByLabel('Mailbox');
    await requireVisible(mailbox, 'Productivity mailbox selector');
    await mailbox.selectOption('extra-1');
    await requireVisible(view.getByText('First private mailbox message'), 'First selected inbox');
    await requireAbsent(view.getByText('Second private mailbox message'), 'Second inbox while first selected');
    const firstLink = await view.getByRole('link', { name: /First private mailbox message/ }).getAttribute('href');
    if (!firstLink?.includes('authuser=private1%40example.test')) throw new Error('First inbox link lost mailbox identity');
    await mailbox.selectOption('extra-2');
    await requireVisible(view.getByText('Second private mailbox message'), 'Second selected inbox');
    await requireAbsent(view.getByText('First private mailbox message'), 'First inbox while second selected');
    if (!inboxReads.includes('extra-1') || !inboxReads.includes('extra-2')) throw new Error('Account IDs did not reach inbox reads');

    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Today' }).click();
    await requireVisible(view.getByLabel('Gmail mailbox: private1@example.test'), 'First AI Priorities mailbox label');
    await requireVisible(view.getByLabel('Gmail mailbox: private2@example.test'), 'Second AI Priorities mailbox label');

    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Admin' }).click();
    await view.getByRole('navigation', { name: 'Administration' }).getByRole('link', { name: 'Google Workspace' }).click();
    await policy.click();
    await view.getByRole('button', { name: 'Save Settings' }).click();
    if (enabled || savedToggles !== 2) throw new Error('Admin pause was not saved');
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Settings' }).click();
    await view.getByRole('navigation', { name: 'Settings' }).getByRole('link', { name: 'Email Preferences' }).click();
    await requireVisible(view.getByText('Your admin has paused additional Gmail accounts.', { exact: false }), 'Admin pause explanation');
    await requireAbsent(view.getByRole('button', { name: 'Connect another Gmail' }), 'Connect button while paused');
    await view.getByRole('button', { name: 'Disconnect private1@example.test' }).click();
    await requireAbsent(view.getByText('private1@example.test', { exact: true }), 'Disconnected account');
    if (accounts.length !== 1) throw new Error('Owner disconnect did not update the account list');
    await view.getByRole('navigation', { name: 'Areas' }).getByRole('button', { name: 'Work' }).click();
    await requireAbsent(view.getByLabel('Mailbox'), 'Extra mailbox selector after admin pause');

    await view.screenshot({ path: '/tmp/cubie-multi-gmail-browser.png', fullPage: true });
    return {
      status: 'passed',
      admin_policy_saves: savedToggles,
      mock_oauth_connections: connectStarts,
      selected_inbox_reads: inboxReads,
      priority_mailbox_labels: 2,
      remaining_paused_account: accounts[0].email,
      blocked_ingest_requests: blockedIngest,
      blocked_other_mutations: blockedMutations,
      screenshot: '/tmp/cubie-multi-gmail-browser.png',
    };
  } finally {
    await context.close();
  }
}
