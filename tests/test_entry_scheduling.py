import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from app import main, pancake_client, worker


def setup_tick(monkeypatch, seconds=30, mode="REAL", decision=None):
    events = []
    clock = [0.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "SETTINGS", replace(worker.SETTINGS, execution_mode=mode))
    snap = NS(current_epoch=123, betting_epoch=123, live_epoch=122,
              chain_timestamp=1000, seconds_to_lock=seconds,
              decision_window=8 <= seconds <= 40, chainlink_price=500,
              betting_round=NS(bull_amount_bnb=3, bear_amount_bnb=7),
              to_dict=lambda: {"betting_epoch": 123})
    client = NS(market_snapshot=lambda: snap, rpc_status=lambda: {})
    monkeypatch.setattr(worker, "from_env", lambda: client)
    monkeypatch.setattr(worker.db, "get_decision", lambda epoch: decision)
    monkeypatch.setattr(worker.db, "get_state", lambda: {})
    monkeypatch.setattr(worker.db, "save_snapshot", lambda data: events.append("save") or True)
    monkeypatch.setattr(worker, "create_mirrored_decision", lambda snap: events.append("mirror"))
    monkeypatch.setattr(worker, "create_locked_decision", lambda snap: events.append("forecast"))
    monkeypatch.setattr(worker, "settle_finished", lambda *a, **k: events.append("settle") or 0)
    monkeypatch.setattr(worker, "sync_recent_closed", lambda *a, **k: events.append("sync"))
    monkeypatch.setattr(worker, "reconcile_actual_results", lambda *a, **k: events.append("claim") or 0)
    executor = NS(execute_bet=lambda d, p, **k: events.append("execute") or d,
                  wallet_snapshot=lambda *a: events.append("wallet") or {}, status=lambda: {})
    monkeypatch.setattr(worker, "live_executor_from_env", lambda *a: executor)
    return events, clock


@pytest.mark.parametrize("seconds", [8, 30, 40, 70, 100, 150])
def test_real_waits_for_paper_without_starting_maintenance(monkeypatch, seconds):
    events, _ = setup_tick(monkeypatch, seconds=seconds)
    result = worker.tick()
    assert result["maintenance_deferred_for_entry"]
    assert not {"settle", "sync", "claim", "wallet", "forecast"} & set(events)
    assert ("mirror" in events) == (8 <= seconds <= 40)


def test_real_entry_before_any_background_work(monkeypatch):
    events, _ = setup_tick(monkeypatch, decision={"betting_epoch": 123, "execution_requested": True})
    worker.tick()
    assert events == ["execute"]


def test_existing_hash_reconciled_before_background_outside_window(monkeypatch):
    events, _ = setup_tick(monkeypatch, seconds=250,
                           decision={"betting_epoch": 123, "execution_requested": True, "tx_hash": "0xknown"})
    worker.tick()
    assert events == ["execute", "settle", "sync", "wallet", "claim"]


def test_batch_budget_stops_following_background_operations(monkeypatch):
    events, clock = setup_tick(monkeypatch, seconds=250)
    def slow_settlement(*args, **kwargs):
        events.append("settle")
        clock[0] += 6
        assert not kwargs["can_continue"]()
        return 1
    monkeypatch.setattr(worker, "settle_finished", slow_settlement)
    worker.tick()
    assert events == ["settle"]


def test_paper_maintenance_order_unchanged(monkeypatch):
    events, _ = setup_tick(monkeypatch, mode="PAPER")
    worker.tick()
    assert events == ["settle", "sync", "forecast", "claim"]


def test_old_settlements_yield_between_rounds(monkeypatch):
    monkeypatch.setattr(worker.db, "unsettled_decisions", lambda **k: [{"betting_epoch": 100}, {"betting_epoch": 101}])
    calls = []
    client = NS(round=lambda epoch: calls.append(epoch) or NS(oracle_called=False))
    allowed = iter([True, False])
    worker.settle_finished(client, 123, can_continue=lambda: next(allowed))
    assert calls == [100]


def test_cache_sync_yields_and_retries_newest_first(monkeypatch):
    monkeypatch.setattr(worker, "_LAST_SYNC_AT", 0)
    calls = []
    client = NS(round=lambda epoch: calls.append(epoch) or NS(oracle_called=False))
    allowed = iter([True, False])
    worker.sync_recent_closed(client, 123, can_continue=lambda: next(allowed))
    assert calls == [121]
    assert worker._LAST_SYNC_AT == 0


def test_claim_backlog_yields_between_decisions(monkeypatch):
    monkeypatch.setattr(worker, "SETTINGS", replace(worker.SETTINGS, execution_mode="REAL"))
    monkeypatch.setattr(worker.db, "actual_reconciliation_candidates", lambda **k: [{"betting_epoch": 100}, {"betting_epoch": 101}])
    calls = []
    executor = NS(finalize_actual=lambda d, p: calls.append(d["betting_epoch"]) or d)
    monkeypatch.setattr(worker, "live_executor_from_env", lambda *a: executor)
    allowed = iter([True, False])
    worker.reconcile_actual_results(NS(), 500, can_continue=lambda: next(allowed))
    assert calls == [100]


def test_real_startup_starts_without_history_bootstrap(monkeypatch):
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, execution_mode="REAL", worker_enabled=False))
    monkeypatch.setattr(main.db, "init_db", lambda: None)
    monkeypatch.setattr(main, "bootstrap_rounds", lambda *a: pytest.fail("real startup must not bootstrap"))
    monkeypatch.setattr(main, "_STOP", None)
    monkeypatch.setattr(main, "_TASK", None)
    async def startup():
        async with main.lifespan(main.app):
            pass
    asyncio.run(startup())


def test_observer_has_its_own_provider_lock(monkeypatch):
    monkeypatch.setattr(pancake_client, "_CLIENT", None)
    monkeypatch.setattr(pancake_client, "_OBSERVER_CLIENT", None)
    worker_client = pancake_client.from_env()
    observer = pancake_client.observer_from_env()
    assert observer is not worker_client
    assert observer._lock is not worker_client._lock
    assert pancake_client.from_env() is worker_client
    assert pancake_client.observer_from_env() is observer


def test_preflight_wallet_reads_use_observer_client(monkeypatch):
    monkeypatch.setattr(main, "_PREFLIGHT_CACHE", {})
    snap = NS(betting_epoch=123, chain_timestamp=1000, oracle_updated_at=999,
              chainlink_price=500, seconds_to_lock=30, decision_window=True,
              betting_round=NS(lock_timestamp=1030))
    observer = NS(market_snapshot=lambda: snap, _call=lambda fn: (False, 1))
    monkeypatch.setattr(main, "observer_from_env", lambda: observer)
    monkeypatch.setattr(main, "from_env", lambda: pytest.fail("preflight touched execution RPC"))
    reads = []
    executor = NS(wallet_snapshot=lambda *a, **k: reads.append(k["client"]) or {"balance_bnb": 3}, status=lambda: {})
    monkeypatch.setattr(main, "live_executor_from_env", lambda: executor)
    monkeypatch.setattr(main.db, "get_reference_decision", lambda epoch: None)
    assert main.preflight()["ok"]
    assert reads == [observer]
