# ＧＡＭＥＲ-ＣＯＲＰ — V16 server-profile diagnostics (2026-10-11)

## V16: Show the exact result of each profile check

- Admin account details now show the last profile-check timestamp and the server-reported result/reason (`changed`, `unchanged`, `failed`, or `not checked`) separately. This distinguishes a successful unchanged EXP check from an API/network failure.
- Keeps the V15.1 Activity Feed correction: a startup message announcing a 75-second JOIN deadline is a UDP phase, not an actual timeout. Actual timeout events remain classified separately.
- No matchmaking cadence, UDP packet behavior, EXP formula, or reward logic was changed. The app only displays values returned by GetLoginData; it does not create or accelerate game EXP.
- Verification: 74 offline pytest tests pass, plus the Node profile-diagnostics regression check and existing UI/login checks.

---

# ＧＡＭＥＲ-ＣＯＲＰ — V15.1 telemetry correction (2026-10-11)

## V15.1: Do not label a pending UDP deadline as an actual timeout

- Fixed Activity Feed classification: `UDP startup packets sent; awaiting JOIN_MATCH (timeout 75s)` is a startup/phase event, not an expired timeout. The server can still send JOIN_MATCH afterward, as the attached log shows. Only actual JOIN, idle, handshake, or session-limit expiry messages are categorized as UDP timeouts.
- This corrects a misleading log label only; it does **not** change EXP reading, packet flow, matchmaking, or server rewards. A successful profile check still reflects the game server's reported EXP.
- Verification: 74 offline tests pass, including a regression test proving that a pending 75-second deadline followed by JOIN is not counted as a timeout.

---

# ＧＡＭＥＲ-ＣＯＲＰ — V15 authentication update (2026-10-10)

## V15: Keep UID/password and add Access Token → JWT API login

- The V14 glass/RGB redesign stays in place. The authenticated Add/Edit account form now offers **both** `UID + password` (the original Garena OAuth → MajorLogin → GetLoginData path) and `Access Token → JWT API`.
- Token mode POSTs JSON `{"access_token":"..."}` to `ACCESS_JWT_API_URL` (default `https://sabbir-paid-api2.vercel.app/access-jwt`). The token is stored encrypted with Fernet, is never returned in account/public API responses or metadata exports, and request logs avoid bodies/secrets. API URL must be HTTPS and the dedicated Access API client verifies TLS certificates. Token renewal remains manual: replace the value from Edit after it expires.
- Raw-token mode POSTs to the API first and uses its Open ID/platform metadata to build the encrypted login request; the app then runs its existing region-specific MajorLogin → GetLoginData flow and uses that local MajorLogin session JWT. This avoids relying on an API-issued JWT from a potentially different server/version. The attached API also has an already-JWT pass-through response with null Open ID/platform; that branch remains separate, using `player_info.platform_id` when available and the supplied JWT for GetLoginData. **Neither path has been verified with a valid live token/game-server response.** A safe activity event records only the exchange mode, whether Open ID was present, platform and region—never the Open ID or token.
- API errors are summarized without echoing response bodies or credentials; HTTP 422 is reported as Open ID extraction failure and HTTP 502 as API MajorLogin/JWT generation failure. Migration adds `auth_type` and encrypted Access Token columns; existing accounts default to `uid_password`. Metadata exports include only the authentication-method label, never secrets; imports of token accounts require manually re-entering the Access Token.
- UI responsiveness/scroll fixes: unchanged account/detail DOM is not rebuilt on every poll, admin overview polling is every 10 seconds, mobile glass blur/ambient effects are reduced on touch devices, and drawer/modal scroll is contained with page scroll-position restoration on close. The login button is enabled immediately; the boot session check may restore an existing session but no longer blocks sign-in if `/api/auth/me` is slow or unavailable.
- GetLoginData failures now expose only a safe HTTP status or exception class (never response body, token or request headers), to distinguish game-server rejection from network/upstream failures.
- Verification: 73 offline pytest tests pass, including mocked raw-token and direct-JWT paths, encrypted storage, no-secret serialization, mode switching, existing UID/password tests, UI scroll/performance checks, and sanitized GetLoginData diagnostics. A POST using a dummy non-secret test value previously received HTTP 422 with an Open ID extraction error; this proves only the error contract—not a successful exchange or GetLoginData acceptance. If a valid token exchanges but GetLoginData rejects it, share only sanitized logs/source with all credentials removed. Revoke/rotate any Access Token or JWT previously shared.

---

# ＧＡＭＥＲ-ＣＯＲＰ — V14 UI refresh (2026-10-10)

## V14: Full Level-Up website redesign

- Public `/` and private `/admin` now have a new, responsive Level-Up visual system: layered glass cards, neon cyan/violet/pink signal accents, ambient grid/orbit artwork, and a rebuilt page hierarchy rather than a colour-only change.
- The public roster cards now foreground the saved server level/EXP, a chart-verified progress ring/bar, session EXP change, Bot state and queue/UDP counters. Unknown profiles and chart mismatches still do not get an invented level-progress bar.
- Added public manual refresh plus sorting by name, level, saved EXP or latest profile check. Existing search, state filters, read-only behavior, theme switch and 10-second refresh remain.
- Rebuilt the admin login, navigation rail, overview command banner, account directory, activity feed, detail drawer and settings panels. Existing authenticated API routes, forms, account controls, worker logic and session boundary are preserved.
- `static/css/ui-v14.css` is the active skin. The previous CSS files remain in the repository for rollback/compatibility but are not loaded by the new page shell. No external fonts or image CDN are required.
- Verification: `python -m pytest -q` (55 passed), plus Node syntax and offline UI/login-renderer checks. These checks do not test live Garena servers or guarantee in-game EXP/rewards.

---

# ＧＡＭＥＲ-ＣＯＲＰ — Pulse upgrade (2026-10-09)

## কী বদলেছে

