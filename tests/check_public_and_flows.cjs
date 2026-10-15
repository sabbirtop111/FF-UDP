/*
 * Public board + remaining admin flows smoke test in jsdom.
 * Run: node tests/check_public_and_flows.cjs
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const read = f => fs.readFileSync(path.join(ROOT, f), 'utf8');
const failures = [], passes = [];
const check = (n, ok, d) => (ok ? passes : failures).push(n + (d ? ` — ${d}` : ''));
const noise = m => /Not implemented:|Could not parse CSS/i.test(String(m));
const nowIso = new Date().toISOString();
const wait = ms => new Promise(r => setTimeout(r, ms));

const bot = {
  id: 'acc_1', display_name: 'ALPHA-01', nickname: 'Alpha', region: 'BD', level: 43, exp: 1284930,
  status: 'in_match', status_label: 'UDP joined', status_message: 'Match session active',
  exp_chart_status: 'calculated', exp_to_next_level: 312400, profile_check_status: 'changed',
  profile_last_checked_at: nowIso, session_started_at: nowIso, session_last_exp_gain_at: nowIso,
  udp_join_count: 42, server_finish_count: 3, daily_exp_gained: 6400, daily_exp_limit: 50000,
  daily_exp_reached: false, daily_auto_stopped: false, daily_reset_at: '2026-10-12T18:00:00Z',
  error_count: 0
};
const overview = { observed_at: nowIso, statuses: { 'UDP joined': 1 }, bots: [bot] };

function boot(htmlFile, scripts, requests, fetchMap) {
  const html = read(htmlFile).replace(/<script src="[^"]*"[^>]*><\/script>/g, '');
  const jsErrors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', e => { const m = String(e && e.message); if (!noise(m)) jsErrors.push(m); });
  virtualConsole.on('error', m => { if (!noise(m)) jsErrors.push(String(m)); });
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole, url: 'https://example.test/' });
  const { window } = dom;
  window.fetch = (url, opts) => {
    const clean = String(url).split('?')[0];
    requests.push(clean);
    const body = fetchMap(clean) || {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
  };
  window.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
  window.matchMedia = window.matchMedia || (q => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
  scripts.forEach(code => window.eval(code));
  return { window, jsErrors, $: id => window.document.getElementById(id) };
}

(async () => {
  /* ---------------------------- public board ---------------------------- */
  const pubRequests = [];
  const pub = boot('static/public.html', [read('static/js/theme.js'), read('static/js/public.js')], pubRequests,
                   clean => clean.endsWith('/api/public/overview') ? overview : {});
  await wait(200);
  const cards = pub.$('bot-grid') || pub.$('board') || pub.window.document.querySelector('.card,.bot-card');
  check('public board renders a card', !!cards && pub.window.document.body.innerHTML.includes('ALPHA-01'));
  check('public board requests only the read-only endpoint', pubRequests.every(r => r.includes('/api/public/overview')), pubRequests.join(','));
  const boardText = pub.window.document.body.textContent || '';
  check('public board shows today\'s EXP against the target', boardText.includes('TODAY') && boardText.includes('6,400') && boardText.includes('50,000'), boardText.includes('TODAY') ? 'has TODAY line' : 'no TODAY line');
  check('public board shows the remaining allowance', /EXP left today|Daily target reached/i.test(boardText),
        (boardText.match(/[0-9,]+ EXP left today|Daily target reached/i) || [''])[0]);
  check('public board never exposes admin controls', !boardText.includes('reset-daily') && !pub.window.document.body.innerHTML.includes('RESET DAILY') && !pub.window.document.body.innerHTML.includes('SERVER SAFETY'));
  check('public board has no script errors', pub.jsErrors.length === 0, pub.jsErrors[0]);

  /* ------------------------- admin modal + form flows ------------------------- */
  const requests = [];
  const admin = boot('static/index.html', [read('static/js/theme.js'), read('static/js/app.js')], requests,
    clean => clean.endsWith('/api/dashboard/overview') ? { accounts: [], status_breakdown: {}, recent_activity: [], summary: { safety: {}, daily_rules: {}, daily_totals: {} } }
      : clean.endsWith('/api/health') ? { host: {}, game_api: {} }
      : clean.endsWith('/api/activity') ? { events: [] }
      : clean.endsWith('/api/regions') ? { regions: ['BD', 'ME'] }
      : clean.endsWith('/api/exp/chart') ? { levels: [] }
      : {});
  const w = admin.window, $ = admin.$;
  await wait(150);
  w.fetch = (url, opts) => {
    const clean = String(url).split('?')[0];
    requests.push(clean);
    if (clean.endsWith('/api/auth/login')) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ authenticated: true, username: 'admin' }) });
    if (clean.endsWith('/api/accounts') && opts && opts.method === 'POST') return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ account: { id: 'acc_new' } }) });
    if (clean.endsWith('/api/accounts/bulk-add')) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ created_count: 1, error_count: 0 }) });
    if (clean.endsWith('/api/system/diagnostics')) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ region: 'BD', ok: true, message: 'reachable' }) });
    if (clean.endsWith('/api/accounts/export/metadata')) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ accounts: [] }) });
    const map = clean.endsWith('/api/dashboard/overview') ? { accounts: [], status_breakdown: {}, recent_activity: [], summary: { safety: {}, daily_rules: {}, daily_totals: {} } }
      : clean.endsWith('/api/health') ? { host: {}, game_api: {} }
      : clean.endsWith('/api/activity') ? { events: [] }
      : clean.endsWith('/api/regions') ? { regions: ['BD', 'ME'] }
      : clean.endsWith('/api/exp/chart') ? { levels: [] }
      : {};
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(map), text: () => Promise.resolve(JSON.stringify(map)) });
  };
  const form = $('login-form');
  form.querySelector('[name=username]').value = 'admin';
  form.querySelector('[name=password]').value = 'pw';
  form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await wait(250);

  const click = sel => { const el = typeof sel === 'string' ? w.document.querySelector(sel) : sel; if (el) el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); return !!el; };

  // Add Bot modal -> create
  requests.length = 0;
  check('Add Bot button opens the modal', click('[data-action="open-add"]') && $('modal').classList.contains('is-open') || ($('modal') && $('modal').innerHTML.includes('account-form')));
  const accForm = w.document.getElementById('account-form');
  if (accForm) {
    accForm.querySelector('[name=display_name]').value = 'NEW-01';
    const uidField = accForm.querySelector('[name=uid]');
    const pwField = accForm.querySelector('[name=password]');
    if (uidField) uidField.value = '12345';
    if (pwField) pwField.value = 'secret-pass';
    check('Add Bot form exposes the UID field', !!uidField);
    accForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
    await wait(250);
    check('Add Bot form posts to /api/accounts', requests.includes('/api/accounts'), requests.join(','));
  } else { check('Add Bot form renders', false); }

  // Bulk add modal -> create many
  requests.length = 0;
  click('[data-action="open-bulk"]');
  const bulkForm = w.document.getElementById('bulk-form');
  if (bulkForm) {
    bulkForm.querySelector('[name=raw_lines]').value = '111:aaa\n222:bbb';
    bulkForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
    await wait(250);
    check('Bulk add posts to /api/accounts/bulk-add', requests.includes('/api/accounts/bulk-add'), requests.join(','));
  } else { check('Bulk add form renders', false); }

  // Chart modal
  requests.length = 0;
  click('[data-action="open-chart"]');
  await wait(200);
  check('EXP chart modal renders', ($('modal') || {}).innerHTML?.includes('chart') || requests.includes('/api/exp/chart'), requests.join(','));
  check('modal closes again', click('[data-action="close-modal"]') && !($('modal') || {}).classList?.contains('is-open'));

  // Diagnostics
  requests.length = 0;
  click('[data-action="diagnostics"]');
  await wait(250);
  check('diagnostics writes its result into the panel', ($('diagnostic-result') || {}).textContent?.length > 5, ($('diagnostic-result') || {}).textContent);
  check('diagnostics called the server', requests.includes('/api/system/diagnostics'));

  // Export / import buttons exist and are wired
  check('export button exists', !!w.document.querySelector('[data-action="export"]'));
  check('import button exists', !!w.document.querySelector('[data-action="open-import"]'));

  // Error rack and level-up celebration are wired
  check('error rack exists', !!$('error-rack'));
  check('level-up celebration element exists', !!$('levelup-close'));

  // Sign out
  requests.length = 0;
  click('#logout-btn');
  await wait(200);
  check('sign out calls the logout endpoint', requests.includes('/api/auth/logout') || ($('login-screen') || {}).classList?.contains('is-hidden') === false, requests.join(','));

  check('no script errors in the admin flows', admin.jsErrors.length === 0, admin.jsErrors.slice(0, 2).join(' | '));

  console.log('— public board + admin flows —');
  passes.forEach(p => console.log('  ✓ ' + p));
  failures.forEach(f => console.log('  ✗ ' + f));
  console.log(`  ${passes.length} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})().catch(e => { console.error('harness error:', e); process.exit(2); });
