# V17 — Neon Glass / Cyber Clay UI Upgrade Notes

**Scope:** complete front-end redesign + one new *real* backend event. No change to login protocol,
account storage, encryption, or matchmaking behaviour. Account credentials, tokens and worker logic are untouched.

---

## 1. What changed on screen

| Layer | V14 (before) | V17 (now) |
| --- | --- | --- |
| Design system | one dark glass skin | `static/css/ui-v17.css` — RGB glassmorphism, claymorphism, clean minimalism, monochrome + duotone, neon accents, cyberpunk framing |
| Background | two blurred blobs | parallax FX stack (`#v17-fx`): aurora wash, perspective neon floor, scanlines, film grain, vignette, floating orbs |
| Depth | flat cards | pointer-driven 3D tilt with glare on stat/clay cards, layered z-depth on heroes and orbs |
| Type | static headings | kinetic typography: animated gradient sweeps on `<em>`, glow pulse while the live stream is attached, glitch overprint on the two hero headings |
| Operator console | 4 views | 4 views **plus a persistent live deck**: real event console, error rack, level-up radar, kinetic fleet ticker |
| Public board | read-only cards | same skin: duotone parallax hero, 3D roster cards, real-number ticker, level-up watch |
| Motion budget | transitions only | GPU-only `transform`/`opacity`/`filter`, `requestAnimationFrame`-throttled, auto-disabled for `prefers-reduced-motion`, `Effects off`, and touch devices |

`ui-v14.css` is still loaded first and keeps the structural grid; **every visual rule is overridden** in
`ui-v17.css`, so nothing falls back to the old look. The older skins (`styles.css`, `control-v10.css`,
`command-v11.css`, `glass-v13.css`) remain on disk for rollback and are still referenced for the
branding/rollback contract that the test-suite checks.

---

## 2. Real telemetry features (nothing simulated)

### 2.1 Real event console
- Source: `GET /api/activity` (admin-authenticated) — the actual `activity_log` table, plus
  `dashboard_sync` frames from `/ws/live`.
- Local filter (all / level-ups / profile gain / UDP join / UDP timeout / queue cooldown / profile-check failed / errors),
  pause, clear view, and `Download .log`.
- Every line shows real values: server timestamp, event type, account label, redacted message.
  The console header prints its source so a viewer can never mistake it for a demo feed.

### 2.2 Error rack (all errors handled)
Captured and shown as dismissible, timestamped entries:
- `api()` failures: network failure, HTTP error, `401` session end (with safe status text only),
- WebSocket transport errors and unparseable frames,
- `window.onerror` and `unhandledrejection`,
- browser offline/online transitions.
Transient network entries auto-clear when the same route succeeds. Errors never include credentials,
tokens or response bodies — the same redaction rules as the server log apply.

### 2.3 Real level-up (new backend event)
- `app/bot_manager.py` → `_on_profile_result()` now compares the **saved** level with the **newly
  returned** level from a genuine server profile check. If it increased, it writes a dedicated
  `level_up` activity record:

  `LEVEL UP recorded from server profile: LV 42 -> LV 43 | total EXP 1,284,930 | next threshold 1,597,330 | saved observation, not a reward promise.`

- `app/main.py` → `GET /api/activity` accepts `event_type=level_up`, and the activity-view filter has a
  **Level up** option.
- The operator UI raises the celebration card from either the server `level_up` event or a real `level`
  increase between two snapshots (fallback, e.g. when the page was closed during the check).
- The public board fires the same card only when a **saved public profile level** really increased.
- Level-up detection is intentionally conservative: it needs a known previous level, so first-load
  snapshots never celebrate, and each account is rate-limited to one celebration per 90 s (120 s public).

### 2.4 Live transport
- Operator page attaches to `/ws/live` (same-origin HttpOnly cookie; no token in the URL).
- If the socket is blocked (some HTTP-only proxies), the deck header switches to `STREAM / POLLING`
  and the client keeps working on a 4.5 s poll with exponential reconnect backoff (1 s → 30 s).
- The 10 s overview poll is unchanged and still drives the roster.

---

## 3. Accessibility & performance

- All animation is skipped under `prefers-reduced-motion: reduce`; `.reveal` elements render in place.
- `Effects off` toggle (top bar **and** Settings → Display) writes `body.low-fx`, which unloads the FX
  stack and freezes tilt. The choice persists in `localStorage`.
- `@supports not (backdrop-filter: blur(2px))` fallback replaces translucent glass with solid surfaces.
- Log stream is capped at 600 rows / 300 rendered; error rack at 60 entries; DOM is rewritten with
  `innerHTML` only when new rows arrive.
- Focus rings, `aria-pressed` states, `role="log"`, `role="progressbar"` and reduced-motion are honoured.

---

## 4. Verification performed

```
python3 -m pytest tests -q                                  → 74 passed
node tests/check_login_ui.cjs                               → success, boot-pending, rejection, offline feedback ✓
node tests/check_card_renderer.cjs                          → valid LV line, fallbacks, escaping ✓
node tests/check_profile_diagnostics.cjs                    → changed / unchanged / failed / never ✓
```

Live smoke test against a running server:
`/health`, `/admin`, `/`, `/api/public/overview`, `/api/accounts`, `/api/activity`,
`/api/activity?event_type=level_up` and a real `/ws/live` handshake (`dashboard_sync` frame received,
`ping` → `pong`) all returned the expected responses.

---

## 5. Preview file

`V17_UI_PREVIEW.html` is a **single self-contained file** (theme + skins inlined, no network) that shows
the login card, live deck, roster grid, the level-up celebration and the public board with **demo data**.
Its banner says so, and its sign-in button is disabled — it is a design reference, not a console.
The real pages remain `/admin` (operator) and `/` (public board).

---

## 6. Rollback

Delete or rename the single line in each page:

```html
<link rel="stylesheet" href="/static/css/ui-v17.css">
```

The pages then fall back to the V14 skin with all structure, IDs and API contracts intact. To also drop
the V17 runtime, remove the `V17` / `V17P` blocks appended to `static/js/app.js` and `static/js/public.js`
and the `#v17-fx`, `.live-dock` and `#levelup-overlay` sections from the two HTML pages.