- লগইন একসঙ্গে চলার সীমা এখন `.env`-এ `MAX_CONCURRENT_LOGINS` (ডিফল্ট 8)। ব্যর্থ লগইনের 15 সেকেন্ডের অপেক্ষা আর লগইন স্লট ধরে রাখে না।
- দীর্ঘ সময় চলা অ্যাকাউন্টের জন্য আলাদা `MAX_ACTIVE_WORKERS` (ডিফল্ট 50, কনফিগারযোগ্য সর্বোচ্চ 1000)। সীমা পূর্ণ হলে স্পষ্ট error পাওয়া যায়; 1000 **রেজিস্টার্ড ID** মানে 1000 **একই সঙ্গে সফল live session** নয়।
- Bulk Start/Stop-এ বারবার সম্পূর্ণ dashboard broadcast বাদ দেওয়া হয়েছে; সর্বোচ্চ 1000 account ID বাছা যায়। capacity না থাকলে UI প্রথম error দেখায়।
- `lv.py`-এর আসল worker output থেকে account-specific log save করা হয়; `DEBUG`, ping ও high-volume success ACK বাদ থাকে। বাস্তব লগ `GET /api/activity` থেকে authentication, pagination এবং account/level filter-সহ দেখা যায়; UI ছয় সেকেন্ড অন্তর আপডেট হয়।
- Match পাওয়া মানেই `in_match` নয়: server থেকে প্রকৃত UDP `JOIN_MATCH` packet এলে তবেই `in_match` দেখায়। কোনো simulated gameplay event যোগ করা হয়নি।
- Dark UI, mobile layout, activity console, worker/login capacity metric উন্নত হয়েছে। `.gitignore` যোগ হয়েছে যাতে `.env`, database বা device profiles ভুল করে GitHub-এ না যায়।

## চালানোর নিয়ম

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env-এ ADMIN_PASSWORD অবশ্যই শক্তিশালী পাসওয়ার্ড দিন।
# প্রয়োজনে MAX_ACTIVE_WORKERS=50, MAX_CONCURRENT_LOGINS=8 থেকে ধীরে বাড়ান।
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**জরুরি:** `--workers 1` ব্যবহার করুন। একাধিক Uvicorn process এই in-memory account manager ও socket session দ্বিগুণ করে ফেলবে। SQLite ও outbound TCP/UDP-এর জন্য স্থায়ী storage এবং UDP-সমর্থিত host লাগবে। Render free-তে network/memory/ephemeral storage-এর সীমা থাকতে পারে।

**Fernet key স্থায়ী রাখুন:** `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` দিয়ে একটি key তৈরি করে hosting secret-এ `FERNET_KEY` সেট করুন। হারিয়ে গেলে পুরোনো encrypted account password আর ডিক্রিপ্ট হবে না। `SECRET_KEY`-ও স্থায়ী রাখতে হবে। `.env`, guest credentials বা DB public repo-তে আপলোড করবেন না।

## পরীক্ষা ও বাস্তব সীমাবদ্ধতা

```bash
pip install pytest
pytest -q tests/test_upgrade.py
```

Offline পরীক্ষায় ১২টি **mocked** worker একসঙ্গে start, capacity limit, retry-তে semaphore release, log-to-account mapping, redaction এবং authenticated paginated API যাচাই হয়েছে। আসল Garena OAuth, server acceptance, TCP/UDP gameplay বা 50/1000 simultaneous live sessions **পরীক্ষা করা হয়নি**—বৈধ নিজস্ব test account ও অনুমোদিত network/hosting-এ ধাপে ধাপে পরীক্ষা করতে হবে। Server rate limit, account policy, CPU/RAM ও port/file-descriptor সীমা এই কোড একা দূর করতে পারে না।

### পরিচিত সীমা
- অ্যাকাউন্টের saved status server restart-এ idle হয়ে যায়; প্রয়োজন হলে আবার Start দিন।
- `ADMIN_PASSWORD`-এর কোনো shared default নেই; `admin123`/দুর্বল password থাকলে server চালু হবে না। নিজের ১২+ অক্ষরের শক্তিশালী secret দিন।
- আসল live logs দেখতে worker চালাতে হবে; account না থাকলে শুধু admin/system event দেখা যাবে।

## V3: ＧＡＭＥＲ-ＣＯＲＰ UI ও EXP intelligence

- আগের ব্র্যান্ডিং সরিয়ে login screen, sidebar, hero, dashboard, account fleet ও backend title-এ `ＧＡＭＥＲ-ＣＯＲＰ` দেওয়া হয়েছে। UI এখন সম্পূর্ণ নতুন graphite/lime/violet layout; account view প্রথমে কার্ড হিসেবে খোলে, প্রয়োজন হলে table-এ যাওয়া যায়। **আসল guest UID পরিবর্তন করা হয়নি**—পরিচয় ভুল দেখানো বিপজ্জনক।
- Overview-তে account বেছে নিলে **server থেকে পাওয়া latest level/total EXP** এবং **তোমার chart থেকে হিসাব করা next-level EXP remaining** আলাদাভাবে দেখায়। প্রতিটি card, table row ও account details drawer-তেও হিসাব দেখা যায়। Login/profile না পাওয়া পর্যন্ত `Awaiting profile`; chart-এর বাইরে বা অমিলে `Chart mismatch`, অনুমান করে সংখ্যা বানানো হয় না।
- Level 1–65 chart UI-তে খোলা যায়। Level 19: chart-এ দেওয়া 1730 কিন্তু পরের মোট EXP অনুযায়ী **1930**; level 20: 1792 বনাম **1592**; level 54: 51484 বনাম **51464**। এই তিন সারিতে **পরের level-এর cumulative EXP threshold**-কে authoritative ধরে বাকি EXP হিসাব করা হয়; UI ⚠ দিয়ে চিহ্নিত করে। Level 65 → 66-এর threshold = 1634300 + 206646 = **1840946** (দেওয়া প্রয়োজনীয় EXP থেকে নির্ণীত)।
- EXP chart **user-supplied static reference**, server-এর official chart নয়। Level/EXP live worker profile থেকে আসে, আর `EXP left` হলো chart-derived estimate; একই জিনিস হিসেবে দেখানো হয় না।
- নতুন protected `GET /api/exp/chart` endpoint, 65-level গণনার test ও mismatched profile test যোগ হয়েছে।

## V4: +0 EXP / 23-minute stalled match diagnostic fix

