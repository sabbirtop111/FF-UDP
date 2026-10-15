# -*- coding: utf-8 -*-
"""
Async Multi-Account Worker Orchestrator & WebSocket Broadcast Manager.
Wraps lv.py's core functions without breaking CLI compatibility.
"""

import asyncio
import re
import time
from collections import deque
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
import psutil
from fastapi import WebSocket

import lv
from app import database as db
from app.config import (
    DAILY_EXP_AUTO_RESUME,
    DAILY_EXP_ENABLED,
    EXP_STALL_MINUTES,
    EXP_STALL_RELOG_MINUTES,
    daily_limit_for_level,
    daily_rule_payload,
)
from app.crypto_vault import redact_sensitive
from app.config import (ACCOUNT_START_STAGGER_SECONDS, MAX_ACTIVE_WORKERS,
                        MAX_CONCURRENT_LOGINS, QUEUE_NO_JOIN_LIMIT, QUEUE_COOLDOWN_SECONDS)

QUEUE_OBSERVATION_SECONDS = 5  # Do not preempt the 100th in-flight candidate.

# Statuses in which an account is expected to be earning EXP from the game.
RUNNING_ACCOUNT_STATES = {"authenticating", "online", "searching_match", "in_match", "reconnecting"}


def humanize_seconds(seconds: float) -> str:
    """Short, readable duration used in logs and in the UI message."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


class BotManager:
    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.workers: Dict[str, asyncio.Task] = {}
        self.worker_started_at: Dict[str, float] = {}
        self.uid_to_account_id: Dict[str, str] = {}
        self.account_labels: Dict[str, str] = {}
        self._profile_error_logged_at: Dict[str, float] = {}
        self._manual_profile_checked_at: Dict[str, float] = {}
        # V18: accounts currently being taken offline because they hit today's EXP target.
        self._daily_stop_inflight: set = set()
        self._daily_seen: Dict[str, str] = {}
        # V18.2: bounded traffic counters used by the safety panel (no extra polling).
        self._profile_check_times: "deque[float]" = deque(maxlen=600)
        self._profile_failure_times: "deque[float]" = deque(maxlen=600)
        self._profile_checks_total = 0
        self._profile_failures_total = 0
        self._profile_forced_total = 0
        # V18.1: last time the 'no EXP credit from the server' warning was logged.
        self._exp_stall_logged_at: Dict[str, float] = {}
        self._profile_fail_streaks: Dict[str, int] = {}
        # V18.5: serialises account logins so a fleet start is never a burst.
        self._start_slot_lock: Optional[asyncio.Lock] = None
        self._last_start_at: float = 0.0
        self.game_id_to_account_id: Dict[str, str] = {}
        self.active_account_data: Dict[str, Dict[str, Any]] = {}
        self.queue_cooldown_tasks: Dict[str, asyncio.Task] = {}
        self.ws_clients: Set[WebSocket] = set()
        self._broadcast_lock = asyncio.Lock()

        self.diagnostics: Dict[str, Any] = {
            "last_checked": None,
            "playstore_version": "Checking...",
            "release_version": headers_release_ver(),
            "remote_version": "Unknown",
            "server_url": lv.REGION_SERVERS.get("ME", ""),
            "dns_resolved_ip": None,
            "dns_latency_ms": None,
            "cloudflare_dns": lv.CLOUDFLARE_PRIMARY_DNS,
            "version_api_ok": None,
        }

        lv.register_log_callback(self._on_lv_log)

    def _on_lv_log(self, raw_msg: str) -> None:
        """Persist actual worker events, never synthetic gameplay events or secrets."""
        msg = redact_sensitive(raw_msg.strip())
        if not msg or "[DEBUG]" in msg or "PING-REPLY" in msg or "[SEND-OK]" in msg:
            return
        # Match a whole UID field, never a substring of another player's UID.
        uid_match = re.search(r"\bUID\s*:?\s*([A-Za-z0-9_-]+)\b", msg)
        matched_acc_id = None
        if uid_match:
            key = uid_match.group(1)
            matched_acc_id = self.uid_to_account_id.get(key) or self.game_id_to_account_id.get(key)
        if not matched_acc_id:
            return  # Unattributed CLI/system event: do not claim it belongs to an account.

        try:
            self._process_account_log_event(matched_acc_id, msg)
            # High-frequency successful packet sends are excluded above.
            level = ("error" if any(x in msg for x in ("[-]", "[SEND-FAIL]", "error:"))
                     else "warning" if "[!]" in msg
                     else "success" if any(x in msg for x in ("[+]", "[★]", "Match found", "finished"))
                     else "info")
            event_type = "worker_output"
            lower_msg = msg.lower()
            # Only actual expiry messages are timeouts. In particular,
            # "startup packets sent; awaiting JOIN_MATCH (timeout 75s)" is
            # a pending deadline, not an expired join; the server may still
            # send JOIN_MATCH afterward.
            actual_timeout_markers = (
                "udp join_match timeout",
                "udp idle timeout",
                "udp handshake timeout",
                "session limit reached without completion packet",
            )
            if any(marker in lower_msg for marker in actual_timeout_markers):
                event_type = "udp_timeout"
            elif "UDP JOIN_MATCH received from server" in msg:
                event_type = "udp_join"
            elif (
                "UDP HELLO received" in msg
                or "UDP ACK received" in msg
                or "UDP startup packets sent; awaiting" in msg
            ):
                event_type = "udp_phase"
            elif "finished (cmd" in msg:
                event_type = "server_finish"
            elif "Profile sync | UID" in msg:
                event_type = "profile_sync"
            db.add_activity_log(
                account_id=matched_acc_id,
                account_label=self.account_labels.get(matched_acc_id, matched_acc_id),
                level=level,
                event_type=event_type,
                message=msg[:800],
            )
        except Exception:
            # A logging failure must never interrupt the live network worker.
            pass

    def _process_account_log_event(self, account_id: str, msg: str) -> None:
        """Parse lv.py log patterns to update account status & counters in real time."""
        try:
            if "TCP gateway connected" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message="TCP Gateway Connected (Online)",
                )
            elif "-> BR match #" in msg and "(request sent)" in msg:
                db.update_account_runtime_state(
                    account_id, status="searching_match",
                    status_message="TCP StartMatch sent; awaiting server match candidate",
                    inc_matches_searched=1, inc_queue_no_join=1,
                    udp_phase="queue_sent",
                )
                streak = db.get_account_by_id(account_id)["queue_no_join_streak"]
                if streak >= QUEUE_NO_JOIN_LIMIT:
                    self._schedule_queue_cooldown(account_id)
            elif "Queue confirmed (BR)" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="searching_match",
                    status_message="In BR Matchmaking Queue",
                )
            elif "Match found [BR]" in msg or "[BR] injected ->" in msg:
                # Match candidate is not a verified UDP session yet.
                db.update_account_runtime_state(
                    account_id,
                    status="searching_match",
                    status_message="Match candidate received; building UDP startup",
                    udp_phase="match_candidate",
                )
            elif "UDP startup packets sent; awaiting" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="searching_match",
                    status_message="UDP startup sent — awaiting JOIN_MATCH from server",
                    udp_phase="startup_sent",
                )
            elif "UDP HELLO received from server" in msg:
                db.update_account_runtime_state(account_id, udp_phase="hello_received")
            elif "UDP ACK received from server" in msg:
                db.update_account_runtime_state(account_id, udp_phase="ack_received")
            elif "UDP JOIN_MATCH received from server" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="in_match",
                    status_message="UDP JOIN_MATCH received; match completion/EXP not yet confirmed",
                    inc_udp_joins=1, reset_queue_no_join=True,
                    udp_phase="joined",
                )
                self._cancel_pending_queue_cooldown(account_id)
            elif "[SEND-OK] PING-REPLY" in msg:
                return
            elif "UDP JOIN_MATCH timeout" in msg:
                db.update_account_runtime_state(
                    account_id, status="reconnecting",
                    status_message="No JOIN_MATCH from server — session timed out and retrying",
                    inc_reconnects=1, inc_udp_timeouts=1,
                    udp_phase="join_timeout",
                )
            elif "UDP idle timeout" in msg:
                db.update_account_runtime_state(
                    account_id, status="reconnecting",
                    status_message="UDP packets stopped — reconnecting and checking profile",
                    inc_reconnects=1, inc_udp_timeouts=1, udp_phase="idle_timeout",
                )
            elif "session limit reached without completion packet" in msg:
                db.update_account_runtime_state(
                    account_id, status="reconnecting",
                    status_message="UDP session limit reached without completion — checking profile",
                    inc_udp_timeouts=1,
                )
            elif "UDP handshake timeout (25s" in msg:
                db.update_account_runtime_state(
                    account_id,
                    status="reconnecting",
                    status_message="UDP handshake timed out (25s) — re-queueing next match...",
                    inc_udp_timeouts=1, udp_phase="handshake_timeout",
                )
            elif "finished (cmd" in msg or "finished (max duration" in msg or "finished (UDP session complete)" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message=msg.replace("[★] ", "").split(" | UID:")[0],
                    level=lvl,
                    exp=exp,
                    inc_matches_completed=1 if "finished (cmd" in msg else 0,
                    server_finish="finished (cmd" in msg,
                    reset_queue_no_join="finished (cmd" in msg,
                    udp_phase="server_finish" if "finished (cmd" in msg else None,
                )
                if "finished (cmd" in msg:
                    self._cancel_pending_queue_cooldown(account_id)
            elif "Profile sync | UID" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    level=lvl,
                    exp=exp,
                )
            elif "| Lvl " in msg and "Next: BR" in msg:
                acc_data = self.active_account_data.get(account_id, {})
                lvl = int(acc_data.get("level") or 0) or None
                exp = int(acc_data.get("exp") or 0) if "exp" in acc_data else None
                db.update_account_runtime_state(
                    account_id,
                    level=lvl,
                    exp=exp,
                    status_message=msg.replace("[*] ", ""),
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcast_state_update())
            except RuntimeError:
                pass
        except Exception:
            pass

    async def run_diagnostics(self, region: str = "ME") -> Dict[str, Any]:
        """Run real DNS resolution and version checks using lv.py functions."""
        t0 = time.perf_counter()
        resolved_ip = await lv.resolve_host_cloudflare("clientbp.ppmainecoonghj.com")
        dns_ms = round((time.perf_counter() - t0) * 1000, 1)

        play_ver = await lv.get_playstore_version()
        ver_cfg = await lv.version_config(region=region)

        if ver_cfg:
            rel_ver, rem_ver, srv_url = ver_cfg
            ver_ok = True
        else:
            rel_ver = lv.headers.get("ReleaseVersion", "OB55")
            rem_ver = "Unavailable (version API offline)"
            srv_url = lv.REGION_SERVERS.get(region.upper(), "https://clientbp.ppmainecoonghj.com/")
            ver_ok = False

        self.diagnostics = {
            "last_checked": db.utc_now_iso(),
            "playstore_version": play_ver or "Unavailable",
            "release_version": rel_ver,
            "remote_version": rem_ver,
            "server_url": srv_url,
            "dns_resolved_ip": resolved_ip,
            "dns_latency_ms": dns_ms,
            "cloudflare_dns": lv.CLOUDFLARE_PRIMARY_DNS,
            "version_api_ok": ver_ok,
        }
        return self.diagnostics

    def _cancel_pending_queue_cooldown(self, account_id: str) -> None:
        """A server join/end makes a 100-request stall alert obsolete."""
        data = self.active_account_data.get(account_id)
        if data:
            data['_queue_cooldown_pending'] = False
        task = self.queue_cooldown_tasks.get(account_id)
        # Once actually offline, a manual Stop (not a delayed JOIN) owns cancellation.
        current = db.get_account_by_id(account_id)
        if task and not task.done() and current and current['status'] != 'cooldown':
            task.cancel()

    def _schedule_queue_cooldown(self, account_id: str) -> None:
        task = self.queue_cooldown_tasks.get(account_id)
        if task and not task.done():
            return
        if not self.is_account_running(account_id):
            return
        data = self.active_account_data.get(account_id)
        if data:
            data['_queue_cooldown_pending'] = True
        self.queue_cooldown_tasks[account_id] = asyncio.create_task(
            self._queue_cooldown_lifecycle(account_id))

    async def _queue_cooldown_lifecycle(self, account_id: str,
                                        existing_until: Optional[str] = None) -> None:
        """Stop all sockets; persist 10-minute offline deadline; restart this ID only."""
        myself = asyncio.current_task()
        try:
            if existing_until is None:
                # Allow the 100th request's in-flight match/UDP handshake to finish.
                # Do not send request #101 during this short observation window.
                await asyncio.sleep(QUEUE_OBSERVATION_SECONDS)
                acc = db.get_account_by_id(account_id)
                if not acc or acc['queue_no_join_streak'] < QUEUE_NO_JOIN_LIMIT:
                    return
                if acc['udp_phase'] in ('startup_sent', 'hello_received', 'ack_received') and acc['udp_startup_at']:
                    started = datetime.fromisoformat(acc['udp_startup_at'].replace('Z', '+00:00')).timestamp()
                    remaining = max(0, lv.UDP_JOIN_TIMEOUT_SECONDS + 5 - (time.time() - started))
                    if remaining:
                        await asyncio.sleep(remaining)
                acc = db.get_account_by_id(account_id)
                if not acc or acc['queue_no_join_streak'] < QUEUE_NO_JOIN_LIMIT:
                    return
                stopped = await self.stop_account(account_id, reason="100 queue requests without UDP JOIN; 10-minute cooldown",
                                                  broadcast=False, cancel_cooldown=False)
                if not stopped['worker_stopped']:
                    db.update_account_runtime_state(account_id, status="error",
                        status_message="Worker could not stop safely; automatic restart cancelled")
                    return
                until = datetime.fromtimestamp(time.time() + QUEUE_COOLDOWN_SECONDS, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                db.set_queue_cooldown(account_id, until, increment=True)
                db.update_account_runtime_state(account_id, status="cooldown",
                    status_message=f"Fully offline: {QUEUE_NO_JOIN_LIMIT} queue requests without JOIN; auto restart at {until}")
                db.add_activity_log(account_id, acc['display_name'], 'warning',
                    'queue_cooldown', f"No UDP JOIN after {QUEUE_NO_JOIN_LIMIT} sent queue requests. "
                    f"All worker connections closed; auto restart at {until}.")
                await self.broadcast_state_update(force=True)
            else:
                until = existing_until
            while True:
                acc = db.get_account_by_id(account_id)
                if not acc or acc['status'] != 'cooldown' or acc['cooldown_until'] != until:
                    return
                remaining = datetime.fromisoformat(until.replace('Z', '+00:00')).timestamp() - time.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                # Capacity may be full at the deadline: stay offline and retry later.
                if sum(not t.done() for t in self.workers.values()) >= MAX_ACTIVE_WORKERS:
                    until = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                    db.set_queue_cooldown(account_id, until)
                    db.update_account_runtime_state(account_id, status='cooldown',
                        status_message='Worker capacity full; offline, retrying in 60s')
                    await self.broadcast_state_update(force=True)
                    continue
                db.set_queue_cooldown(account_id, None)
                db.update_account_runtime_state(account_id, status='inactive', reset_queue_no_join=True,
                                                status_message='Cooldown ended; auto-starting')
                try:
                    await self.start_account(account_id)
                except Exception as exc:
                    db.update_account_runtime_state(account_id, status='error',
                        status_message=f'Automatic restart failed: {type(exc).__name__}')
                    db.add_activity_log(account_id, acc['display_name'], 'error',
                        'queue_cooldown', f'Automatic restart failed: {type(exc).__name__}. Start manually after checking logs.')
                    await self.broadcast_state_update(force=True)
                else:
                    db.add_activity_log(account_id, acc['display_name'], 'info',
                        'queue_resume', '10-minute offline cooldown finished; this account restarted automatically.')
                return
        finally:
            if self.queue_cooldown_tasks.get(account_id) is myself:
                data = self.active_account_data.get(account_id)
                if data:
                    data['_queue_cooldown_pending'] = False
                self.queue_cooldown_tasks.pop(account_id, None)

    def is_account_running(self, account_id: str) -> bool:
        t = self.workers.get(account_id)
        return bool(t and not t.done())

    async def start_account(self, account_id: str, *, broadcast: bool = True) -> Dict[str, Any]:
        if self.is_account_running(account_id):
            return {"ok": True, "message": "Account worker is already running.", "account": db.get_account_by_id(account_id)}
        saved = db.get_account_by_id(account_id)
        if saved and saved['status'] == 'cooldown':
            raise ValueError("Account is offline for queue cooldown. Click Stop to cancel, then Start manually.")
        if saved:
            _daily = self.daily_status_for_account(account_id, saved.get('level'))
            if _daily['reached']:
                raise ValueError(
                    "Daily EXP target already reached for this account "
                    f"({_daily['exp_gained']:,} / {_daily['limit']:,} EXP · LV {int(saved.get('level') or 1)}). "
                    "Use 'Reset today' before starting it again."
                )

        if sum(not t.done() for t in self.workers.values()) >= MAX_ACTIVE_WORKERS:
            raise ValueError(f"Worker capacity reached ({MAX_ACTIVE_WORKERS}). Increase MAX_ACTIVE_WORKERS only if your server can handle it.")

        cred_row = db.get_account_credentials_internal(account_id)
        if not cred_row:
            raise ValueError(f"Account '{account_id}' not found.")

        uid = cred_row["uid"]
        auth_type = cred_row.get("auth_type") or "uid_password"
        password = cred_row.get("password_plain") or ""
        access_token = cred_row.get("access_token_plain") or ""
        if auth_type == "uid_password" and not password:
            db.record_account_error(account_id, "Credentials", "Stored UID password could not be decrypted or is empty.")
            db.update_account_runtime_state(
                account_id,
                status="error",
                status_message="Missing or invalid encrypted password. Please edit account credentials.",
            )
            raise ValueError("Missing or undecryptable password for this account.")
        if auth_type == "access_token" and not access_token:
            db.record_account_error(account_id, "Credentials", "Stored Access Token could not be decrypted or is empty.")
            db.update_account_runtime_state(
                account_id,
                status="error",
                status_message="Missing or invalid encrypted Access Token. Please edit account credentials.",
            )
            raise ValueError("Missing or undecryptable Access Token for this account.")
        if auth_type not in {"uid_password", "access_token"}:
            raise ValueError("Unsupported account authentication method.")

        self.uid_to_account_id[uid] = account_id
        self.account_labels[account_id] = f"{cred_row['display_name']} ({uid})"
        self.worker_started_at[account_id] = time.time()

        db.update_account_runtime_state(
            account_id,
            status="authenticating",
            status_message=("Starting worker — Access Token → JWT API login..." if auth_type == "access_token"
                            else "Starting worker — verifying version & Garena OAuth..."),
            reset_queue_no_join=True, udp_phase="none",
        )
        db.add_activity_log(
            account_id=account_id,
            account_label=f"{cred_row['display_name']} ({uid})",
            level="info",
            event_type="worker_start",
            message=f"Started worker for '{cred_row['display_name']}' (UID {uid}, Region {cred_row['region']}).",
        )

        task = asyncio.create_task(self._account_worker_lifecycle(account_id))
        self.workers[account_id] = task
        if broadcast:
            await self.broadcast_state_update()
        return {"ok": True, "message": "Account worker started.", "account": db.get_account_by_id(account_id)}

    async def stop_account(self, account_id: str, reason: str = "Stopped by administrator", *,
                           broadcast: bool = True, cancel_cooldown: bool = True) -> Dict[str, Any]:
        if cancel_cooldown:
            pending = self.queue_cooldown_tasks.pop(account_id, None)
            if pending and pending is not asyncio.current_task():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            db.set_queue_cooldown(account_id, None)
        task = self.workers.get(account_id)
        started = self.worker_started_at.pop(account_id, None)
        elapsed = int(time.time() - started) if started else 0
        # Wait for the real worker and all child sockets. Never claim fully
        # offline, or restart a second worker, if cancellation did not finish.
        if task and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=10.0)
        worker_stopped = not task or task.done()
        if not worker_stopped:
            if started:
                self.worker_started_at[account_id] = started
            db.update_account_runtime_state(account_id, status="error",
                status_message="Worker shutdown timed out; NOT safe to auto-restart")
            if broadcast:
                await self.broadcast_state_update()
            return {"ok": False, "worker_stopped": False,
                    "message": "Worker shutdown timed out; check its sockets before restarting.",
                    "account": db.get_account_by_id(account_id)}
        if task:
            await asyncio.gather(task, return_exceptions=True)
        if self.workers.get(account_id) is task:
            self.workers.pop(account_id, None)
        self.active_account_data.pop(account_id, None)

        self.uid_to_account_id = {uid: aid for uid, aid in self.uid_to_account_id.items() if aid != account_id}
        self.game_id_to_account_id = {gid: aid for gid, aid in self.game_id_to_account_id.items() if aid != account_id}
        self.account_labels.pop(account_id, None)
        self._profile_error_logged_at.pop(account_id, None)
        self._manual_profile_checked_at.pop(account_id, None)
        self._exp_stall_logged_at.pop(account_id, None)
        self._profile_error_logged_at.pop(account_id, None)
        self._profile_fail_streaks.pop(account_id, None)
        acc = db.get_account_by_id(account_id)
        if acc:
            db.update_account_runtime_state(
                account_id,
                status="inactive",
                status_message=reason,
                inc_uptime_seconds=elapsed,
            )
            db.add_activity_log(
                account_id=account_id,
                account_label=f"{acc['display_name']} ({acc['uid']})",
                level="warning",
                event_type="worker_stop",
                message=f"Worker stopped for '{acc['display_name']}': {reason}.",
            )
        if broadcast:
            await self.broadcast_state_update()
        return {"ok": True, "worker_stopped": True,
                "message": "Account worker stopped.", "account": db.get_account_by_id(account_id)}

    async def restart_account(self, account_id: str) -> Dict[str, Any]:
        result = await self.stop_account(account_id, reason="Restarting worker...")
        if not result["worker_stopped"]:
            raise ValueError(result["message"])
        await asyncio.sleep(0.2)
        return await self.start_account(account_id)

    async def _acquire_start_slot(self) -> float:
        """Hold each worker back until ACCOUNT_START_STAGGER_SECONDS after the previous one.

        Clicking "Start all" used to make every account run its version check, OAuth and
        login within the same second. Garena answers that burst by throttling, after which
        the accounts sit in a retry loop and never reach a match (so: no EXP at all).
        The stagger only delays logins; the response to the operator is immediate.
        """
        if ACCOUNT_START_STAGGER_SECONDS <= 0:
            return 0.0
        if self._start_slot_lock is None:
            self._start_slot_lock = asyncio.Lock()
        waited = 0.0
        async with self._start_slot_lock:
            while True:
                gap = ACCOUNT_START_STAGGER_SECONDS - (time.monotonic() - self._last_start_at)
                if gap <= 0:
                    break
                await asyncio.sleep(gap)
                waited += gap
            self._last_start_at = time.monotonic()
        return waited

    async def _account_worker_lifecycle(self, account_id: str) -> None:
        """
        Executes the full lv.py login and BR worker loop for a single managed account,
        capturing accurate per-stage status and errors without fabricating success.
        """
        cred = db.get_account_credentials_internal(account_id)
        if not cred:
            return

        try:
            waited = await self._acquire_start_slot()
            if waited > 0.5:
                db.update_account_runtime_state(
                    account_id, status="authenticating",
                    status_message=f"Waiting {int(waited)}s so accounts do not log in at the same second...")
        except Exception:
            pass

        uid = str(cred["uid"])
        auth_type = str(cred.get("auth_type") or "uid_password")
        password = str(cred.get("password_plain") or "")
        stored_access_token = str(cred.get("access_token_plain") or "")
        region = str(cred.get("region") or "ME").upper()
        display_name = str(cred.get("display_name") or uid)
        label = f"{display_name} ({uid})"

        max_consecutive_auth_failures = 3
        auth_failures = 0

        try:
            while True:
                db.update_account_runtime_state(
                    account_id,
                    status="authenticating",
                    status_message=f"Step 1/4: Fetching version config for region {region}...",
                )
                await self.broadcast_state_update()

                async with lv._LOGIN_SEMAPHORE:
                    verconfig_res = await lv.version_config(region=region)
                if verconfig_res is None:
                    # Fallback to PlayStore version + standard region server URL if version API is unreachable
                    async with lv._LOGIN_SEMAPHORE:
                        play_ver = await lv.get_playstore_version()
                    release_version = lv.headers.get("ReleaseVersion", "OB55")
                    client_version = play_ver or "1.132.8"
                    server_url = lv.REGION_SERVERS.get(region, "https://clientbp.ppmainecoonghj.com/")
                    db.record_account_error(
                        account_id,
                        "VersionConfig",
                        f"Whitehawk version API unreachable; using fallback release={release_version}, client={client_version}.",
                    )
                else:
                    release_version, client_version, server_url = verconfig_res

                api_player_info = {}
                api_region = ""
                api_nickname = ""
                api_game_id = ""
                api_auth_mode = ""
                if auth_type == "access_token":
                    db.update_account_runtime_state(
                        account_id,
                        status="authenticating",
                        status_message="Step 2/4: Resolving Access Token with the JWT API...",
                    )
                    await self.broadcast_state_update()
                    exchange_error = ""
                    try:
                        async with lv._LOGIN_SEMAPHORE:
                            jwt_response = await lv.exchange_access_token_for_jwt(stored_access_token)
                    except lv.AccessJWTExchangeError as exc:
                        # This exception contains a deliberately sanitized, user-safe summary only.
                        jwt_response = None
                        exchange_error = str(exc)
                    if jwt_response is None:
                        auth_failures += 1
                        err_msg = (
                            "Access Token → JWT exchange failed: "
                            f"{exchange_error or 'API returned no usable response.'}"
                        )
                        db.record_account_error(account_id, "AccessJWT", err_msg)
                        db.add_activity_log(
                            account_id=account_id,
                            account_label=label,
                            level="error",
                            event_type="access_jwt_failed",
                            message=f"{err_msg} Attempt {auth_failures}/{max_consecutive_auth_failures}.",
                        )
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(
                                account_id,
                                status="error",
                                status_message=(
                                    "Access Token → JWT exchange failed after 3 attempts. "
                                    f"Last result: {exchange_error or 'no usable API response'}"
                                ),
                            )
                            await self.broadcast_state_update()
                            break
                        db.update_account_runtime_state(
                            account_id,
                            status="reconnecting",
                            status_message=(
                                "Access Token exchange failed — retrying in 15s "
                                f"({auth_failures}/{max_consecutive_auth_failures})."
                            ),
                            inc_reconnects=1,
                        )
                        await self.broadcast_state_update()
                        await asyncio.sleep(15)
                        continue

                    # For the API's direct-JWT branch, Open ID is not returned. The JWT is
                    # still used as the GetLoginData bearer token, and player_info supplies
                    # optional account/platform metadata. Actual game-server acceptance is
                    # validated by GetLoginData below.
                    open_id = str(jwt_response.get("open_id") or "")
                    login_access_token = stored_access_token
                    session_jwt = jwt_response["jwt"]
                    platform = jwt_response["platform_type_used"]
                    api_auth_mode = str(
                        jwt_response.get("auth_mode") or ("direct_jwt" if not open_id else "access_token")
                    )
                    api_player_info = jwt_response.get("player_info") or {}
                    api_game_id = str(jwt_response.get("account_id") or "")
                    api_region = str(jwt_response.get("region") or "").upper()
                    api_nickname = str(jwt_response.get("nickname") or "")
                    db.add_activity_log(
                        account_id=account_id,
                        account_label=label,
                        level="info",
                        event_type="access_jwt_exchange",
                        message=(
                            f"Access Token API returned mode={api_auth_mode}; "
                            f"Open ID {'present' if open_id else 'not returned'}; "
                            f"platform={platform}; API region={api_region or 'unknown'}."
                        ),
                    )
                else:
                    db.update_account_runtime_state(
                        account_id,
                        status="authenticating",
                        status_message="Step 2/4: Requesting Garena Guest OAuth token...",
                    )
                    await self.broadcast_state_update()

                    async with lv._LOGIN_SEMAPHORE:
                        tokengrant_response = await lv.get_access_token(uid, password)
                    if tokengrant_response is None:
                        auth_failures += 1
                        err_msg = (
                            f"Garena OAuth token grant failed for UID {uid} "
                            f"(attempt {auth_failures}/{max_consecutive_auth_failures}). "
                            "Check guest UID/password or Garena OAuth availability."
                        )
                        db.record_account_error(account_id, "GarenaOAuth", err_msg)
                        db.add_activity_log(
                            account_id=account_id,
                            account_label=label,
                            level="error",
                            event_type="oauth_failed",
                            message=err_msg,
                        )
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(
                                account_id,
                                status="error",
                                status_message="OAuth authentication failed after 3 attempts (invalid UID/password or rate-limited).",
                            )
                            await self.broadcast_state_update()
                            break

                        db.update_account_runtime_state(
                            account_id,
                            status="reconnecting",
                            status_message=f"OAuth failed (attempt {auth_failures}/{max_consecutive_auth_failures}) — retrying in 15s...",
                            inc_reconnects=1,
                        )
                        await self.broadcast_state_update()
                        await asyncio.sleep(15)
                        continue

                    open_id, login_access_token, platform = tokengrant_response
                    session_jwt = ""


                db.update_account_runtime_state(
                    account_id,
                    status="authenticating",
                    status_message=(
                        "Step 3/4: Preparing direct-JWT GetLoginData payload..."
                        if auth_type == "access_token" and api_auth_mode == "direct_jwt"
                        else "Step 3/4: Preparing Access Token payload for regional MajorLogin..."
                        if auth_type == "access_token"
                        else "Step 3/4: Encrypting & sending MajorLogin payload..."
                    ),
                )
                await self.broadcast_state_update()

                device_info = lv.get_device_for_account(uid)
                if auth_type == "access_token":
                    login_payload_data = await lv.build_majorlogin_payload(
                        open_id, login_access_token, platform, client_version, device_info,
                        honor_platform=True,
                    )
                else:
                    login_payload_data = await lv.build_majorlogin_payload(
                        open_id, login_access_token, platform, client_version, device_info
                    )
                if login_payload_data is None:
                    err_msg = "Failed to build/encrypt MajorLogin protobuf payload."
                    db.record_account_error(account_id, "MajorLoginBuild", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break

                direct_jwt_login = auth_type == "access_token" and api_auth_mode == "direct_jwt"
                if direct_jwt_login:
                    # An already-JWT input has no Open ID, so it must use the JWT
                    # returned by the API directly. Game-server acceptance is unverified.
                    try:
                        api_account_id = int(api_game_id) if api_game_id.isdigit() else 0
                    except (TypeError, ValueError):
                        api_account_id = 0
                    api_login_region = api_region if api_region in lv.REGION_PREFIX else region
                    majorlogin_response = SimpleNamespace(
                        account_id=api_account_id,
                        region=api_login_region,
                        token=session_jwt,
                        server_time=int(time.time()),
                        aes_ak=lv.AES_KEY,
                        iv_i=lv.AES_IV,
                        url=server_url,
                    )
                else:
                    # For a raw Access Token, the API resolves the Open ID/platform first.
                    # Reuse this app's region-specific MajorLogin path so GetLoginData gets
                    # a session JWT issued by the same game-server region/version.
                    async with lv._LOGIN_SEMAPHORE:
                        majorlogin_response = await lv.send_majorlogin(
                            login_payload_data, release_version, server_url
                        )
                    if majorlogin_response is None:
                        auth_failures += 1
                        err_msg = f"MajorLogin rejected or timed out from {server_url} (release {release_version})."
                        db.record_account_error(account_id, "MajorLogin", err_msg)
                        if auth_failures >= max_consecutive_auth_failures:
                            db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                            await self.broadcast_state_update()
                            break
                        db.update_account_runtime_state(
                            account_id,
                            status="reconnecting",
                            status_message=f"MajorLogin failed — retrying in 15s ({auth_failures}/{max_consecutive_auth_failures})...",
                            inc_reconnects=1,
                        )
                        await self.broadcast_state_update()
                        await asyncio.sleep(15)
                        continue


                db.update_account_runtime_state(
                    account_id,
                    status="authenticating",
                    status_message="Step 4/4: Fetching GetLoginData & gateway addresses...",
                )
                await self.broadcast_state_update()

                getlogin_error = ""
                try:
                    async with lv._LOGIN_SEMAPHORE:
                        getlogin_result = await lv.send_getlogin(
                            login_payload_data,
                            majorlogin_response.url or server_url,
                            majorlogin_response.token,
                            release_version,
                        )
                except lv.GetLoginDataError as exc:
                    getlogin_result = None
                    getlogin_error = str(exc)
                if getlogin_result is None:
                    auth_failures += 1
                    err_msg = (
                        f"GetLoginData failed: {getlogin_error}"
                        if getlogin_error else "GetLoginData failed to return profile & gateway addresses."
                    )
                    db.record_account_error(account_id, "GetLoginData", err_msg)
                    if auth_failures >= max_consecutive_auth_failures:
                        db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                        await self.broadcast_state_update()
                        break
                    await asyncio.sleep(15)
                    continue

                res_proto, dict_res = getlogin_result

                raw_acc_id = (
                    majorlogin_response.account_id
                    or getattr(res_proto, "account_id", 0)
                    or lv.get_proto_field(dict_res, 1, 0)
                    or (int(api_game_id) if api_game_id.isdigit() else 0)
                    or 0
                )
                game_acc_id = str(raw_acc_id)
                if not game_acc_id or game_acc_id in ("0", "None"):
                    err_msg = (
                        f"Server returned invalid account_id (0) for UID {uid}. "
                        "Either this Guest UID has not created an in-game character/nickname yet, "
                        "is suspended/banned, or thunderFF_pb2 schema mismatched."
                    )
                    db.record_account_error(account_id, "AccountValidation", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break
                majorlogin_response.account_id = int(game_acc_id)

                existing_owner = self.game_id_to_account_id.get(game_acc_id)
                if existing_owner and existing_owner != account_id and self.is_account_running(existing_owner):
                    err_msg = ("This game account is already connected through another guest UID. "
                               "Concurrent sessions for the same game ID are blocked.")
                    db.record_account_error(account_id, "DuplicateGameAccount", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break
                auth_failures = 0
                self.game_id_to_account_id[game_acc_id] = account_id

                raw_level = lv.get_proto_field(dict_res, 6, None)
                raw_exp = lv.get_proto_field(dict_res, 7, None)
                try:
                    if raw_level is None or raw_exp is None:
                        raise ValueError("Level or EXP field missing from GetLoginData")
                    level = int(raw_level)
                    exp = int(raw_exp)
                    if level < 1 or exp < 0:
                        raise ValueError("Level or EXP value is invalid")
                except (TypeError, ValueError) as exc:
                    err_msg = f"Cannot verify profile EXP: {exc}"
                    db.record_account_error(account_id, "ProfileDecode", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break
                nickname = (
                    res_proto.nickname
                    or lv.get_proto_field(dict_res, 4, None)
                    or api_nickname
                    or f"Player_{game_acc_id}"
                )
                resolved_region = (
                    majorlogin_response.region
                    or lv.get_proto_field(dict_res, 3, None)
                    or api_region
                    or region
                )

                func_addr = res_proto.functional_addrs
                info_addr = res_proto.informational_addrs
                if not isinstance(func_addr, str) or ":" not in func_addr:
                    func_addr = None
                if not isinstance(info_addr, str) or ":" not in info_addr:
                    info_addr = None

                key_val = majorlogin_response.aes_ak
                iv_val = majorlogin_response.iv_i
                if not isinstance(key_val, (bytes, bytearray)) or len(key_val) < 16:
                    key_val = lv.AES_KEY
                if not isinstance(iv_val, (bytes, bytearray)) or len(iv_val) < 16:
                    iv_val = lv.AES_IV

                account_data = {
                    "account_id": majorlogin_response.account_id,
                    "nickname": nickname,
                    "region": resolved_region,
                    "level": level,
                    "exp": exp,
                    "open_id": open_id,
                    "access_token": login_access_token if auth_type == "uid_password" else "",
                    "platform": str(platform),
                    "token": majorlogin_response.token,
                    "server_time": majorlogin_response.server_time,
                    "aes_ak": key_val,
                    "iv_i": iv_val,
                    "functional_addrs": func_addr,
                    "informational_addrs": info_addr,
                    "release_version": release_version,
                    "client_version": client_version,
                    "server_url": majorlogin_response.url or server_url,
                    "login_payload_data": login_payload_data,
                    "auth_type": "guest" if auth_type == "uid_password" else "access_token",
                    "auth_uid": uid,
                    "auth_password": password if auth_type == "uid_password" else "",
                    "mode_id": int(cred.get("mode_id") or 1),
                    "map_id": int(cred.get("map_id") or 1),
                    "_profile_refresh_lock": asyncio.Lock(),
                    "_profile_result_callback": lambda result: self._on_profile_result(account_id, result),
                }
                self.active_account_data[account_id] = account_data

                db.update_account_runtime_state(
                    account_id,
                    status="online",
                    status_message=f"Login OK — Lvl {level} ({exp:,} EXP) | Connecting to BR gateway...",
                    game_account_id=game_acc_id,
                    nickname=nickname,
                    region=resolved_region,
                    level=level,
                    exp=exp,
                    start_session=True,
                )
                db.add_activity_log(
                    account_id=account_id,
                    account_label=label,
                    level="success",
                    event_type="login_success",
                    message=f"Authenticated game account {game_acc_id} via {'Access Token' if auth_type == 'access_token' else 'UID/password'} ({nickname}) | Lvl {level} | Region {resolved_region}.",
                )
                await self.broadcast_state_update()

                if not func_addr:
                    err_msg = f"No functional TCP gateway address returned for account {game_acc_id}."
                    db.record_account_error(account_id, "TCPGateway", err_msg)
                    db.update_account_runtime_state(account_id, status="error", status_message=err_msg)
                    await self.broadcast_state_update()
                    break

                await lv.run_account_worker(account_data, uid)

                db.update_account_runtime_state(
                    account_id,
                    status="reconnecting",
                    status_message="Session ended — reconnecting in 3s...",
                    inc_reconnects=1,
                )
                await self.broadcast_state_update()
                await asyncio.sleep(3)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_msg = redact_sensitive(f"Unexpected worker exception: {e}")
            db.record_account_error(account_id, "WorkerRuntime", err_msg)
            db.update_account_runtime_state(
                account_id,
                status="error",
                status_message=err_msg,
            )
            db.add_activity_log(
                account_id=account_id,
                account_label=label,
                level="error",
                event_type="worker_exception",
                message=err_msg,
            )
            await self.broadcast_state_update()
        finally:
            started = self.worker_started_at.pop(account_id, None)
            if started:
                db.update_account_runtime_state(
                    account_id,
                    inc_uptime_seconds=int(time.time() - started),
                )
            self.active_account_data.pop(account_id, None)
            # Leave terminal error in the DB, but remove abandoned mappings.
            if self.workers.get(account_id) is asyncio.current_task():
                self.workers.pop(account_id, None)
            self.uid_to_account_id.pop(uid, None)
            self.game_id_to_account_id = {k: v for k, v in self.game_id_to_account_id.items() if v != account_id}
            self.account_labels.pop(account_id, None)

    def _on_profile_result(self, account_id: str, result: Dict[str, Any]) -> None:
        """Persist polling outcome, distinct from gameplay events/last activity."""
        ok = bool(result.get("ok"))
        changed = bool(result.get("changed"))
        reason = str(result.get("reason") or ("Server profile unchanged" if ok else "Profile check failed"))
        exp_delta = int(result.get("exp_delta") or 0) if ok and changed else 0
        db.record_profile_check(account_id, ok=ok, changed=changed,
                                message=reason, exp_delta=exp_delta)
        if ok and changed and exp_delta > 0:
            db.add_activity_log(account_id=account_id,
                account_label=self.account_labels.get(account_id, account_id),
                level="success", event_type="profile_gain",
                message=f"Server profile EXP increased by {exp_delta:,}; current EXP {int(result['exp']):,}. UDP join/end alone does not prove reward.")
            try:
                self._track_daily_exp(account_id, exp_delta, int(result.get("level") or 0), int(result.get("exp") or 0))
            except Exception:
                pass
        self._profile_fail_streaks[account_id] = int(result.get("fail_streak") or 0)
        self._note_profile_result(ok=ok, forced=bool(result.get("forced")),
                                  fail_streak=int(result.get("fail_streak") or 0))
        try:
            self._evaluate_exp_stall(account_id, ok=ok, changed=changed, exp_delta=exp_delta)
        except Exception:
            pass
        if ok and changed:
            # V17: a genuine level increase now becomes its own recorded event so the
            # dashboard, activity feed and live console can label it exactly as LEVEL UP.
            try:
                previous = db.get_account_by_id(account_id) or {}
                previous_level = int(previous.get("level") or 0)
                new_level = int(result["level"])
                if previous_level and new_level > previous_level:
                    db.add_activity_log(
                        account_id=account_id,
                        account_label=self.account_labels.get(account_id, account_id),
                        level="success",
                        event_type="level_up",
                        message=(
                            f"LEVEL UP recorded from server profile: LV {previous_level} -> "
                            f"LV {new_level} | total EXP {int(result['exp']):,} | "
                            f"next threshold {int(result.get('exp_to_next_level') or 0):,} | "
                            "saved observation, not a reward promise."
                        ),
                    )
            except Exception:
                pass
            db.update_account_runtime_state(account_id,
                level=int(result["level"]), exp=int(result["exp"]))
        elif not ok:
            now = time.monotonic()
            last_error_log = self._profile_error_logged_at.get(account_id)
            if last_error_log is None or now - last_error_log >= 300:
                self._profile_error_logged_at[account_id] = now
                db.add_activity_log(account_id=account_id,
                    account_label=self.account_labels.get(account_id, account_id),
                    level="warning", event_type="profile_check_failed",
                    message=f"Profile check could not confirm current EXP: {reason}")
        try:
            asyncio.get_running_loop().create_task(self.broadcast_state_update())
        except RuntimeError:
            pass

    # ---------------------------------------------------------------------------------
    # V18 daily EXP target — real verified gains only; the worker is stopped when the
    # day's target for that level is reached (LV 1-29: 5,000 · LV 30+: 50,000 by default).
    # ---------------------------------------------------------------------------------
    def _note_profile_result(self, *, ok: bool, forced: bool = False, fail_streak: int = 0) -> None:
        """Bounded, in-memory bookkeeping for the safety panel (never persisted per check)."""
        now = time.monotonic()
        self._profile_checks_total += 1
        self._profile_check_times.append(now)
        if forced:
            self._profile_forced_total += 1
        if not ok:
            self._profile_failures_total += 1
            self._profile_failure_times.append(now)

    def safety_payload(self) -> Dict[str, Any]:
        """Traffic and guard rails for both sides: hosting box and game API.

        Everything here is derived from counters already in memory - the panel never
        triggers an extra request to the game servers or to the database.
        """
        now = time.monotonic()
        window = 60.0
        checks_minute = sum(1 for t in self._profile_check_times if now - t <= window)
        failures_minute = sum(1 for t in self._profile_failure_times if now - t <= window)
        throttled = sum(1 for acc_id in self.workers
                        if int(self._profile_fail_streaks.get(acc_id, 0)) >= lv.GAME_API_BACKOFF_THRESHOLD)
        active_workers = len(self.workers)
        return {
            "profile_checks_per_minute": checks_minute,
            "profile_failures_per_minute": failures_minute,
            "profile_checks_total": self._profile_checks_total,
            "profile_failures_total": self._profile_failures_total,
            "profile_forced_total": self._profile_forced_total,
            "throttled_accounts": throttled,
            "poll_floor_seconds": lv.PROFILE_POLL_SECONDS,
            "poll_max_seconds": lv.PROFILE_POLL_MAX_SECONDS,
            "forced_min_interval_seconds": lv.PROFILE_FORCED_MIN_INTERVAL_SECONDS,
            "check_concurrency": lv.PROFILE_CHECK_CONCURRENCY,
            "backoff_threshold": lv.GAME_API_BACKOFF_THRESHOLD,
            "active_workers": active_workers,
            "max_workers": MAX_ACTIVE_WORKERS,
            "max_concurrent_logins": MAX_CONCURRENT_LOGINS,
            "start_stagger_seconds": ACCOUNT_START_STAGGER_SECONDS,
            "matchmaking_min_gap_seconds": lv.MATCHMAKING_MIN_GAP_SECONDS,
            "game_api_state": "throttled" if throttled else "normal",
        }

    def _evaluate_exp_stall(self, account_id: str, *, ok: bool, changed: bool,
                            exp_delta: int) -> None:
        """Detect "the account keeps running but the server never credits EXP".

        Only the real server profile is used as evidence: a running account whose
        profile checks answer correctly while the EXP value never moves is recorded
        with the exact numbers, so an operator can tell a game-side problem (no
        settlement, mode without rewards, the game's own daily cap) apart from a
        dashboard problem.
        """
        row = db.get_account_by_id(account_id)
        if not row:
            return
        status = str(row.get("status") or "")
        running = status in RUNNING_ACCOUNT_STATES

        if not running:
            if row.get("exp_stall_since"):
                db.clear_exp_stall(account_id)
            return

        if ok and changed and exp_delta > 0:
            if row.get("exp_stall_since"):
                elapsed = self._iso_age_seconds(row.get("exp_stall_since"))
                db.clear_exp_stall(account_id)
                db.add_activity_log(
                    account_id=account_id,
                    account_label=self.account_labels.get(account_id, account_id),
                    level="success", event_type="exp_gain_resumed",
                    message=(f"Server profile credited EXP again after {humanize_seconds(elapsed)} without a gain: "
                             f"+{int(exp_delta):,} EXP · now {int(row.get('exp') or 0):,} EXP (LV {row.get('level')})."))
            self._exp_stall_logged_at.pop(account_id, None)
            return

        if ok and changed:
            # The server profile did move (level/EXP changed but not upward) — not a stall.
            if row.get("exp_stall_since"):
                db.clear_exp_stall(account_id)
            return

        if not ok:
            # A failing profile check is a different problem (tracked separately).
            return

        reference = row.get("session_last_exp_gain_at") or row.get("session_started_at")
        if not reference:
            return
        elapsed = self._iso_age_seconds(reference)
        window = max(5, EXP_STALL_MINUTES) * 60
        if elapsed < window:
            if row.get("exp_stall_since"):
                db.clear_exp_stall(account_id)
            return

        joins = int(row.get("udp_join_count") or 0)
        matches = int(row.get("server_finish_count") or 0)
        message = (
            f"No verified EXP increase for {humanize_seconds(elapsed)} while this account keeps running. "
            f"Every profile check was answered by the game server, but the EXP value stayed at "
            f"{int(row.get('exp') or 0):,} (LV {row.get('level')}). Totals so far: {joins:,} UDP joins, "
            f"{matches:,} matches ended. The EXP reward is decided by the game server after a match "
            "settles — a join without settlement (or a mode/day limit without rewards) credits 0 EXP."
        )
        checks = int(row.get("exp_stall_checks") or 0) + 1
        since = row.get("exp_stall_since") or reference
        db.set_exp_stall_state(account_id, since=since, checks=checks, message=message)

        now = time.monotonic()
        relog_after = max(5, EXP_STALL_RELOG_MINUTES) * 60
        last_logged = self._exp_stall_logged_at.get(account_id)
        # Never treat a missing record as "far in the past": monotonic() starts at an
        # arbitrary point (a freshly booted host can report a few hundred seconds), which
        # would silently swallow the first warning.
        if last_logged is None or now - last_logged >= relog_after:
            self._exp_stall_logged_at[account_id] = now
            db.add_activity_log(
                account_id=account_id,
                account_label=self.account_labels.get(account_id, account_id),
                level="warning", event_type="exp_stall",
                message=message,
            )

    @staticmethod
    def _iso_age_seconds(value: Optional[str]) -> float:
        """Seconds since an ISO-8601 UTC stamp; 0 when it cannot be parsed."""
        if not value:
            return 0.0
        try:
            clean = str(value).replace("Z", "+00:00")
            stamp = datetime.fromisoformat(clean)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        except Exception:
            return 0.0

    def daily_status_for_account(self, account_id: str, level: Optional[int] = None) -> Dict[str, Any]:
        """Today's verified gain, the level-based cap and whether the account is done for today."""
        day = db.daily_day_key()
        row = db.get_daily_exp(account_id, day=day)
        if level is None:
            acc = db.get_account_by_id(account_id) or {}
            level = acc.get("level")
        limit = daily_limit_for_level(level)
        gained = int(row.get("exp_gained") or 0)
        return {
            "day": day, "exp_gained": gained, "limit": limit,
            "remaining": max(0, limit - gained) if limit else None,
            "reached": bool(limit) and (gained >= limit or bool(row.get("reached_at"))),
            "auto_stopped": bool(row.get("auto_stopped")),
            "reached_at": row.get("reached_at"), "stopped_at": row.get("stopped_at"),
            "reset_at": db.daily_reset_at(day),
        }

    def _track_daily_exp(self, account_id: str, delta: int, level: int, total_exp: int) -> None:
        """Count a server-verified EXP increase and queue the automatic stop at the target."""
        if not DAILY_EXP_ENABLED or delta <= 0:
            return
        day = db.daily_day_key()
        limit = daily_limit_for_level(level)
        if not limit:
            return
        already_gained = int(db.get_daily_exp(account_id, day=day).get("exp_gained") or 0)
        if already_gained >= limit:
            return
        # Never count past the target: the day's quota stops exactly at the limit.
        row = db.add_daily_exp(account_id, min(int(delta), limit - already_gained), day=day)
        gained = int(row.get("exp_gained") or 0)
        if gained < limit:
            return
        db.mark_daily_limit_reached(account_id, day=day, total=gained)
        already = bool(row.get("auto_stopped")) or account_id in self._daily_stop_inflight
        if already:
            return
        self._daily_stop_inflight.add(account_id)
        label = self.account_labels.get(account_id, account_id)
        db.add_activity_log(account_id=account_id, account_label=label, level="warning",
            event_type="daily_limit_reached",
            message=(f"Daily EXP target reached: {gained:,} / {limit:,} EXP verified today "
                     f"(LV {level}). Account is being set offline automatically until the next daily reset."))
        try:
            asyncio.get_running_loop().create_task(self._auto_offline_for_daily_limit(account_id, gained, limit, day))
        except RuntimeError:
            self._daily_stop_inflight.discard(account_id)

    async def _auto_offline_for_daily_limit(self, account_id: str, gained: int, limit: int, day: str) -> None:
        reason = (f"Auto offline — daily EXP target reached ({gained:,} / {limit:,} EXP verified today). "
                  f"Starts again after the {day} reset.")
        try:
            res = await self.stop_account(account_id, reason=reason, broadcast=False, cancel_cooldown=True)
            if res.get("worker_stopped"):
                db.mark_daily_auto_stopped(account_id, day=day)
                db.update_account_runtime_state(account_id, status="inactive", status_message=reason)
                db.add_activity_log(account_id=account_id, account_label="System", level="info",
                    event_type="daily_auto_offline",
                    message=f"Worker stopped automatically: {gained:,} / {limit:,} EXP verified today. Next reset: {db.daily_reset_at(day)}.")
            else:
                db.record_account_error(account_id, "DailyLimit",
                    "Daily EXP target reached but the worker did not stop; retrying on the next profile check.")
                db.add_activity_log(account_id=account_id, account_label="System", level="error",
                    event_type="daily_limit_stop_failed",
                    message="Daily EXP target reached but the worker could not be stopped safely; it will retry.")
        except Exception as exc:
            db.record_account_error(account_id, "DailyLimit", f"Auto-offline failed: {exc}")
            db.add_activity_log(account_id=account_id, account_label="System", level="error",
                event_type="daily_limit_stop_failed",
                message=f"Auto-offline could not run: {exc}")
        finally:
            self._daily_stop_inflight.discard(account_id)
            try:
                await self.broadcast_state_update(force=True)
            except Exception:
                pass

    async def apply_daily_reset(self, account_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Clear today's counters (admin action) and optionally restart accounts that are done for today."""
        ids = list(account_ids) if account_ids else [a["id"] for a in db.list_accounts()]
        cleared = 0
        resumed: List[str] = []
        for acc_id in ids:
            try:
                cleared += db.reset_daily_exp(acc_id)
                self._daily_stop_inflight.discard(acc_id)
            except Exception:
                continue
        db.purge_old_daily_exp()
        db.add_activity_log(account_id=None, account_label="Administrator", level="info",
            event_type="daily_reset",
            message=f"Daily EXP counters cleared for {len(ids)} account(s) by the administrator.")
        if DAILY_EXP_AUTO_RESUME:
            for acc_id in ids:
                acc = db.get_account_by_id(acc_id)
                if acc and acc.get("status") == "inactive" and not self.is_account_running(acc_id):
                    try:
                        await self.start_account(acc_id, broadcast=False)
                        resumed.append(acc_id)
                    except Exception:
                        continue
        await self.broadcast_state_update(force=True)
        return {"ok": True, "cleared": cleared, "accounts": len(ids), "resumed": resumed}

    async def refresh_profile_now(self, account_id: str) -> Dict[str, Any]:
        """Admin-triggered network profile check; does not invent a match reward."""
        acc_data = self.active_account_data.get(account_id)
        if not self.is_account_running(account_id) or not acc_data:
            raise ValueError("Start the account worker and wait for login before checking EXP.")
        now = time.monotonic()
        if now - self._manual_profile_checked_at.get(account_id, 0) < 12:
            raise ValueError("Please wait 12 seconds between manual profile checks.")
        self._manual_profile_checked_at[account_id] = now
        async with acc_data["_profile_refresh_lock"]:
            result = await lv.refresh_account_profile(acc_data)
            lv.report_profile_result(acc_data, result)
        await self.broadcast_state_update(force=True)
        return {"ok": bool(result.get("ok")), "check": result,
                "account": db.get_account_by_id(account_id)}

    def get_system_status(self) -> Dict[str, Any]:
        uptime_sec = int(time.time() - self.start_time)
        cpu_pct = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        proc = psutil.Process()
        proc_mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)

        active_workers = sum(1 for t in self.workers.values() if not t.done())
        return {
            "uptime_seconds": uptime_sec,
            "cpu_percent": round(cpu_pct, 1),
            "memory_percent": round(vm.percent, 1),
            "process_memory_mb": proc_mem_mb,
            "active_workers": active_workers,
            "max_active_workers": MAX_ACTIVE_WORKERS,
            "max_concurrent_logins": MAX_CONCURRENT_LOGINS,
            "websocket_clients": len(self.ws_clients),
            "dns_cache_entries": len(lv._DNS_CACHE),
            "start_match_interval_sec": lv.START_MATCH_INTERVAL,
            "max_match_duration_sec": lv.MAX_MATCH_DURATION,
            "udp_join_timeout_sec": lv.UDP_JOIN_TIMEOUT_SECONDS,
            "udp_idle_timeout_sec": lv.UDP_IDLE_RECONNECT_SECONDS,
            "supported_regions": sorted(list(lv.REGION_PREFIX.keys())),
            "diagnostics": self.diagnostics,
        }

    def get_dashboard_overview(self) -> Dict[str, Any]:
        accounts = db.list_accounts()
        acc_by_id = {a["id"]: a for a in accounts}
        updated_any = False
        for acc_id, live_data in list(self.active_account_data.items()):
            try:
                db_acc = acc_by_id.get(acc_id)
                if not db_acc:
                    continue
                live_lvl = int(live_data.get("level") or 0)
                live_exp = int(live_data.get("exp") or 0)
                live_nick = str(live_data.get("nickname") or "")
                if live_lvl > 0 and (
                    live_lvl != int(db_acc.get("level") or 0)
                    or live_exp != int(db_acc.get("exp") or 0)
                ):
                    db.update_account_runtime_state(
                        acc_id,
                        level=live_lvl,
                        exp=live_exp,
                        nickname=live_nick if live_nick else None,
                    )
                    updated_any = True
            except Exception:
                pass
        if updated_any:
            accounts = db.list_accounts()
        total = len(accounts)
        active_statuses = {"authenticating", "online", "searching_match", "in_match", "reconnecting"}
        active = sum(1 for a in accounts if a["status"] in active_statuses)
        error_cnt = sum(1 for a in accounts if a["status"] == "error")
        inactive = total - active

        status_breakdown = {
            "in_match": sum(1 for a in accounts if a["status"] == "in_match"),
            "searching_match": sum(1 for a in accounts if a["status"] == "searching_match"),
            "online": sum(1 for a in accounts if a["status"] == "online"),
            "authenticating": sum(1 for a in accounts if a["status"] in ("authenticating", "reconnecting")),
            "inactive": sum(1 for a in accounts if a["status"] == "inactive"),
            "cooldown": sum(1 for a in accounts if a["status"] == "cooldown"),
            "error": error_cnt,
        }

        region_breakdown: Dict[str, int] = {}
        for a in accounts:
            r = a.get("region", "ME")
            region_breakdown[r] = region_breakdown.get(r, 0) + 1

        # Legacy matches_completed could count duration expiry; only explicit
        # server end packets recorded from V5 are shown as confirmed signals.
        total_matches_completed = sum(int(a.get("server_finish_count") or 0) for a in accounts)
        total_matches_searched = sum(int(a.get("matches_searched") or 0) for a in accounts)
        total_exp = sum(int(a.get("exp") or 0) for a in accounts)
        verified = [a for a in accounts if a.get("exp_chart_status") != "awaiting_profile"]
        avg_level = round(sum(int(a.get("level") or 1) for a in verified) / len(verified), 1) if verified else 0.0
        total_errors = sum(int(a.get("error_count") or 0) for a in accounts)
        daily_totals = db.daily_exp_totals()
        daily_totals["accounts_at_target"] = sum(1 for a in accounts if a.get("daily_exp_reached"))
        daily_totals["accounts_auto_stopped"] = sum(1 for a in accounts if a.get("daily_auto_stopped"))

        return {
            "summary": {
                "safety": self.safety_payload(),
                "daily_rules": daily_rule_payload(),
                "daily_totals": daily_totals,
                "total_accounts": total,
                "active_accounts": active,
                "inactive_accounts": inactive,
                "cooldown_accounts": status_breakdown["cooldown"],
                "queue_no_join_limit": QUEUE_NO_JOIN_LIMIT,
                "queue_cooldown_seconds": QUEUE_COOLDOWN_SECONDS,
                "error_accounts": error_cnt,
                "total_matches_completed": total_matches_completed,
                "total_matches_searched": total_matches_searched,
                "total_exp": total_exp,
                "average_level": avg_level,
                "total_errors": total_errors,
            },
            "status_breakdown": status_breakdown,
            "region_breakdown": region_breakdown,
            "system": self.get_system_status(),
            "recent_activity": db.get_recent_activity(limit=30),
            "accounts": accounts,
        }

    async def broadcast_state_update(self, force: bool = False) -> None:
        if not self.ws_clients:
            return
        now = time.time()
        last_b = getattr(self, "_last_broadcast_ts", 0.0)
        if not force and (now - last_b) < 2.0:
            return
        self._last_broadcast_ts = now
        async with self._broadcast_lock:
            payload = {
                "type": "dashboard_sync",
                "timestamp": db.utc_now_iso(),
                "data": self.get_dashboard_overview(),
            }
            stale: List[WebSocket] = []
            for ws in list(self.ws_clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.ws_clients.discard(ws)

    async def resume_active_accounts_on_startup(self) -> None:
        """Auto-resume accounts that were left in an active state prior to server restart."""
        accounts = db.list_accounts()
        active_statuses = {"authenticating", "online", "searching_match", "in_match", "reconnecting"}
        for acc in accounts:
            if acc.get('status') == 'cooldown':
                if acc.get('cooldown_until'):
                    self.queue_cooldown_tasks[acc['id']] = asyncio.create_task(
                        self._queue_cooldown_lifecycle(acc['id'], acc['cooldown_until']))
                else:
                    db.update_account_runtime_state(acc['id'], status='inactive',
                                                    status_message='Cooldown deadline missing; start manually')
                continue
            if acc.get("status") in active_statuses and not self.is_account_running(acc["id"]):
                daily = self.daily_status_for_account(acc["id"], acc.get("level"))
                if daily["reached"]:
                    db.update_account_runtime_state(
                        acc["id"], status="inactive",
                        status_message=(f"Not resumed — daily EXP target already reached "
                                        f"({daily['exp_gained']:,} / {daily['limit']:,}). Resumes after the next reset."),
                    )
                    db.add_activity_log(account_id=acc["id"], account_label="System", level="info",
                        event_type="daily_auto_offline",
                        message="Startup skipped this worker: today's EXP target was already reached.")
                    continue
                try:
                    await self.start_account(acc["id"])
                    await asyncio.sleep(0.3)
                except Exception:
                    db.update_account_runtime_state(
                        acc["id"],
                        status="inactive",
                        status_message="Stopped (click Start to run worker)",
                    )

    async def shutdown_all(self) -> None:
        for task in list(self.queue_cooldown_tasks.values()):
            task.cancel()
        if self.queue_cooldown_tasks:
            await asyncio.gather(*self.queue_cooldown_tasks.values(), return_exceptions=True)
        self.queue_cooldown_tasks.clear()
        for acc_id in list(self.workers.keys()):
            try:
                await self.stop_account(acc_id, reason="Server shutting down")
            except Exception:
                pass


def headers_release_ver() -> str:
    return str(lv.headers.get("ReleaseVersion", "OB55"))


bot_manager = BotManager()
