# ＧＡＭＥＲ-ＣＯＲＰＯＲＡＴＩＯＮ — Multi-Account Web Dashboard & Command Center

**Short name:** ＧＡＭＥＲ-ＣＯＲＰ  ·  **Developer:** ＧＡＭＥＲ ＳＡＢＢＩＲ

**V18.5 multi-account start (why 1 account worked and 4 did not):** starting several accounts at once used to send every login and every match request in the same second. Garena answers that burst with throttling, so the accounts sat in a retry loop and were never matched into a settled game — which is exactly what "one account earns EXP, four together earn nothing" looked like from the console. Two rate guards now pace a fleet: `ACCOUNT_START_STAGGER_SECONDS` (default 4) makes each worker wait its turn before it begins its version check / OAuth / login, and `MATCHMAKING_MIN_GAP_SECONDS` (default 1.2) is a process-wide floor between any two match requests, so four accounts produce roughly the same request rate a single account produces instead of 4×. Both are liftable per deployment (`.env`), the console shows the live numbers in Settings → 06 / Server safety, and a single account behaves exactly as before (its first match request still goes out immediately). Starting a fleet now answers instantly — the staggering happens inside each worker, never in the HTTP response.

**V18.4 "a deploy must never look stale" + live-log fix:** every script and stylesheet the console loads now carries a version (`?v=18.4`), `/` and `/admin` are served `Cache-Control: no-store`, `/static/*` is served `no-cache, must-revalidate` (ETag revalidation), and the running identity is printed in the header (`BUILD 18.4`), in `GET /health` (`build`) and at startup. That combination removes the whole class of "I installed the new version but the site looks old" problems — including the daily limits and safety panel appearing late because a browser kept an old `app.js`. The WebSocket path was also fixed: `dashboard_sync` frames now feed the **live log** (`ingest`) as well as the panels, so an event pushed live (a daily cap stop, a level up, an EXP stall warning) appears on screen instantly instead of waiting for the next poll. Two jsdom harnesses (`tests/check_admin_features.cjs`, `tests/check_public_and_flows.cjs`) now load the real pages and click through every feature — 57 checks — and run inside `pytest` whenever jsdom is installed.

**V18.2 safety rails (both sides):** the dashboard guards the hosting box and the game servers with fixed limits that no feature can bypass. **Game side:** every account reads its profile no faster than `PROFILE_POLL_SECONDS` (default 45 s, 15 s floor), at most `PROFILE_CHECK_CONCURRENCY` reads (default 3) run at the same time across all accounts, an early match-triggered check waits `PROFILE_FORCED_MIN_INTERVAL_SECONDS` (default 10) so bursts cannot become extra requests, and repeated failures make an account back off exponentially up to `PROFILE_POLL_MAX_SECONDS` (default 300) before returning to normal — a refusing game API is retried more slowly, never harder. Match traffic itself is unchanged: the same UDP packets as before, verified byte-for-byte by a test. **Hosting side:** `MAX_ACTIVE_WORKERS` and `MAX_CONCURRENT_LOGINS` cap the work, per-account state is dropped when an account stops, traffic counters are bounded in memory (never persisted per read), the dashboard only re-renders when something really changed, and the safety panel reads its own footprint at most once a minute on the existing tick — no new timers, no extra polling. `GET /api/health` (admin) reports the hosting footprint (memory, load per core, loop lag, data size, disk) plus the game-API traffic picture (reads per minute, failures, throttle state) and a plain-language note; the cheap unauthenticated `/health` stays for hosting platforms. `GET /api/dashboard/overview` carries the same numbers as `summary.safety` for the **Server safety** panel in Settings → section 06.

**V18.1 "no EXP from the server" diagnosis:** an account that keeps joining matches while the game server never credits EXP is now reported instead of left a mystery. Every profile read (`GetLoginData`) is compared against the saved profile; if the value does not move for `EXP_STALL_MINUTES` (default 30) while the account is running, the dashboard raises an **EXP STALL** badge on the roster row, a full explanation block in the account drawer, the activity event `exp_stall`, and the count of affected accounts in the Daily EXP watch header. The message carries the exact evidence (current EXP, level, UDP joins, matches ended, checks performed) and states the truth behind it: EXP is credited by the game server when a match settles — a join that never settles, a mode without rewards, or the account's own in-game daily EXP limit all produce zero, and no dashboard can create EXP that the server did not award. When the server credits EXP again the badge clears and `exp_gain_resumed` is recorded. Profile reads happen every `PROFILE_POLL_SECONDS` (default 45 s, 15 s floor) and are also triggered immediately when a match joins and when a match session ends, so a real gain shows up within seconds. `UDP_IDLE_RECONNECT_SECONDS` (default 180) controls how long the UDP session is held before the idle rejoin — raise it if matches are being cut short before settlement.