- মূল বাগ: `initial_exp == 0` হলে প্রথমবার EXP বাড়ার সময় পুরোনো কোড `initial_exp`-কেই নতুন EXP দিয়ে replace করত। নতুন অ্যাকাউন্টে প্রথম verified baseline **0-ই থাকে**; 0 → 48 হলে এখন lifetime gain +48 হয়। `Session EXP Gained` আলাদা `session_start_exp` থেকে গণনা হয় এবং প্রতি **সফল login-এ নতুন baseline** বসে।
- পুরোনো DB-তে যেসব initial baseline ইতিমধ্যেই overwrite হয়েছে, সেখানকার অতীত gain নির্ভরযোগ্যভাবে উদ্ধার করা যায় না। Migration তাদের `legacy_unknown` হিসেবে চিহ্নিত করে—পুরোনো +0 সত্যি ছিল এমন দাবি করে না। নতুন session শুরু ও সফল profile check-এর পর নতুন gain নির্ভুলভাবে ট্র্যাক হয়। **DB backup রেখে upgrade করুন; `dashboard.db`, `.env` ও `FERNET_KEY` অক্ষত রাখুন।**
- প্রতি ~45 সেকেন্ডের profile refresh এখন `last check / changed / unchanged / error` দেখায়। `GetLoginData`-তে level বা EXP field না থাকলে আগের 1/0 default দিয়ে সত্যি profile মুছে ফেলে না। UI-র `Check server EXP ↻` button authenticated, প্রতি account-এ ১২ সেকেন্ড cooldown; active logged-in worker লাগবে।
- `JOIN_MATCH` পাওয়া মানে কেবল UDP protocol event; EXP reward বা match completed হওয়ার প্রমাণ নয়। `Done 0 / Queued 2` হলে server completion পাওয়া যায়নি। চুপ থাকা UDP connection ডিফল্ট 180 সেকেন্ডে timeout/reconnect করে, ২৫ মিনিট ধরে stale match status রাখে না (পরিবর্তনযোগ্য `UDP_IDLE_RECONNECT_SECONDS`)। ২৫ মিনিটের session limit-ও আর match completed হিসেবে লেখা হয় না।
- যদি manual check-এ `Server profile unchanged` আসে, আসল server এখনও একই EXP দিচ্ছে; local dashboard জোর করে EXP বাড়াবে না। `Check failed` এলে auth/network/protocol সমস্যা বোঝার জন্য account event history দেখুন।
- Offline tests: zero-EXP baseline, legacy migration, missing fields rejection, real changed/unchanged check, authenticated manual refresh and cooldown; **আসল game server-এ EXP award যাচাই করা হয়নি।**

## V5: সত্যিকারের multi-account fleet diagnostics

- `In Match` (UDP JOIN_MATCH) **EXP বা match reward বোঝায় না**। UI এখন `UDP Joined · Reward Unverified` দেখায়। Fleet Diagnostics-এ প্রতিটি account-এর **queue requests, server UDP joins, server end packets (cmd 103/107), UDP timeouts, last JOIN, last finish, latest server EXP, profile check ও session EXP gained** আলাদা করে দেখা যায়। নতুন UDP JOIN/TIMEOUT এবং **server_finish_count** এই version থেকে শুরু হয়; পুরোনো log পুনর্গণনা হয় না। পুরোনো `matches_completed`-এ duration expiry গোনা হয়ে থাকতে পারে বলে সেটিকে নতুন verified server-finish metric হিসেবে দেখানো হয় না।
- UDP startup-এর পর `JOIN_MATCH` না এলে default **75 সেকেন্ডে** timeout/log/requeue (`UDP_JOIN_TIMEOUT_SECONDS`)। আগের কোডে এই অবস্থায় 25 মিনিট অপেক্ষা হতে পারত। Confirmed JOIN-এর পর বৈধ server packet বন্ধ হলে `UDP_IDLE_RECONNECT_SECONDS` (default 180) timeout। **এটি reward পাওয়ার নিশ্চয়তা নয়।**
- Server থেকে HELLO ও ACK প্রথমবার আসলে সংক্ষিপ্ত **inbound UDP phase logs** লেখা হয় (raw packets, keys বা token নয়); logs-এ event-type filter আছে। `Fleet Diagnostics → Logs` দিয়ে account-specific log দেখা যায়। Safe telemetry JSON export-এ password/token নেই, কিন্তু UID/Game ID থাকে—শেয়ার করার আগে দেখে নিন।
- দুটি Guest UID যদি **একই game account ID**-তে login করে, দ্বিতীয় simultaneous worker বন্ধ হয়ে স্পষ্ট error দেয়; এক account-এর token/session অন্য worker-এর নামে silently map করে না। UDP startup packet তৈরিতে account-এর নিজস্ব `mode_id` ও `map_id` ব্যবহার করা হয় (আগে global default সব worker-এ লাগত)। TCP StartMatch template-এর compatibility server-dependent; server-side acceptance যাচাই হয়নি।
- Match packet account field login ID থেকে আলাদা হলে নিরাপদ warning log দেখায়; field-এর protocol semantics নিশ্চিত নয় বলে সম্ভাব্য বৈধ session discard করে না।
- Offline tests: **17 passed** including two concurrent managed accounts with separate mode/map/game IDs, duplicate-game-ID guard, UDP join timeout and real telemetry counters. Real game servers/EXP award with multiple accounts **পরীক্ষা করা হয়নি**। প্রত্যেক UID আলাদা কিনা এবং `server finish`-এর পর server profile EXP সত্যি বাড়ছে কিনা Fleet page-এ দেখুন।

## V6: BD account comparison / EXP evidence (2026-10-10)

- দুটি আলাদা BD guest-এর screenshot-এ একটির profile-এ +651 EXP দেখা গেছে, অথচ **দুটিরই server-finish counter 0**। তাই server-end packet-কে EXP পাওয়ার শর্ত বলা **ভুল**; UDP JOIN_MATCH-ও EXP-এর নিশ্চয়তা নয়। `0 / 27` ও `0 / 110`-এর queue সংখ্যা cumulative, এই login-এ সমান কাজ হয়েছে বোঝায় না। Screenshot থেকে আলাদা in-game numeric ID, session timing বা কেন অন্যটির EXP বাড়েনি নিশ্চিত করা যায় না।
- BD `REGION_PREFIX` ছিল `031400` (IND-র মতো), কিন্তু informational TCP keepalive-এ BD fallback হয়ে `0215` (SG family) পাঠাচ্ছিল। Region mapping-এর সঙ্গে মিলিয়ে BD-র packet এখন `0214`। এটি **নির্দিষ্ট client-side inconsistency fix**; live server-এ এর ফলে সব account reward পাবে এমন দাবি নয়।
- প্রতিটি login-এ `session_last_exp_gain_at` ও `session_exp_gain_events` reset হয়। সফল GetLoginData profile refresh-এ সত্যিকারের EXP **বাড়লে** শুধু ওই account-এর জন্য `profile_gain` event লেখা হয়; level-only change, UDP JOIN বা failed check-এ নয়। Fleet card-এ last gain time এবং account-specific ব্যাখ্যা দেখায়; পুরোনো cumulative counter-কে current-login gain হিসেবে ধরে না। Export-এ per-login baseline/timestamp থাকে (কোনো token/password থাকে না)।
- `NEEDS REVIEW` এখন শুধু ৫ মিনিট joined থাকার জন্য দেখায় না; প্রথম verified gain ছাড়া ১০ মিনিট বা শেষ verified gain-এর পর ২০ মিনিটে account-টি review করতে বলে। **এটি diagnosis signal, automatic reward বা restart নয়**।
- Offline regression test-এ BD keepalive mapping ও একসঙ্গে joined দুই account-এর মধ্যে কেবল একটির +651 profile gain isolation যাচাই হয়। **আসল game-server reward, BD keepalive acceptance, এবং অন্য account-এর +0-এর কারণ live-এ যাচাই করা যায়নি।**

