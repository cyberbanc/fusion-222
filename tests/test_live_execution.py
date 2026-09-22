from types import SimpleNamespace

from app import live_execution


class FakeClient:
    pass


def test_missing_wallet_blocks_without_broadcast(monkeypatch):
    executor = live_execution.LiveExecutor(FakeClient())
    executor._account = None
    executor._configuration_error = "WALLET_PRIVATE_KEY is not configured"
    monkeypatch.setattr(executor, "reconcile_bet", lambda decision, price: decision)
    seen = {}

    def update(epoch, **fields):
        seen.update(fields)
        return {"betting_epoch": epoch, **fields}

    monkeypatch.setattr(live_execution.db, "update_execution", update)
    result = executor.execute_bet(
        {"betting_epoch": 123, "execution_requested": True, "signal": "UP", "stake": 22},
        600.0,
    )
    assert result["execution_status"] == "BLOCKED"
    assert result["trade_executed"] is False
    assert "WALLET_PRIVATE_KEY" in result["execution_error"]


def test_existing_contract_ledger_prevents_duplicate_send(monkeypatch):
    executor = live_execution.LiveExecutor(FakeClient())
    executor._account = SimpleNamespace(address="0x0000000000000000000000000000000000000001")
    executor._configuration_error = None
    monkeypatch.setattr(
        executor,
        "ledger",
        lambda epoch: {"amount_wei": 10**16, "amount_bnb": 0.01, "position": 0, "claimed": False},
    )

    def update(epoch, **fields):
        return {"betting_epoch": epoch, **fields}

    monkeypatch.setattr(live_execution.db, "update_execution", update)
    monkeypatch.setattr(executor, "_build_sign", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not sign")))
    result = executor.execute_bet(
        {"betting_epoch": 123, "execution_requested": True, "signal": "UP", "stake": 22},
        600.0,
    )
    assert result["trade_executed"] is True
    assert result["execution_status"] == "RECONCILED"
    assert result["reconciliation_status"] == "CONTRACT_LEDGER_MATCH"
