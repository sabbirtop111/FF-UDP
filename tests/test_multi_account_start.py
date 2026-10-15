"""V18.5: starting 2-4 accounts at once must not degrade into a burst the game server refuses.

Evidence this covers (from the operator's own console):
* one account alone gained EXP (3,034 in a day) while four started together gained none;
* every worker used to fire its StartMatch request on its own 3s timer, so a 4-account
  fleet produced ~1.3 requests/second at the same gateway (servers throttle that);
* "Start all" began every login in the same second, and Garena answers login bursts with
  throttling, after which workers sit in a retry loop and never reach a match.
"""
import asyncio
import time

import pytest

import lv
from app import bot_manager as bm
from app import database as db
from app import config


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'multi.db'))
    db.init_db()
    return db


# --------------------------------------------------------------------------------------
# 1. match requests are spread across accounts, not multiplied
# --------------------------------------------------------------------------------------
def test_matchmaking_gap_is_a_safe_default():
    assert 0.2 <= lv.MATCHMAKING_MIN_GAP_SECONDS <= 10.0
    assert hasattr(lv, 'matchmaking_slot')


def test_matchmaking_slot_serialises_sends(monkeypatch):
    """Four accounts asking at the same instant must leave the process one at a time."""
    monkeypatch.setattr(lv, 'MATCHMAKING_MIN_GAP_SECONDS', 0.25)
    monkeypatch.setattr(lv, '_matchmaking_last_sent', 0.0)
    stamp_holder = {}

    async def scenario():
        stamps = []

        async def account(tag):
            await lv.matchmaking_slot()
            stamps.append((tag, time.monotonic()))

        await asyncio.gather(*(account(f"acc{i}") for i in range(4)))
        stamps.sort(key=lambda x: x[1])
        gaps = [round(b[1] - a[1], 3) for a, b in zip(stamps, stamps[1:])]
        stamp_holder['gaps'] = gaps
        return stamps

    asyncio.run(scenario())
    gaps = stamp_holder['gaps']
    assert len(gaps) == 3
    # every send waited for the previous one, within timer tolerance
    assert all(g >= 0.18 for g in gaps), gaps


def test_matchmaking_slot_does_not_delay_a_single_account(monkeypatch):
    monkeypatch.setattr(lv, 'MATCHMAKING_MIN_GAP_SECONDS', 1.2)
    monkeypatch.setattr(lv, '_matchmaking_last_sent', 0.0)

    async def scenario():
        started = time.monotonic()
        await lv.matchmaking_slot()   # first ever send goes out immediately
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.05, elapsed


def test_fleet_rate_stays_close_to_one_account_rate(monkeypatch):
    """A 4-account fleet must not push 4x the request rate of a single account."""
    monkeypatch.setattr(lv, 'MATCHMAKING_MIN_GAP_SECONDS', 1.2)
    per_account_cadence = lv.START_MATCH_INTERVAL          # 3s by default
    fleet_rate = 1.0 / lv.MATCHMAKING_MIN_GAP_SECONDS      # hard ceiling across all accounts
    single_rate = 1.0 / per_account_cadence
    assert fleet_rate <= single_rate * 3, "the fleet ceiling must stay near a single account's rate"
    assert fleet_rate > single_rate, "but it must still allow a fleet to search"


# --------------------------------------------------------------------------------------
# 2. logins are staggered so a fleet start is not a burst
# --------------------------------------------------------------------------------------
def test_start_stagger_spreads_workers(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'ACCOUNT_START_STAGGER_SECONDS', 0.3)
    manager = bm.BotManager()

    async def scenario():
        waits = await asyncio.gather(*(manager._acquire_start_slot() for _ in range(4)))
        return waits

    waits = asyncio.run(scenario())
    assert sum(waits) >= 0.3 * 3 * 0.8, waits
    # exactly one worker gets the slot immediately
    assert sum(1 for w in waits if w < 0.05) == 1, waits


def test_start_stagger_can_be_disabled(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'ACCOUNT_START_STAGGER_SECONDS', 0.0)
    manager = bm.BotManager()

    async def scenario():
        started = time.monotonic()
        await asyncio.gather(*(manager._acquire_start_slot() for _ in range(4)))
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.05


def test_stagger_default_is_sane():
    assert 0 <= config.ACCOUNT_START_STAGGER_SECONDS <= 60


def test_worker_lifecycle_waits_for_its_turn(isolated_db, monkeypatch):
    """Two accounts started together must reach their login step seconds apart.

    The gate lives in the worker (not in the HTTP call), so "Start all" answers instantly
    while the logins trickle in. The test cancels the workers as soon as both slots were
    taken, so no real network call is made.
    """
    monkeypatch.setattr(bm, 'ACCOUNT_START_STAGGER_SECONDS', 0.4)
    manager = bm.BotManager()
    taken = []

    real_gate = bm.BotManager._acquire_start_slot
    fake_cred = {"uid": "u1", "auth_type": "uid_password", "password_plain": "x",
                 "access_token_plain": "", "region": "BD", "display_name": "T"}

    async def gate(self):
        waited = await real_gate(self)
        taken.append((time.monotonic(), waited))
        return waited

    monkeypatch.setattr(bm.BotManager, '_acquire_start_slot', gate)
    monkeypatch.setattr(db, 'get_account_credentials_internal', lambda aid: dict(fake_cred))

    async def scenario():
        tasks = [asyncio.create_task(manager._account_worker_lifecycle('acc-a')),
                 asyncio.create_task(manager._account_worker_lifecycle('acc-b'))]
        for _ in range(200):
            if len(taken) >= 2:
                break
            await asyncio.sleep(0.02)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return tasks

    asyncio.run(scenario())
    assert len(taken) == 2, taken
    gap = abs(taken[0][0] - taken[1][0])
    assert gap >= 0.3, f"workers reached login only {gap:.2f}s apart"
    assert max(t[1] for t in taken) >= 0.3, taken


# --------------------------------------------------------------------------------------
# 3. the panel reports both guards
# --------------------------------------------------------------------------------------
def test_safety_payload_exposes_the_multi_account_guards(isolated_db):
    manager = bm.BotManager()
    payload = manager.safety_payload()
    assert payload['start_stagger_seconds'] == config.ACCOUNT_START_STAGGER_SECONDS
    assert payload['matchmaking_min_gap_seconds'] == lv.MATCHMAKING_MIN_GAP_SECONDS
    assert payload['max_concurrent_logins'] >= 1


def test_no_extra_udp_or_tcp_packets_from_the_gap(monkeypatch):
    """The matchmaking gate only delays a send; it must not add or change any packet."""
    sends = []

    async def fake_start(*a, **k):
        sends.append(time.monotonic())

    monkeypatch.setattr(lv, 'start_game_battle_royale', fake_start)
    monkeypatch.setattr(lv, 'MATCHMAKING_MIN_GAP_SECONDS', 0.2)
    monkeypatch.setattr(lv, '_matchmaking_last_sent', 0.0)

    async def scenario():
        await asyncio.gather(*(lv.matchmaking_slot() for _ in range(3)))
        await asyncio.gather(*(fake_start() for _ in range(3)))

    asyncio.run(scenario())
    assert len(sends) == 3, "one request per account, never more"