### লাইভে পরীক্ষা করার নিরাপদ পদ্ধতি

১. পুরোনো database ও `.env` backup রেখে V6 deploy করুন; একই guest যোগ করার প্রয়োজন নেই। দুই account-এর `Game` numeric ID আলাদা কি না Fleet Diagnostics-এ দেখুন।
২. একসঙ্গে Start করার পর `Check EXP ↻` দিয়ে উভয়ের সফল baseline/EXP নোট করুন; তারপর একই সময় ব্যবধানে আবার চেক করুন। `profile_gain` event, Last Profile EXP Increase, UDP JOIN, timeout এবং Check failed/unchanged আলাদা করে তুলনা করুন।
৩. সমস্যাটি থাকলে Fleet থেকে sanitized JSON report এবং উভয় account-এর **redacted** login/UDP/profile logs ও check-এর সময় পাঠান। শেয়ার করার আগে UID/Game ID চাইলে আড়াল করুন; password, token, key, raw packet বা `.env` পাঠাবেন না।

## V7: 100 queue requests → 10-minute fully offline recovery (2026-10-10)

- `BR match #1502` হলো ওই worker-এর **queue request/attempt serial**, ১৫০২টি সফল match নয়। আগে লগটি network send-এর *আগে* লেখা হতো; এখন TCP `writer.drain()` সফল হলে account-specific `(request sent)` লেখা হয়। নতুন `queue_no_join_streak` কেবল এই version থেকে সত্যি sent request গোনে; পুরোনো cumulative সংখ্যা থেকে অনুমান করে ১০০-এর threshold বসায় না। Server থেকে প্রকৃত UDP `JOIN_MATCH` (অথবা server finish) এলে streak শূন্য হয়।
- **১০০টি পরপর sent request-এ JOIN না এলে** worker নতুন queue request থামায়, ১০০তম request-এর in-flight candidate-এর জন্য ৫ সেকেন্ড (এবং UDP startup হলে তার join timeout পর্যন্ত) অপেক্ষা করে। তবু JOIN না এলে এই account-এর TCP/UDP/info/profile tasks cancel ও await করে **পুরোপুরি offline** করে, তারপর ঠিক **৬০০ সেকেন্ড**-এর cooldown deadline সংরক্ষণ করে। অন্য account চলে; deadline শেষে নিজের credentials দিয়ে শুধু সেই account স্বয়ংক্রিয়ভাবে start হয়। App restart হলেও offline deadline DB-তে থাকে; manual Stop দিলে timer বাতিল, পরের Start-এ নতুন streak। Worker socket বন্ধ নিশ্চিত না হলে "offline" দেখায় না/দ্বিতীয় worker চালায় না; capacity পূর্ণ থাকলে offline রেখে প্রতি ৬০ সেকেন্ডে আবার চেষ্টা করে।
- একসঙ্গে বহু account-এর request burst কমাতে default TCP queue interval **১ থেকে ৩ সেকেন্ড** এবং প্রতি worker-এ ±১৫% jitter; `.env`-এ `START_MATCH_INTERVAL_SECONDS` (1–30), `QUEUE_NO_JOIN_LIMIT` (default 100), `QUEUE_COOLDOWN_SECONDS` (default 600)। এটি server-side queue/rate-limit সমস্যার প্রমাণিত সমাধান নয়।
- UDP startup-এর অর্ধেক packet send ব্যর্থ হলে আগে `THUNDER`-কে সম্পূর্ণ ধরা হতো, `SHARMA` retry হত না; এখন সম্পূর্ণ sequence সফল হলেই startup-success flag বসে। `HELLO`-এর পর ACK আর না আসা অবস্থায়ও ২৫ মিনিট আটকে যাওয়ার বদলে startup ছাড়া **২৫ সেকেন্ডে handshake timeout** হয়। Server থেকে HELLO, ACK, JOIN কোন পর্যায়ে এসেছে/আসেনি তা account-এর `udp_phase`-এ দেখা যায়। UDP packet পাঠানো server গ্রহণ করেছে এমন প্রমাণ নয়; JOIN/EXP server-controlled।
- UI-তে পুরোনো কেন্দ্রীয় অলংকারভিত্তিক hero-এর বদলে **dual-pane Operations Board + Recovery Watch**, desktop-এ **horizontal command navigation**, account-এর **queue lane** (streak/progress/UDP phase), live **MM:SS offline timer**, list-layout account rows, recovery/cooldown filters, queue-streak sort ও per-ID safe report fields এসেছে। UI কোনো fake JOIN, gain বা log বানায় না।
- Tests: 26 offline tests including ২ account isolation, JOIN-এর সময় pending cooldown cancel, দশ-মিনিট-শেষে auto resume (shortened timer mock), manual Stop cancel, DB restart persistence ও protected API. **আসল game-server JOIN/EXP, server's rate limit, network UDP routing এখনও যাচাই হয়নি।**

Deploy: পুরোনো DB এবং `.env`/`FERNET_KEY` backup রাখুন, `--workers 1`-এ চালান। অনাকাঙ্ক্ষিত real-server rate limit এড়াতে প্রথমে ১–২টি নিজের account-এ পরীক্ষা করুন। Queue stage `UDP STARTUP SENT` কিন্তু server HELLO/ACK/JOIN না দিলে host-এর outbound UDP, NAT/firewall ও account-specific sanitized logs দেখুন; অনুমান করে reward দেখাবেন না।

## V8: সম্পূর্ণ নতুন Admin UI + read-only User Bot Card page (2026-10-10)

