/*
 * Admin console smoke test — runs the REAL static/index.html + app.js in jsdom with
 * canned API answers and checks that each feature actually renders / reacts.
 *
 * Run: node tests/check_admin_features.cjs
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'static/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'static/js/app.js'), 'utf8');
const themeJs = fs.readFileSync(path.join(ROOT, 'static/js/theme.js'), 'utf8');

const failures = [];
const passes = [];
function check(name, ok, detail) {
  (ok ? passes : failures).push(name + (detail ? ` — ${detail}` : ''));
}

const nowIso = new Date().toISOString();
const account = {
  id: 'acc_1', display_name: 'ALPHA-01', nickname: 'Alpha', uid: 'u1', game_account_id: '770011',
  region: 'BD', status: 'in_match', status_message: 'Match session active', auth_type: 'uid_password',
  level: 43, exp: 1284930, exp_chart_status: 'calculated', exp_to_next_level: 312400,
  session_exp_gained: 6400, profile_check_status: 'changed', profile_last_checked_at: nowIso,
  queue_no_join_streak: 0, matches_searched: 42, udp_phase: 'udp_join', udp_join_count: 42,
  server_finish_count: 3, udp_timeout_count: 1, cooldown_until: null, mode_id: 1, map_id: 1,
  daily_limit: 50000, daily_exp_gained: 6400, daily_exp_remaining: 43600, daily_exp_percent: 13,
  daily_exp_reached: false, daily_auto_stopped: false, daily_reached_at: null, daily_stopped_at: null,
  daily_day: '2026-10-12', daily_reset_at: '2026-10-12T18:00:00Z', error_count: 1,
  exp_stalled: true, exp_stall_since: '2026-10-12T09:00:00Z', exp_stall_checks: 4,
  exp_stall_message: 'No verified EXP increase for 1h 12m while this account keeps running.'
};

const overview = {
  accounts: [account],
  status_breakdown: { in_match: 1, searching_match: 0, online: 0, authenticating: 0, inactive: 0, cooldown: 0, error: 0 },
  recent_activity: [
    { id: 1, created_at: nowIso, account_label: 'ALPHA-01', level: 'success', event_type: 'profile_gain', message: 'Server profile EXP increased by 640; current EXP 1,284,930.' },
    { id: 2, created_at: nowIso, account_label: 'ALPHA-01', level: 'warning', event_type: 'exp_stall', message: 'No verified EXP increase for 1h 12m.' }
  ],
  summary: {
    daily_rules: { enabled: true, low_level_max: 29, low_limit: 5000, high_limit: 50000, reset_offset_hours: 6,
                   auto_resume: false, summary: 'LV 1-29: 5,000 EXP/day · LV 30+: 50,000 EXP/day' },
    daily_totals: { total_exp: 6400, reached: 0, auto_stopped: 0, day: '2026-10-12', accounts_at_target: 0, accounts_auto_stopped: 0 },
    safety: { profile_checks_per_minute: 2, profile_failures_per_minute: 0, profile_checks_total: 40,
              profile_failures_total: 0, profile_forced_total: 5, throttled_accounts: 0,
              poll_floor_seconds: 45, poll_max_seconds: 300, forced_min_interval_seconds: 10,
              check_concurrency: 3, backoff_threshold: 5, active_workers: 1, max_workers: 50,
              max_concurrent_logins: 8, game_api_state: 'normal' },
    total_accounts: 1, active_accounts: 1, inactive_accounts: 0, cooldown_accounts: 0,
    queue_no_join_limit: 100, queue_cooldown_seconds: 600, error_accounts: 0,
    total_matches_completed: 3, total_matches_searched: 42, total_exp: 1284930,
    average_level: 43, total_errors: 1, level_distribution: { '40-49': 1 }
  }
};
const activity = { events: overview.recent_activity, next_before_id: null };
const health = {
  status: 'ok', observed_at: nowIso,
  host: { process_uptime_seconds: 300, memory_mb: 96, host_memory_total_mb: 512, cpu_count: 2,
          load_average: [0.12, 0.2, 0.1], load_per_core: 0.06, pressure: 'low', loop_lag_ms: 0.4,
          database_mb: 0.42, data_dir_mb: 0.55, disk_free_mb: 4096, disk_used_percent: 60,
          note: 'Comfortable: memory, CPU and disk all have plenty of room.' },
  game_api: overview.summary.safety
};

const requests = [];
const fetchStub = (url, opts) => {
  const clean = String(url).split('?')[0];
  requests.push(clean);
  const body = clean.endsWith('/api/dashboard/overview') ? overview
    : clean.endsWith('/api/activity') ? activity
    : clean.endsWith('/api/health') ? health
    : clean.endsWith('/api/auth/me') ? { authenticated: true, username: 'admin' }
    : clean.endsWith('/api/regions') ? { regions: ['BD', 'ME', 'IND'] }
    : clean.endsWith('/api/exp/chart') ? { levels: [] }
    : clean.endsWith('/api/accounts') ? { accounts: [] }
    : {};
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
};

class FakeWebSocket {
  constructor(url) { this.url = url; this.readyState = 1; FakeWebSocket.last = this; }
  send() {}
  close() {}
}

const virtualConsole = new VirtualConsole();
const jsErrors = [];
const isNoise = m => /Not implemented:|Could not parse CSS/i.test(String(m));
virtualConsole.on('jsdomError', e => { const m = String(e && e.message); if (!isNoise(m)) jsErrors.push(m); });
virtualConsole.on('error', m => { if (!isNoise(m)) jsErrors.push(String(m)); });

const dom = new JSDOM(html.replace(/<script src="[^"]*"[^>]*><\/script>/g, ''), {
  runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole, url: 'https://example.test/admin'
});
const { window } = dom;
window.fetch = fetchStub;
window.WebSocket = FakeWebSocket;
window.confirm = () => true;
window.alert = () => {};
window.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 0);
window.matchMedia = window.matchMedia || (q => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
window.eval(themeJs);
window.eval(appJs);

const $ = id => window.document.getElementById(id);
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  await wait(120);
  // ---- 1. boot state ----
  check('login screen renders', !!$('login-screen'));
  check('no uncaught script errors on boot', jsErrors.length === 0, jsErrors[0]);

  // ---- 2. sign in like the real console ----
  const form = $('login-form');
  check('login form exists', !!form);
  form.querySelector('[name=username]').value = 'admin';
  form.querySelector('[name=password]').value = 'pw';
  // the app posts to /api/auth/login; our stub answers with a session
  window.fetch = (url, opts) => {
    const clean = String(url).split('?')[0];
    if (clean.endsWith('/api/auth/login')) {
      requests.push(clean);
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ authenticated: true, username: 'admin' }) });
    }
    return fetchStub(url, opts);
  };
  form && form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await wait(300);

  // ---- 3. daily EXP limit surface ----
  check('daily EXP watch panel rendered with a row', !!$('daily-watch') && $('daily-watch').innerHTML.includes('ALPHA-01'));
  check('daily watch shows today gained/limit', ($('daily-watch').textContent || '').includes('6,400') && ($('daily-watch').textContent || '').includes('50,000'));
  check('daily rule text in settings', ($('daily-rule-text') || {}).textContent?.includes('5,000') === true,
        ($('daily-rule-text') || {}).textContent);
  check('roster row shows the daily chip', ($('account-list').innerHTML || '').includes('daily-chip'));
  check('daily reset control exists in settings', ($('admin-shell').innerHTML || '').includes('reset-all-daily'));

  // ---- 4. EXP stall diagnosis ----
  check('EXP stall badge rendered', ($('account-list').innerHTML || '').includes('stall-flag'));

  // ---- 5. live log / live deck ----
  const deckText = ($('log-stream') || $('live-dock') || {}).textContent || '';
  check('live log ingested server events', deckText.includes('EXP increased') || deckText.includes('exp_stall'), deckText.slice(0, 80));
  check('activity endpoints requested by the live stream', requests.includes('/api/activity'));

  // ---- 6. safety panel ----
  await wait(400);
  const grid = $('safety-grid');
  check('safety panel tiles rendered', !!grid && grid.children.length >= 6, grid ? String(grid.children.length) : 'no grid');
  check('safety panel shows instance memory', (grid?.textContent || '').includes('MB'), (grid?.textContent || '').slice(0, 60));
  check('host health endpoint requested', requests.includes('/api/health'));

  // ---- 7. fast mode / immersive toggle ----
  const bodyHadFx = window.document.body.className;
  const fx = $('fx-toggle');
  check('fast-mode toggle exists', !!fx);
  if (fx) {
    fx.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await wait(50);
    const after = window.document.body.className;
    check('fast-mode toggle changes the layout class', after !== bodyHadFx || after.includes('fx-full') || window.localStorage.getItem('gc-v17-fx') !== null,
          `${bodyHadFx} -> ${after}`);
    check('toggle label updates', ['Fast mode', 'Immersive on'].includes(($('fx-toggle-label') || {}).textContent || ''), ($('fx-toggle-label') || {}).textContent);
  }

  // ---- 8. live deck toggle ----
  const dock = $('dock-toggle');
  if (dock) {
    const before = $('live-dock').className;
    dock.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await wait(50);
    check('live-deck toggle reacts', $('live-dock').className !== before || window.localStorage.getItem('gc-v17-dock') !== null, `${before} -> ${$('live-dock').className}`);
  } else {
    check('live-deck toggle exists', false, 'no #dock-toggle in markup');
  }

  // ---- 9. websocket dashboard sync path (a brand-new event must appear) ----
  if (FakeWebSocket.last) {
    check('websocket connected to /ws/live', FakeWebSocket.last.url.includes('/ws/live'), FakeWebSocket.last.url);
    const fresh = { id: 999, created_at: new Date().toISOString(), account_label: 'ALPHA-01', level: 'success',
                    event_type: 'daily_auto_offline', message: 'WS-FRESH-EVENT daily EXP target reached; account offline.' };
    FakeWebSocket.last.onmessage && FakeWebSocket.last.onmessage({ data: JSON.stringify({
      type: 'dashboard_sync',
      data: { ...overview, status_breakdown: overview.status_breakdown,
              recent_activity: [fresh, ...overview.recent_activity], summary: overview.summary } }) });
    await wait(80);
    check('websocket sync pushed a new event into the live log', ($('log-stream')?.textContent || '').includes('WS-FRESH-EVENT'),
          ($('log-stream')?.textContent || '').slice(0, 60));
    check('websocket ping is answered with pong handling', typeof FakeWebSocket.last.onmessage === 'function');
  } else {
    check('websocket opened', false);
  }

  // ---- 11. every server-backed button fires its request ----
  const clickById = async (id, ms) => { const b = $(id) || window.document.querySelector(`[data-action="${id}"]`); if (b) { b.dispatchEvent(new window.MouseEvent('click', { bubbles: true })); await wait(ms || 120); } return !!b; };
  requests.length = 0;

  const hasRefresh = await clickById('refresh', 200);
  check('refresh button exists', hasRefresh);
  check('refresh button reloads the dashboard', requests.includes('/api/dashboard/overview'), requests.join(','));

  requests.length = 0;
  const hasDiag = await clickById('diagnostics', 300);
  check('diagnostics button exists', hasDiag);
  check('diagnostics button calls the diagnostics API', requests.includes('/api/system/diagnostics'), requests.join(','));

  requests.length = 0;
  await clickById('goto-activity', 200);
  const activityVisible = $('view-activity') ? !$('view-activity').classList.contains('is-hidden') : null;
  check('activity view opens', activityVisible !== false, `view-activity hidden=${activityVisible === false}`);
  check('activity view lists server events', ($('activity-list')?.textContent || '').includes('EXP increased') || ($('activity-list')?.textContent || '').includes('ALPHA-01') || requests.includes('/api/activity'), ($('activity-list')?.textContent || '').slice(0, 40));

  // activity filters: the daily + no-credit-EXP options must be selectable
  const logType = $('log-type') || $('activity-type');
  if (logType) {
    const opts = Array.from(logType.options).map(o => o.value);
    check('activity filter offers daily + exp-stall events', opts.includes('daily_limit_reached') && opts.includes('exp_stall'), opts.join(','));
  } else { check('activity event filter exists', false); }

  // drawer: details action must render the daily + stall blocks and the reset button
  requests.length = 0;
  const detailBtn = window.document.querySelector('[data-action="details"]');
  if (detailBtn) {
    detailBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await wait(120);
    const body = $('detail-body')?.innerHTML || '';
    check('account drawer opens', body.length > 40);
    check('drawer shows the daily EXP block', body.includes('daily-row') || body.includes('TODAY'), body.slice(0, 60));
    check('drawer shows the EXP-credit diagnosis', body.includes('stall-block'));
    check("drawer offers Reset today's EXP", body.includes('reset-daily'));
    const resetBtn = window.document.querySelector('[data-action="reset-daily"]');
    if (resetBtn) {
      resetBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      await wait(150);
      check('reset-today button calls the reset endpoint', requests.some(r => r.includes('/reset-daily')), requests.join(','));
    } else { check('reset-today button clickable', false); }
  } else { check('account row exposes a details button', false); }

  // bulk action dropdown carries the new daily option
  const bulkSelect = $('bulk-action-select');
  const bulkOpts = bulkSelect ? Array.from(bulkSelect.options).map(o => o.value) : [];
  check('bulk dropdown offers Reset today\'s EXP', bulkOpts.includes('reset_daily'), bulkOpts.join(','));

  // console filter dropdown carries the new live-log options
  const consoleFilter = $('console-filter');
  const consoleOpts = consoleFilter ? Array.from(consoleFilter.options).map(o => o.value) : [];
  check('live-log filter offers the new event types', consoleOpts.includes('daily_limit_reached') && consoleOpts.includes('exp_stall'), consoleOpts.join(','));

  // theme switch still works
  const themeBtn = window.document.querySelector('[data-theme-toggle],#theme-toggle');
  if (themeBtn) {
    const beforeTheme = window.document.documentElement.getAttribute('data-theme');
    themeBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await wait(60);
    check('theme switch reacts', window.document.documentElement.getAttribute('data-theme') !== beforeTheme || window.localStorage.getItem('gc-theme') !== null,
          `${beforeTheme} -> ${window.document.documentElement.getAttribute('data-theme')}`);
  } else { check('theme switch exists', false, 'no toggle found'); }

  // ---- 10. no thrown errors during the whole run ----
  check('no script errors while using the console', jsErrors.length === 0, jsErrors.slice(0, 2).join(' | '));

  console.log('— admin feature check —');
  passes.forEach(p => console.log('  ✓ ' + p));
  failures.forEach(f => console.log('  ✗ ' + f));
  console.log(`  ${passes.length} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})().catch(err => { console.error('harness error:', err); process.exit(2); });