**V18 daily EXP target:** every account now has a per-day EXP target chosen by its saved level and is taken offline automatically when it is reached. Defaults: **LV 1-29 → 5,000 EXP/day**, **LV 30+ → 50,000 EXP/day** (`.env`: `DAILY_EXP_LOW_LIMIT`, `DAILY_EXP_HIGH_LIMIT`, `DAILY_EXP_LOW_LEVEL_MAX`). Only server-verified profile EXP increases are counted (`profile_gain` deltas), so the counter can never be inflated by queue or UDP traffic, and the day's counted total stops exactly at the target (never above it). When the target is hit the worker is stopped (`stop_account`), the account shows **inactive** with a clear reason, the events `daily_limit_reached` / `daily_auto_offline` are recorded, and pressing **Start** before the reset is refused with the exact numbers. The day boundary defaults to local midnight at **UTC+6** (`DAILY_EXP_RESET_OFFSET_HOURS`) and the next reset time is shown in the UI. Operators can clear today's counter per account (**Reset today's EXP** in the drawer) or for everyone (bulk action `reset_daily`, `POST /api/system/reset-daily`, or the button in Settings → Daily limits). Read-only API: `GET /api/daily/summary`. An optional `DAILY_EXP_AUTO_RESUME=true` restarts accounts automatically on the next day. The public board shows today's verified gain against the target on every card. A Start attempt while today's target is already reached returns HTTP 400 with the exact numbers instead of silently running past the cap.

**V17 neon-glass UI update:** the whole front end was rebuilt on a new design layer, `static/css/ui-v17.css` (loaded after `ui-v14.css`, which now only supplies structure). The V17 language is RGB glassmorphism + claymorphism + clean minimalism + monochrome/duotone + parallax scrolling + immersive 3D tilt + kinetic typography + neon accents + cyberpunk framing. New operator features that use **real server data only**:

- **Real event console** — the admin console streams the actual `activity_log` rows through `/api/activity` and `/ws/live`, with filter, pause, clear and `.log` download. No simulated lines are ever printed; the header states the source (`SQLite activity_log + /ws/live`).
- **Error rack** — every failed fetch, rejected session, WebSocket error, unhandled script error and unhandled promise rejection is captured, timestamped and shown as a dismissible entry; transient network failures are cleared automatically when the same route succeeds again. API failures also add a retry-safe toast instead of failing silently.
- **Level-up radar + celebration** — a genuine level increase from a saved server profile is now recorded server-side as the distinct `level_up` activity event (previously it was only implied by `profile_gain`). The UI raises the celebration card from that server event or from a real `level` increase between two snapshots, showing the account's real old/new level, total EXP and next threshold. It never invents a level.
- **Live transport** — the operator workspace attaches to `/ws/live` (same-origin HttpOnly cookie auth) and falls back to 4.5 s polling with exponential reconnect backoff when the socket is unavailable; the deck header always shows which transport is live.
- **Motion engine** — pointer/scroll parallax, 3D tilt and reveal animations are GPU-only (`transform`/`opacity`) and are disabled automatically for `prefers-reduced-motion`, low-power toggles (`Effects off`) and touch devices.
- **Public board** — the read-only page was rebuilt in the same language with a duotone parallax hero, 3D cards, a real-number kinetic ticker and a level-up watch that fires only when a saved public profile level really increases.

Offline checks stay green: `python3 -m pytest tests -q` (74 passed) and the three Node renderer/login/branding suites. `V17_UI_PREVIEW.html` is a self-contained design preview with **demo data**; it is not a working console.

**V16 diagnostics update:** admin account details now show the latest server profile-check status and reason, distinguishing “unchanged” from a failed check. A pending UDP JOIN deadline is not mislabelled as an actual timeout in the Activity Feed. This is diagnostic only: EXP remains server-awarded; no matchmaking or reward behavior is changed.

**V15 authentication update:** the V14 glass/RGB redesign remains, and the admin Add/Edit form now offers both the existing UID/password path and a manual Access Token → `/access-jwt` path. Access Tokens are sent server-to-server by HTTPS POST and encrypted at rest; no refresh-token flow is included, so replace expired tokens manually. For a raw token, the API resolves Open ID/platform first, then the app uses its existing region-specific MajorLogin → GetLoginData flow. A direct-JWT API response with no Open ID still uses the returned JWT for GetLoginData. API exchange/error handling and compatibility are tested offline with mocks, but a successful live token exchange or game-server login has **not** been verified. See [UPGRADE_NOTES.md](UPGRADE_NOTES.md) for the contract, limitations, tests and security steps.

A modern, responsive dark-by-default dashboard with an optional light mode and multi-account orchestration system built around `lv.py` (`lv.txt`) without breaking its standalone CLI capabilities.

## V15 account authentication

- Add/Edit keeps the original **UID + password** login and adds a manually entered **Access Token → JWT API** choice. Existing database accounts migrate as `uid_password`; no old login is removed.
- The server POSTs `{"access_token":"..."}` to `ACCESS_JWT_API_URL` (default `https://sabbir-paid-api2.vercel.app/access-jwt`) using verified HTTPS. The value is encrypted at rest; it is omitted from API responses, public cards, logs and metadata exports. Exports may carry the non-secret `auth_type` label; after importing an Access Token account, enter a fresh token in the admin form.
- Raw Access Tokens are POSTed to the API first; returned Open ID/platform are then used with the app's region-specific MajorLogin → GetLoginData flow. An already-JWT API pass-through (null Open ID/platform) remains a separate direct-JWT path, using `player_info.platform_id` when available and an empty Open ID. There is no refresh-token support; manually replace an expired token. **Live compatibility remains unverified:** mocked tests do not prove a real API exchange or game-server acceptance.
- Access API and `GetLoginData` failures are summarized with safe status/error-class details, never response bodies or credentials. The admin dashboard avoids rebuilding unchanged account/detail DOM during polling, polls overview every 10 seconds, restores page scroll after drawers/modals close, and reduces glass blur/ambient effects on touch devices. The sign-in button is available immediately rather than waiting for the boot/session check. See [UPGRADE_NOTES.md](UPGRADE_NOTES.md) for test/security details.
- Never share Access Tokens or JWTs in chat, logs, screenshots or exported files. Rotate/revoke any credential previously exposed. If a valid API exchange fails at `GetLoginData`, share only sanitized API source with all credentials removed.

---

## 1. Project Folder Structure

```text
/home/user/
├── .env.example                 # Environment variable configuration template
├── .env                         # Active environment variables (auto-generated keys)
├── requirements.txt             # Complete Python package dependencies
├── Procfile                     # Process declaration for Railway / Render / Heroku
├── render.yaml                  # Render.com Free Tier Blueprint configuration
├── Dockerfile                   # Docker container definition for Fly.io / Koyeb / HF Spaces
├── README.md                    # Complete setup, architecture, and deployment guide
├── V17_UI_PREVIEW.html          # Self-contained V17 design preview (demo data, not a console)
├── lv.py                        # Preserved original CLI script with env-var & log-hook support
├── message_ids.py               # UDP/RUDP command ID mapping required by lv.py
├── thunderFF_pb2.py             # Wire-compatible Protobuf message definitions required by lv.py
├── data/                        # Runtime SQLite database & persistent devices.json
│   ├── dashboard.db
│   └── devices.json
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI entrypoint, REST API endpoints & WebSocket /ws/live
│   ├── config.py                # Environment variable loader & secret key initializer
│   ├── auth.py                  # Argon2id admin authentication, HMAC-SHA256 session tokens & rate limiter
│   ├── admin_access.py          # Operator-only credential check/reset with private DB backup
│   ├── crypto_vault.py          # Fernet credential encryption & log redaction
│   ├── database.py              # SQLite WAL persistence layer for accounts, errors & activity logs
│   ├── schemas.py               # Pydantic v2 input validation models
│   └── bot_manager.py           # Asyncio multi-account worker manager wrapping lv.py functions
└── static/
    ├── index.html               # Admin login and V14 operator console
    ├── public.html              # Read-only V14 Level-Up Bot board
    ├── css/
    │   ├── ui-v14.css           # V14 structural base skin (retained for layout + rollback)
    │   ├── ui-v17.css           # ACTIVE V17 neon-glass / clay / duotone motion skin
    │   ├── theme.css            # Shared dark/light colour tokens
    │   ├── styles.css           # Retained legacy admin layout
    │   ├── control-v10.css      # Retained legacy admin layout
    │   ├── command-v11.css      # Retained legacy skin
    │   ├── glass-v13.css        # Retained legacy frosted skin
    │   └── public.css           # Retained legacy public layout
    ├── images/
    │   └── command-hero.jpg     # Decorative command-room image, no Bot data
    └── js/
        ├── app.js               # Authenticated admin workspace + V17 live deck runtime
        ├── public.js            # Read-only API card renderer
        └── theme.js             # Persistent Dark/Light toggle
```

---

## 2. Local Setup Instructions

### Prerequisites
- Python 3.10+ (tested on Python 3.11 – 3.13)

### Step-by-Step Installation
```bash
# 1. Create and activate a virtual environment (optional but recommended)
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the environment configuration example
cp .env.example .env

# 4. Start the Web Dashboard server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` for public cards and `http://localhost:8000/admin` for login when using the Uvicorn command above. `python run.py` instead uses `PORT` from `.env` (for example, 7723). Do not use a stale copied ZIP's preview HTML to sign in.
- **Default Administrator Username:** `admin`
- **Administrator Password:** No shared default. Set a unique `ADMIN_PASSWORD` (12+ characters) in your private `.env` or hosting secrets. On a writable local first run, one is generated into `.env`; read it privately on your server. In a read-only deployment, provide the variable before startup. Previously published/weak passwords such as `admin123` are rejected at startup: replace them before upgrading.
- **Password changes:** The Admin Settings form stores a new Argon2 hash in the **same persistent SQLite database** used for accounts, and revokes all previous sessions. A Settings change persists across restarts while the configured environment password is unchanged; changing the configured password and restarting intentionally rotates the saved hash and revokes older sessions. On first V13 startup, an old database with no config marker is synchronized to the configured secret once. Keep the database on persistent storage and back it up with `.env`, `SECRET_KEY` and `FERNET_KEY`. To recover access if the new password is lost, stop the service, restore a trusted database backup or have the server operator reset the `admin_auth` row offline and set a new strong `ADMIN_PASSWORD`; do not publish any secrets.
- Use HTTPS on internet deployments. The admin session is an HttpOnly SameSite cookie (Secure under HTTPS); public `/admin` login HTML is visible, but administrator API data/actions and WebSocket connections require valid server-side authentication.

### Running the Original CLI Script Directly
Your original CLI workflow remains intact:
```bash
python3 lv.py
```

---

## 3. Free Hosting & Deployment Instructions

> **Important Hosting Note on Outbound TCP/UDP Sockets:**
> The web dashboard, REST API, SQLite database, Cloudflare DNS checks, and HTTP/HTTPS login checks run on any standard free Python host. However, full Battle Royale match sessions (`play_game`) require outbound **raw UDP sockets** (`socket.SOCK_DGRAM`), which some HTTP-only serverless platforms block. Below are the best free options:

### Option A: Render.com (Free Web Service)
1. Push this project folder to a private GitHub repository.
2. Log in to [Render.com](https://render.com) and click **New +** -> **Blueprint** (uses `render.yaml`) or **Web Service**.
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

### Option B: Fly.io / Railway / Koyeb (Recommended for Full TCP + UDP Socket Support)
1. Install `flyctl` or connect your GitHub repo to Railway / Koyeb.
2. Deploy using the included `Dockerfile`.
3. Set `PORT=8000`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`, and `FERNET_KEY` in the platform's secrets manager.

### Option C: Oracle Cloud Free Tier / Free Linux VPS
1. Provision an Ubuntu VM on Oracle Cloud Always-Free Tier.
2. Clone the repository, install dependencies (`pip install -r requirements.txt`), and run with `systemd` or `tmux`:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
   This provides unrestricted outbound TCP and UDP socket access for all accounts.

### Queue recovery (V7)

Each account independently tracks successful TCP queue sends since its last confirmed UDP JOIN. At 100 consecutive unjoined requests it allows the in-flight UDP join to resolve, closes that account's worker connections, stays entirely offline for 10 minutes, then starts automatically. `Stop` cancels the offline timer; `Start` after that creates a fresh streak. Restart-safe deadlines are stored in SQLite. A sent UDP startup is **not** a server-confirmed JOIN or a reward. Dashboard Recovery Watch shows the exact stage, request streak and offline countdown. Configure `QUEUE_NO_JOIN_LIMIT=100`, `QUEUE_COOLDOWN_SECONDS=600` and `START_MATCH_INTERVAL_SECONDS=3` in `.env` if needed. Use a single Uvicorn worker; a multi-process deployment cannot safely coordinate these in-memory tasks.

### Public Bot cards and Admin page (V8)

`/` is the read-only User Bot Card page. It displays the observed profile and worker states of every saved Bot, with no account controls or login credentials. `/admin` is the private administrator workspace; all management API routes remain protected. The public `/api/public/overview` endpoint returns allowlisted Bot card data only: it intentionally excludes guest login UIDs, secrets, device identities, notes, raw logs and private status messages. In-game IDs and nicknames ARE public; obtain user permission before exposing the service on the internet. Layout previews are labeled and contain no live account data.

### Aurora RGB UI + server-side admin access (V9)

- First visit: **Dark**; the public page and admin workspace both offer **Light mode** via a toggle. The selection is stored only in the current browser's localStorage and is shared across both pages. The deployed cards show actual saved Bot observations; layout-only preview cards are explicitly fictional.
- There is no wildcard credentialed CORS, cross-origin browser writes are rejected, private API responses are not cached, and no password is shared by all installations. Signed sessions must match the current admin username, role and stored revocation epoch. Admin changes persist in SQLite and revoke previous sessions; logout revokes sessions too. WebSockets accept same-origin cookie sessions, not URL query-string tokens; cross-origin browser handshakes are rejected.
- The protected boundary is on the **server**, not a hidden tab: anonymous users can view the login HTML at `/admin` but cannot read admin data, mutate accounts, or subscribe to `/ws/live`. Do not mistake the public Bot API for an authentication layer; it is intentionally read-only and allowlisted.
- The app uses a single worker. Preserve your original `.env`, database and keys during upgrade; do not include them in public archives or repositories. On read-only hosting provide `ADMIN_PASSWORD`, `SECRET_KEY` and `FERNET_KEY` in persistent secrets and use persistent SQLite storage.

### Observatory Bot cards + horizontal Admin command deck (V10)

- The public `/` page has a new Observatory layout: compact status overview and spacious read-only Bot cards, with **LV prominently displayed above a level-progress line**. The line uses the server profile's level and cumulative EXP against the user's supplied level 1–65 chart. It is shown **only** when the saved profile and chart agree; missing profiles, chart mismatches and levels outside the chart show an explanation instead of a fabricated percentage. The EXP remaining and start/next thresholds come from the same server-checked calculation.
- Cards display saved total EXP, observed EXP gained since login, queue request/streak counts, recorded UDP joins and server-end signals, UDP stage, last profile check and cooldown timer. Expand **More recorded details** for last verified EXP gain, number of EXP-gain checks, session start, UDP timeouts and last UDP signals, recorded cooldowns and reconnects. These are allowlisted non-secret database observations; they are **not** guaranteed game rewards or invented live statuses. No guest login UID, passwords, device fingerprint, raw logs or editing controls appear on the public page.
- The admin workspace now uses a horizontal command deck instead of the earlier sidebar. Login, management APIs, authentication rules, Bot protocol, account identifiers and the original worker logic have not been changed for V10.
- Included standalone HTML previews are labeled **fictional**. Real cards populate from `/api/public/overview`. Offline regression tests and a Node renderer check cover the LV line, unavailable/mismatched profiles, HTML escaping, public allowlist and existing security tests. Live game-server or production verification has not been performed.

### V11 / Login + brand + Corporation Signal interface

- **Exact identity:** full name `ＧＡＭＥＲ-ＣＯＲＰＯＲＡＴＩＯＮ`, short name `ＧＡＭＥＲ-ＣＯＲＰ`, credit `Developer ! ＧＡＭＥＲ ＳＡＢＢＩＲ` in the page chrome and credits. The dark/light UI now uses a matte command-deck design with restrained signal accents instead of the previous Aurora/orbit artwork. Saved Bot telemetry, level-line conditions and backend worker logic are unchanged.
- **Important: previews cannot sign in.** `ADMIN_LAYOUT_PREVIEW.html` is a standalone design-only HTML file. Its button is intentionally disabled and the page explains why. Extract the ZIP, install dependencies, run `uvicorn app.main:app --host 0.0.0.0 --port 8000`, then open `http://localhost:8000/admin`. Do **not** open `static/index.html` directly as a local file: it needs the running API and session cookies. `ADMIN_WORKSPACE_PREVIEW.html` is also a noninteractive layout preview.
- Real `/admin` shows connection/sign-in progress, a visible invalid-password or network error, a responsive disabled/loading button, and confirms its cookie against `/api/auth/me` before entering the workspace. Login is server-side; `/api/dashboard/overview` remains inaccessible without an authorized session. V13 syncs an intentionally changed `ADMIN_PASSWORD` from the active process environment on restart; Settings password changes remain saved until that configured secret changes. First V13 startup also migrates a legacy database to the configured secret once. A trusted server operator can restore a private DB backup or reset that row offline after taking backups—never expose the stored hash or share the private `.env`.
- Offline tests cover a valid login, protected dashboard, rejected login and network-feedback UI. The actual user's remote deployment, credentials, cookie/proxy configuration and browser could not be inspected here; if sign-in still fails on a running `/admin`, record the **error text or HTTP response status without sending passwords** for diagnosis.

### V12 / Recovering an admin password safely (operator-only)

**V13 login fix:** older versions let a saved SQLite `admin_auth` hash override a changed `.env` password, causing a confusing 401. V13 records a secret-keyed marker for the configured password. At its first migration, or when the active configured `ADMIN_PASSWORD` changes later, it synchronizes the saved Argon2 hash and revokes older sessions on server startup. A password changed via Admin Settings still persists on restart while the configured secret stays the same. Restart after editing `.env`; process/hosting environment variables take precedence over `.env`. Other causes of 401 include an old running instance, duplicate nonempty `ADMIN_PASSWORD` lines, a different `DATABASE_PATH`, or a 60-second rate-limit window. Never delete the Bot database to fix sign-in.

**Your password has been shared in a message: rotate it first** to a new, unique 12+ character value in your private `.env`/hosting secrets; never post it or your `.env`. Run these commands **on the deployed host and the same data volume**, not a fresh ZIP folder:

```bash
# Does the configured secret match the active database hash? No passwords are printed.
python -m app.admin_access check

# If a running V13 server still reports DOES NOT MATCH, first restart it after
# rotating the configured secret. If an explicit operator reset is still needed,
# stop the server, back up DB/.env and check for conflicting nonempty entries:
python -m app.admin_access reset --confirm

# Start the server again; sign in using your newly rotated private password.
```

The reset tool backs up the SQLite DB to a private adjacent `*.auth-backup-*.db` file, changes **only** `admin_auth`, revokes older sessions and leaves Bot credentials, Guest UIDs, profiles and worker logic untouched. If the check says **MATCHES**, do not reset; confirm that the running process uses the same database/environment, review the exact HTTP response and try again after the 60-second login-rate-limit window. If your host has no shell, set `ADMIN_PASSWORD_RESET_ON_START=1` for **one startup** after rotating the secret and backing up the persistent database; remove the flag immediately after successful login. A forgotten flag can overwrite later UI password changes on subsequent restarts. There is no public web reset endpoint.

The V12 matte command UI uses a generated atmospheric image as a decorative asset (no Bot data in the image), with a text-safe overlay and a light alternative. Public cards still load actual saved Bot/game IDs via the allowlisted API. Standalone design previews contain clearly marked fictional sample cards and cannot sign in. Remote credentials and live game-server behavior were not tested here.

### V13 / Environment-credential synchronization and glass UI

On deployment, V13 hashes the configured `ADMIN_PASSWORD` and stores only a **secret-keyed fingerprint** alongside the admin Argon2 hash. A deliberate change to that environment value replaces the stored admin hash on the next startup and invalidates older sessions; a Settings password change remains valid across restarts if the configured value did not change. Older databases have no fingerprint: on the **first V13 startup**, the configured secret becomes authoritative once. **The password you posted in chat is exposed: rotate it privately before this first restart.** The server never echoes it, and the Bot accounts/Guest UIDs remain untouched. If a hosting secret differs from `.env`, the hosting/process value wins; `python -m app.admin_access check` reports the source and whether it matches the saved hash without printing either. Retain the operator reset command only for special recovery cases, not normal `.env` rotation.

A translucent glass layer is applied to both dark and light modes (navigation, login, account cards, level track and diagnostic panels) over the decorative command-room image. The image contains no game telemetry; real data still arrives from protected/read-only APIs. Offline tests exercise legacy DB migration, changed env value, Settings persistence, session revocation and Bot data retention. Remote deployment credentials and live game servers cannot be verified from this workspace.