- আগের dark/neon admin HTML/CSS/JS পুরো বদলে editorial, light control-room layout: নিজস্ব login, account directory, snapshot, recorded activity, queue observation, settings, responsive navigation। এটি আগের UI-এর রং বদল নয়। Admin-এর Add/Edit/Start/Stop/Restart/Delete, account-specific logs, profile check, bulk start/stop/restart/delete/region/error clear, metadata import/export, diagnostics, password change ও EXP reference নতুন UI-তে পুনরায় সংযুক্ত। Worker/protocol logic V7 থেকেই অপরিবর্তিত।
- সাধারণ User-এর জন্য **`/`**-এ আলাদা **read-only Bot Card page**; Admin Panel **`/admin`**-এ। User card-এ সব saved Bot-এর display name, nickname, in-game ID, region, state, last saved server profile level/EXP, user-chart next-level estimate, login EXP delta, queue streak ও cumulative request, UDP phase/joins/timeouts/server-end এবং cooldown timer/Mode/Map দেখায়। এই সংখ্যা ও অবস্থা database/worker থেকে আসে; page কোনো reward বা game completion invent করে না। User page auto refresh হয়, search/filter আছে, কিন্তু Start/Stop/Edit/Delete নেই।
- Anonymous `GET /api/public/overview` একটি **আলাদা allowlisted read-only query**: Guest **login UID**, password, token, device fingerprint, notes, raw logs, diagnostic errors, private worker status messages ও account UUID export করে না। Public in-game ID/nickname প্রকাশ পায়—public server deploy করার আগে এই exposure নিজের privacy policy-র সঙ্গে মিলিয়ে নিন। Admin API ও WebSocket আগের মতো authenticated; public page থেকে mutation endpoint-এ anonymous access 401। Public response `Cache-Control: no-store`।
- Layout-only standalone HTML preview-এ নমুনা card স্পষ্টভাবে fictional লেখা; deployed User page কেবল actual API Bot card দেখায়। Bot না থাকলে কোনো fabricated Bot দেখায় না।
- Offline regression tests: public data allowlist/unauthorized mutation, no made-up profile, new UI paths/ID wiring এবং V7 worker tests; **live game-server বা public hosting verification নয়**।

### ব্যবহার

- `https://your-host/` → User-এর read-only Bot cards
- `https://your-host/admin` → admin login ও controls
- পুরোনো DB, `.env`, `FERNET_KEY` backup রাখুন; public page খোলার আগে display names/nicknames/in-game ID প্রকাশের অনুমতি নিশ্চিত করুন।

## V9: RGB Dark/Light + Admin server-side protection (2026-10-10)

- `/` User Bot cards ও `/admin` Admin UI-তে নতুন Aurora RGB/digital-glass visual; প্রথমবার **Dark**, toggle দিয়ে Light, পছন্দ browser localStorage-এ থেকে দুই page-এ কার্যকর। UI-তে কেবল বাস্তব API state; preview-তে sample fictional বলে চিহ্নিত। আসল Bot UID, EXP logic, worker/protocol অপরিবর্তিত।
- Default `admin123` বাদ; নতুন local setup-এ writable `.env`-এ private random ADMIN_PASSWORD তৈরি হয়। Existing weak password থাকলে startup বন্ধ হবে—প্রথমে strong ১২+ অক্ষরের `ADMIN_PASSWORD` দিন। Public deployment-এ env/secrets manager-এ নিজের password ও persistent `SECRET_KEY`/`FERNET_KEY` দিন। `.env`, SQLite DB ও key একসঙ্গে private backup রাখুন।
- Settings থেকে password বদলালে নতুন hash **SQLite `admin_auth`** table-এ persist হয়; পুরোনো session/token তখন এবং logout-এ বাতিল হয়। পরের restart-এ DB hash প্রাধান্য পায়, তাই DB persistence প্রয়োজন। হারালে server operator-কে offline trusted DB restore বা auth row reset করে নতুন environment password সেট করতে হবে।
- Admin API-তে wildcard credentialed CORS সরানো হয়েছে, cross-origin browser write 403 ও private API response `no-store`; authenticated signed token-এর role/username/epoch validation, WebSocket-এ cookie ও same-origin handshake check; URL-এ `?token=` নিষিদ্ধ। `GET /admin`-এর **login HTML** জনসাধারণ দেখতে পারে, কিন্তু গোপন তথ্য ও সব পরিবর্তন server-side protected। Browser UI লুকানোকে নিরাপত্তা দাবি করা হয়নি।
- ৩৮ offline regression tests (admin API mutation, forged/expired token, invalid WebSocket, password-change persistence ও theme wiring সহ) পাস; live game server/production penetration test এখানে করা হয়নি। Public service-এ HTTPS, firewall/reverse proxy ও secrets management নিজে configure করুন।

## V10: নতুন Observatory design + বিস্তারিত read-only Bot cards (2026-10-10)

- Public `/` page বড় পুরোনো hero/card layout থেকে **Observatory** style-এ পুনর্গঠন; compact header/status, ২-column বিস্তৃত card ও responsive mobile layout। Admin panel-এর পুরোনো left sidebar বদলে horizontal command deck। Dark default ও Light toggle অপরিবর্তিত।
- Bot card-এ বড় **LV** এবং chart-verified **level progress line**। Saved server profile-এর level+total EXP user-supplied ১–৬৫ chart-এর threshold-এর মধ্যে থাকলে তবেই bar, percent, শুরু/পরবর্তী LV threshold ও remaining EXP দেখায়। Profile না থাকলে, chart mismatch হলে বা range-এর বাইরে হলে bar/ভুয়া percent দেখায় না।
- Card-এ queue request/streak, UDP JOIN, server-end, UDP stage, profile-check ও cooldown; expandable details-এ শেষ verified EXP gain, gain-check count, session start, UDP timeout/last signals, cooldown/reconnect count। কেবল allowlisted non-secret DB column; guest login UID/password/device/raw logs প্রকাশ নয়। সব user card read-only; Admin auth V9-এর server-side নিয়মেই থাকে।
- Standalone preview-তে fictional Bot স্পষ্ট চিহ্নিত; deployed page-এ কেবল API-র saved data। ৪১ offline pytest checks এবং real public.js renderer smoke check পাস। Real game server, উৎপাদন hosting/penetration test এখানে যাচাই হয়নি। Upgrade-এর আগে `.env`, database ও Fernet key backup রাখুন।

## V11: Login UX ও ব্র্যান্ডিং (2026-10-10)

