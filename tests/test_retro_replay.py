from app import db


def row(epoch, version, winner, signal="UP", ev=0.03, p=0.55, coeff=2.0, executed=True):
    return {
        "betting_epoch": epoch,
        "strategy_version": version,
        "settled": True,
        "final_winner": winner,
        "signal": signal,
        "selected_ev": ev,
        "probability_up": p,
        "probability_down": 1-p,
        "trade_executed": executed,
        "final_coeff_up": coeff,
        "final_coeff_down": coeff,
        "features_json": {"selected_payout_bucket_ready": True},
        "shadow_stats_json": {
            "recent": {"samples": 8, "pnl": 0},
            "quality": {"samples": 30, "profit_factor": 1.2, "win_rate": 0.4},
            "settings": {"recent_window": 8, "recent_min_pnl": -30, "quality_min_samples": 10, "quality_min_profit_factor": 0.85},
        },
        "created_at": None,
    }


def test_retro_replay_fixed_22_without_breaker(monkeypatch):
    rows = [
        row(1,"1.3.6.6","DOWN"), row(2,"1.3.6.6","DOWN"), row(3,"1.3.6.6","DOWN"),
        row(4,"1.3.6.6","UP"), row(5,"1.3.6.6","UP"), row(6,"1.3.6.6","UP"),
        row(7,"1.3.6.6","UP"),
    ]
    monkeypatch.setattr(db, "_base_rows", lambda cutoff, strategy_version=None: rows)
    selected, metrics = db._retro_replay(7)
    # No circuit breaker: every otherwise-eligible signal is traded.
    assert [r["betting_epoch"] for r in selected] == [1,2,3,4,5,6,7]
    assert selected[0]["stake"] == 22.0
    assert metrics["trades_count"] == 7
    assert metrics["wins"] == 4
    assert metrics["losses"] == 3
    assert metrics["breaker_signals_remaining"] == 0


def test_retro_replay_rejects_other_versions(monkeypatch):
    rows = [
        row(1, "1.3.6.4", "UP"),
        row(2, "1.3.6.6", "UP"),
    ]
    monkeypatch.setattr(db, "_base_rows", lambda cutoff, strategy_version=None: rows)
    selected, metrics = db._retro_replay(2)
    assert [r["betting_epoch"] for r in selected] == [2]
    assert metrics["retro_scope_version"] == "1.3.6.6"