- অফিসিয়াল পূর্ণ নাম **ＧＡＭＥＲ-ＣＯＲＰＯＲＡＴＩＯＮ**; সংক্ষিপ্ত নাম **ＧＡＭＥＲ-ＣＯＲＰ**; credit **Developer ! ＧＡＭＥＲ ＳＡＢＢＩＲ**। Admin ও public UI-তে matte gaming command-deck visual—আগের orbit/Aurora look বাদ। Dark default / Light option অপরিবর্তিত। Bot UID বা worker logic বদলায়নি।
- আগে দেওয়া `ADMIN_LAYOUT_PREVIEW.html` ছিল **standalone design preview**; সেখানে `app.js` বা backend নেই, তাই Enter workspace button কাজ করত না—এই বিভ্রান্তিকর preview এখন sign-in button **disabled** করে এবং স্পষ্টভাবে বলে সার্ভার চালিয়ে `/admin` খুলতে হবে। আলাদা `ADMIN_WORKSPACE_PREVIEW.html`-ও noninteractive, sample user cards fictional।
- চালু `/admin`-এ login submit-এর loading/error/connection status স্পষ্ট; সফল login-এ session-cookie `/api/auth/me` দিয়ে নিশ্চিত করে তারপর panel খোলে। API-তে valid credentials সহ login→`/api/auth/me`→protected dashboard offline integration test করা হয়েছে; frontend success, invalid password, server offline—তিনটিই script দিয়ে যাচাই। User-এর নিজস্ব remote hosting/browser এখানে দেখা হয়নি; যদি actual running server-এ সমস্যা থাকে তাহলে **কোন পেজ/HTTP status/error** জানান, password নয়। Password Settings দিয়ে আগে বদলানো হলে SQLite `admin_auth` hash `.env`-এর bootstrap password-এর আগে ব্যবহৃত হয়; ডেটা ব্যাকআপ ছাড়া reset করবেন না।

## V12: `.env` password mismatch recovery + realistic visual (2026-10-10; V13-এর আগে পুরোনো আচরণ)

- Settings-এ admin password পরিবর্তনের পর `admin_auth` SQLite hash-ই কার্যকর; `.env`-এর `ADMIN_PASSWORD` **শুধু initial/bootstrap**। তাই `.env`-এ লেখা password ঠিক হলেও পুরোনো DB hash আলাদা হলে login 401 দেখাবে। স্বয়ংক্রিয়ভাবে পুরোনো env password দিয়ে bypass করা হয়নি। Shell-এ `python -m app.admin_access check` চালালে **password না দেখিয়েই** configured value saved hash-এর সঙ্গে মেলে কি না জানায়।
- আগে ব্যবহারকারীর পাঠানো password chat-এ প্রকাশিত হয়েছে; private `.env`/hosting secret-এ **নতুন শক্তিশালী password দিন**, পুরোনো DB ও `.env` backup রাখুন, live server বন্ধ করে `python -m app.admin_access reset --confirm` চালান, তারপর restart করুন। Reset শুধু admin hash ও session revocation epoch বদলায়, SQLite-consistent private DB backup রাখে, Bot UID/account data বা worker logic ছোঁয় না। `ADMIN_PASSWORD`-এর একাধিক nonempty line থাকলে reset বন্ধ হয় (blank template line + একটি value ঠিক আছে)। Shell না থাকলে `ADMIN_PASSWORD_RESET_ON_START=1` **একবার** দিয়ে boot, পরে সরিয়ে দিন। Check MATCHES হলে reset নয়; deployed process-এর environment, database path, rate-limit এবং 401/403 response যাচাই করুন।
- UI-তে একটি decorative cinematic command-room image, আরও grounded matte gaming visual ও clearer typography; Dark/Light mode, full brand ও Developer credit অক্ষুণ্ণ। Preview-র Bot নমুনা স্পষ্ট fictional; আসল page কেবল allowlisted saved values দেখায়। কোনো public reset API বা authentication shortcut নেই।

## V13: Environment password auto-sync + glass UI (2026-10-10)

- V12 পর্যন্ত আগে বদলানো SQLite `admin_auth` hash থাকলে `.env` password দিয়েও 401 আসত। V13-এ configured `ADMIN_PASSWORD`-এর **secret-keyed fingerprint** DB-তে থাকে: `.env`/hosting secret বদলে server restart করলে নতুন value দিয়ে admin hash sync হয় ও আগের session বাতিল হয়। Existing DB-র প্রথম V13 boot-এ fingerprint না থাকলে configured secret **একবার** অগ্রাধিকার পায়। Settings-এর password change **যতক্ষণ configured secret অপরিবর্তিত** থাকে ততক্ষণ restart-এর পরও থাকে। Auth bypass বা alternate web password নয়। Bot UID/worker logic অপরিবর্তিত।
- আগে chat-এ পাঠানো password প্রকাশিত—প্রথম V13 restart-এর **আগে** hosting process secret বা private `.env`-এ নতুন strong password দিয়ে rotate করুন। Hosting/process env `.env`-এর চেয়ে অগ্রাধিকার পায়। `python -m app.admin_access check` কার্যকর source ও match/mismatch দেখায়, কিন্তু কোনো secret/hash দেখায় না। পুরোনো DB/.env backup রাখুন। যদি এখনও 401 আসে, running instance/version, process env, DB path, rate limit ও HTTP status যাচাই করুন; password পাঠাবেন না।
- Dark ও Light দুই মোডে glass/frosted surface: login, navigation, summary, Bot card ও admin panels-এ translucent layer; পেছনে decorative command-room photo, যা real Bot data নয়। Public cards এখনও API-র আসল saved Game ID/EXP/UDP state দেখায়; fictional preview স্পষ্ট চিহ্নিত। Offline regression tests পাস, কিন্তু ব্যবহারকারীর remote deployment বা live game-server পরীক্ষা হয়নি।


---

# V18 — Daily EXP target (auto-offline)

## Behaviour
- Each account has a **daily target** derived from its saved level:
  `LV 1-29 → DAILY_EXP_LOW_LIMIT` (default 5,000) · `LV 30+ → DAILY_EXP_HIGH_LIMIT` (default 50,000).
- Only **server-verified** gains count: the delta comes from comparing the saved profile with a
  fresh `GetLoginData`/profile response inside `_on_profile_result`. Queue sends, UDP joins and
  server-end signals never add to the counter.
- When the day's target is reached:
  1. `daily_limit_reached` is written to the activity log with the exact numbers;
  2. `bot_manager._auto_offline_for_daily_limit()` calls the normal `stop_account()` path
     (sockets, worker task, queue-cooldown timer are all cancelled the same way a manual Stop does);
  3. `daily_auto_offline` is logged, the account is `inactive` with a reason that names the reset time;
  4. a forced dashboard broadcast refreshes every open console.
- The day's counter **stops exactly at the target**: a verified gain that would cross the limit is
  counted only up to the remaining quota, so `daily_exp_gained` never shows more than 5,000 / 50,000
  and the account cannot bank EXP beyond its daily rule even if a trailing profile response arrives.
- If the worker cannot be stopped safely the stop is **not** claimed: the failure is recorded
  (`daily_limit_stop_failed` + an error-history entry) and retried on the next profile check.
- `start_account()` refuses to start an account whose target is already reached for today and returns
  the exact numbers, so the cap cannot be exceeded by clicking Start. `resume_active_accounts_on_startup`
  also skips such accounts after a server restart.
- The daily counter is stored in `daily_exp_progress(account_id, day, ...)` — one row per account per
  local day — and old rows are trimmed by `purge_old_daily_exp()` at startup (`DAILY_EXP_HISTORY_DAYS`).

## Day boundary
`DAILY_EXP_RESET_OFFSET_HOURS` (default **6** = UTC+6) defines local midnight, so "today" ends at
00:00 Asia/Dhaka instead of 06:00. `db.daily_reset_at()` returns the next reset as a UTC ISO stamp and
the UI counts down to it.

## API
| Route | Purpose |
| --- | --- |
| `GET /api/daily/summary` | day, reset stamp, active rule, totals and per-account daily rows |
| `POST /api/accounts/{id}/reset-daily` | clear today's counter for one account |
| `POST /api/system/reset-daily` | clear today's counter for every account |
| `POST /api/accounts/bulk-action` `{"action":"reset_daily"}` | bulk clear for the selected accounts |
| `GET /api/activity?event_type=daily_limit_reached` | audit the stops |

Account payloads (admin and public cards) now carry `daily_limit`, `daily_exp_gained`,
`daily_exp_remaining`, `daily_exp_percent`, `daily_exp_reached`, `daily_auto_stopped`,
`daily_reached_at`, `daily_stopped_at`, `daily_day` and `daily_reset_at`.
The dashboard summary adds `daily_rules` and `daily_totals`.

## Optional auto-resume
`DAILY_EXP_AUTO_RESUME=true` starts the accounts again on the next day when the counter is cleared.
It is **off** by default: an operator decides when a bot runs again.

## Tests
`tests/test_daily_exp_limit.py` (23 tests) covers the level rule, missing-level defaulting, the day
boundary, counter accumulation, per-level differences, the manager tracker, the scheduled
auto-offline (with a stub `stop_account`), the SQLite state, the HTTP guards (start refused, reset
endpoints, bulk action, invalid action) and the public-card allowlist. Full suite: **97 passed**.


---

# V18.1 — Why an account can run for hours without gaining EXP (diagnosis)

## What the bot can and cannot do
The bot authenticates, searches a match, performs the UDP join/rejoin handshake and keeps the TCP
session alive. **The EXP reward is decided by the game server** after a match *settles* (the match must
end with a recorded result). A join that never settles, a mode/map that grants no EXP, or the account's
own in-game daily EXP limit all mean **0 EXP**, and no dashboard can invent EXP the server never
awarded. What the dashboard can do — and now does — is prove which case you are in, quickly and honestly.

## Evidence the console now shows
- `PROFILE_POLL_SECONDS` (default 45 s, floor 15 s) sets how often the real profile is re-read.
- A profile read is also triggered **immediately after each `JOIN_MATCH`** and **when a match session
  ends** (`signal_profile_refresh`), so a genuine credit appears within seconds instead of a minute.
- Every read is compared with the saved profile: `profile_gain` (verified increase), `unchanged`, or a
  failed check (which is a different problem and stays a `profile_check_failed` warning).

## The stall watchdog
`BotManager._evaluate_exp_stall()` runs on every profile result:
- **running account + checks answered by the server + EXP unchanged for `EXP_STALL_MINUTES` (default 30)**
  → the account is flagged: `exp_stalled`, `exp_stall_since`, `exp_stall_checks`, `exp_stall_message`,
  activity event `exp_stall` (re-logged at most every `EXP_STALL_RELOG_MINUTES`, default 60).
- a **verified gain** clears the flag and records `exp_gain_resumed` with the awaited time and the delta
  (that also resets the daily counter logic path);
- a **profile that moved without an EXP increase** (e.g. a level change) clears the flag silently;
- a **failing profile check** never raises a stall — it is already reported as a check failure;
- a **stopped account** is never flagged; the state is reset on the next `start_session`.

The message contains only real numbers (current EXP, level, UDP joins, matches ended, checks) and never
password material — `redact_sensitive` is applied before it is stored.

## Where you see it
Roster row: amber **NO EXP FROM SERVER · <duration> · N checks** badge (tooltip carries the full text).
Account drawer: an **EXP CREDIT CHECK** block with the explanation and the exact things to verify
(mode/map rewards, whether the match really ends, the account's own in-game daily EXP limit).
Daily EXP watch header: number of accounts not credited yet. Activity and Live-log filters:
**No EXP credit from server** (`exp_stall`). API: `GET /api/activity?event_type=exp_stall`.

## Knobs
`PROFILE_POLL_SECONDS`, `EXP_STALL_MINUTES`, `EXP_STALL_RELOG_MINUTES` and the pre-existing
`UDP_IDLE_RECONNECT_SECONDS` (how long a UDP session is held before the idle rejoin — raise it if
matches are cut short before they can settle). All are documented in `.env.example`.

## Tests
`tests/test_daily_exp_limit.py` gained 5 watchdog tests (stall recorded with exact numbers, cleared +
resumed, never raised for stopped accounts or failing checks, never raised for a fresh session, and the
fields reaching the dashboard payload). Full suite: **103 passed**.


---

# V18.2 — Safety rails for the hosting server and the game servers

The daily cap and the EXP diagnosis both read the game profile, so both sides need hard limits. Nothing
in V18.2 changes gameplay traffic; every limit below only *reduces* or *bounds* load.

## Game-server side (never a burst)
| Guard | Default | What it prevents |
| --- | --- | --- |
| `PROFILE_POLL_SECONDS` | 45 s (floor 15 s) | Faster than 1 read / 15 s per account is impossible, even by config |
| `PROFILE_CHECK_CONCURRENCY` | 3 | All accounts cannot read profiles simultaneously (`asyncio.Semaphore`) |
| `PROFILE_FORCED_MIN_INTERVAL_SECONDS` | 10 s | Join/match-end triggers cannot stack into extra requests |
| `GAME_API_BACKOFF_THRESHOLD` + `PROFILE_POLL_MAX_SECONDS` | 5 / 300 s | Exponential back off with a ceiling when the API answers with errors |
| jitter ±5 s | always | Reads from many accounts never align on the same second |

`profile_poll_delay()` and `forced_refresh_wait()` are pure functions with tests pinning the floor, the
ceiling and the debounce. The UDP match path is untouched: `test_profile_refresh_hook_adds_zero_packets_to_a_match`
starts the same session with and without the hook and asserts the sent bytes are identical.

## Hosting-side (light footprint)
- `MAX_ACTIVE_WORKERS` / `MAX_CONCURRENT_LOGINS` remain the only concurrency gates for real work.
- Per-account memory maps (`_exp_stall_logged_at`, `_profile_error_logged_at`, `_profile_fail_streaks`,
  `_manual_profile_checked_at`) are cleared in `stop_account()`; traffic counter deques are bounded
  (`maxlen=600`), so long uptimes cannot grow memory.
- No per-read database write was added: counters live in memory and the daily counter still writes at
  most one row per account per day.
- Front end: the panel renders only when its numbers change (`S.safetyKey`), and host readings are
  throttled to one request per minute on the existing tick — no new `setInterval`/`setTimeout`.
- `GET /api/health` (admin): process uptime, RSS, host memory, load per core, event-loop lag, DB and
  data-dir size, disk free, plus the game-API numbers and a plain-language pressure note. `/health`
  stays open and cheap for the hosting platform's liveness probe.

## UI
Settings → **06 / SERVER SAFETY** shows eight tiles (reads per minute, game API state, parallel reads,
failed reads, workers, instance memory, load per core, data size) and a one-line verdict. Public cards
never expose any of it. Bengali operator notes: `V17_START_HERE.txt`.

## Tests
`tests/test_server_safety.py` (12 tests): floor/ceiling/back-off/debounce maths, concurrency cap,
byte-identical UDP traffic with the hook enabled, bounded counters, memory-map cleanup on stop, host
health payload shape, `/api/health` auth + both-sides coverage, public allowlist, and the "no new timer"
front-end contract. Full suite: **116 passed**.


---

# V18.4 — Deploy freshness, live-log fix and browser-level feature tests

## Fixed
1. **Stale console after an upgrade (the daily limits / safety panel "missing").** `app.js` was still
   requested with an old `?v=20261011-…` string while four other assets had no version at all, so a
   browser or CDN could keep serving the pre-V18 files. Now every script/style is versioned
   (`?v=18.4`), `/` and `/admin` answer `Cache-Control: no-store, must-revalidate`, `/static/*` answers
   `no-cache, must-revalidate` with ETag revalidation, and the build is visible three ways:
   header badge (`BUILD 18.4`), `GET /health` → `build`, and `APP_BUILD` at startup.
2. **WebSocket updates never reached the live log.** `applySync()` refreshed the panels but never called
   `ingest()`, so events pushed live (daily-cap stop, level up, EXP stall) only appeared on the next
   8-second poll. `applySync()` now ingests the frame's `recent_activity`; the browser harness proves it
   with a synthetic frame and fails against the old code.
3. Diagnostics/export/import/bulk-add/chart/theme/login/logout/start/stop paths were re-verified end to
   end in a real DOM instead of by reading code.

## New browser-level tests (run in pytest when jsdom is installed)
`tests/check_admin_features.cjs` (37 checks): boot, sign-in, daily watch rows + rule text + roster chip,
EXP-stall badge, live log ingestion (poll **and** WebSocket), safety tiles, fast-mode toggle, live-deck
toggle, refresh, diagnostics, activity view + filters, drawer with daily block/stall block/reset button,
bulk dropdown, theme switch, no script errors.
`tests/check_public_and_flows.cjs` (20 checks): public board renders today's EXP vs target without any
admin control, and the admin modals (add/bulk/chart/import/export), diagnostics panel, error rack,
level-up celebration and sign-out all work without script errors.

## Docs
`.env.example` documents `APP_BUILD`; `V17_START_HERE.txt` explains in Bengali what to do if the console
ever looks old (check `BUILD 18.4`, then a normal reload is enough — no hard refresh needed any more).


---

# V18.5 — Mobile overlay fix + multi-account start pacing

## Fixed: every modal was half off-screen on phones
`ui-v14.css` centres overlays with `transform: translate(-50%, -50%)` while the V17 sheet used the
modern `translate: -50% -50%` property. Both apply, so the offsets doubled: measured at a 360 px
viewport the Add-account modal rendered at `left = -152px` (only its right half visible) — which is
exactly the squeezed column in the operator's screenshot on both **Add Bot** and **EXP reference**.
The V17 rules now set `transform: none` and own the centring through `translate` alone. Also fixed on
the same pass: the toast inherited `right: 20px` from V14 while claiming `left: 50%`, the reference
table is wrapped in a `.table-scroll` container so its three columns stay readable and scroll
sideways, and phone-width paddings/`100dvh` limits were added for modals and the drawer.

## Fixed: 2-4 accounts started together gained no EXP
Each worker opened its own connection to the same gateway and fired `StartMatch` on its own 3 s timer,
and "Start all" began every login in the same second. From the game server that is a burst; matches
were found but never settled, so no EXP was credited. Two guards:

| Guard | Default | Effect |
| --- | --- | --- |
| `ACCOUNT_START_STAGGER_SECONDS` | 4 s | Each worker waits its turn before version check / OAuth / login; the HTTP response is immediate |
| `MATCHMAKING_MIN_GAP_SECONDS` | 1.2 s | Process-wide floor between any two `StartMatch` sends, so a 4-account fleet sends ~1 request/s instead of ~1.3 per account |

A single account is unaffected (its first request goes out with no delay), and the safety panel now
prints both numbers.

## Verification added
* `tests/test_multi_account_start.py` (10 tests): the gap serialises four simultaneous asks, a single
  account is never delayed, the fleet ceiling stays near one account's rate, the start stagger spreads
  workers (one immediate, the rest queued), it can be disabled, the worker itself (not the HTTP call)
  waits its turn, the safety payload exposes both numbers, and no extra packet is produced.
* `tests/browser_mobile_audit.py` drives real Chromium at 320/360/768 px through login, Add Bot, EXP
  reference, settings, activity and accounts, and fails if any surface overflows the viewport
  (currently **0 overflow problems** across all three sizes; it also saves screenshots with `--shots`).
* Two older multi-account tests now opt out of the stagger explicitly, because they measure worker
  isolation rather than fleet pacing.

Full suite: **129 passed, 2 skipped** (the skips are the jsdom harnesses when `npm install jsdom` has
not been run).
