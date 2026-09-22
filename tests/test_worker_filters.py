from types import SimpleNamespace

from app.models import Forecast
from app import worker


class Snapshot(SimpleNamespace):
    def to_dict(self):
        return {"betting_epoch": self.betting_epoch, "live_epoch": self.live_epoch, "seconds_to_lock": self.seconds_to_lock}


def make_forecast(ev=0.03, p=0.55):
    return Forecast(
        signal="UP", probability_up=p, probability_down=1-p, raw_probability_up=p,
        raw_expected_coeff_up=2.0, raw_expected_coeff_down=2.0,
        payout_correction_up=1.0, payout_correction_down=1.0,
        payout_bucket_up="2.00-2.50", payout_bucket_down="2.00-2.50",
        payout_bucket_ready_up=True, payout_bucket_ready_down=True,
        expected_coeff_up=2.0, expected_coeff_down=2.0,
        ev_up=ev, ev_down=-0.1, selected_ev=ev, agreement=0.7,
        source_key="EV_PRIMARY", selection_reason="POSITIVE_EV_BEST_SIDE",
        components=[], weights={}, features={},
    )


def prep(monkeypatch, ev=0.03, p=0.55, shadow_allowed=True):
    snap = Snapshot(betting_epoch=123, live_epoch=122, chain_timestamp=1000, seconds_to_lock=40)
    monkeypatch.setattr(worker.db, "get_decision", lambda epoch: None)
    monkeypatch.setattr(worker, "forecast", lambda current: make_forecast(ev, p))
    monkeypatch.setattr(worker, "evaluate_shadow", lambda *args: (shadow_allowed, "SHADOW_QUALITY_PF_CONFIRMED" if shadow_allowed else "QUALITY_PROFIT_FACTOR_BELOW_MINIMUM", {}))
    monkeypatch.setattr(worker.db, "get_state", lambda: {"bank": 2137.12})
    monkeypatch.setattr(worker.db, "insert_decision", lambda data: data)
    return snap


def test_good_signal_trades_fixed_22(monkeypatch):
    d = worker.create_locked_decision(prep(monkeypatch))
    assert d["execution_requested"] is True
    assert d["trade_executed"] is False
    assert d["execution_status"] == "PENDING"
    assert d["stake"] == 22.0
    assert d["stake_mode"] == "fixed_22"


def test_ev_below_two_percent_is_blocked(monkeypatch):
    d = worker.create_locked_decision(prep(monkeypatch, ev=0.019))
    assert d["trade_executed"] is False
    assert d["no_trade_reason"] == "EV_BELOW_2_PERCENT"


def test_probability_below_53_is_blocked(monkeypatch):
    d = worker.create_locked_decision(prep(monkeypatch, ev=0.03, p=0.529))
    assert d["trade_executed"] is False
    assert d["no_trade_reason"] == "PROBABILITY_BELOW_53_PERCENT"


def test_real_decision_mirrors_exact_paper_row(monkeypatch):
    snap = Snapshot(
        betting_epoch=456,
        live_epoch=455,
        chain_timestamp=2000,
        seconds_to_lock=37,
        chainlink_price=612.5,
    )
    reference = {
        "betting_epoch": 456,
        "live_epoch": 455,
        "locked_at_chain_timestamp": 1998,
        "locked_at_seconds_to_lock": 39,
        "signal": "DOWN",
        "probability_up": 0.44,
        "probability_down": 0.56,
        "expected_coeff_up": 1.8,
        "expected_coeff_down": 2.1,
        "ev_up": -0.208,
        "ev_down": 0.176,
        "selected_ev": 0.176,
        "agreement": 0.8,
        "decision_quality": "FUSION_222_TRADE",
        "stake": 22.0,
        "trade_executed": True,
        "features_json": {"paper_feature": 1},
        "components_json": [],
        "weights_json": {},
        "snapshot_json": {"paper_snapshot": True},
        "shadow_stats_json": {},
        "strategy_version": "FUSION-222-v1.0.5",
        "stake_mode": "fixed_22",
        "stake_tier": "FIXED_22",
        "source_key": "EV_PRIMARY",
    }
    monkeypatch.setattr(worker.db, "get_decision", lambda epoch: None)
    monkeypatch.setattr(worker.db, "get_reference_decision", lambda epoch: reference)
    monkeypatch.setattr(worker.db, "insert_decision", lambda data: data)

    d = worker.create_mirrored_decision(snap)

    assert d is not None
    assert d["signal"] == "DOWN"
    assert d["stake"] == 22.0
    assert d["execution_requested"] is True
    assert d["trade_executed"] is False
    assert d["execution_status"] == "PENDING"
    assert d["origin"] == "LIVE_REAL_MIRROR"
    assert d["features_json"]["mirrored_from_paper"] is True
    assert d["strategy_version"] == "FUSION-222-v1.0.5"


def test_real_mirror_waits_when_paper_row_is_not_ready(monkeypatch):
    snap = Snapshot(
        betting_epoch=789,
        live_epoch=788,
        chain_timestamp=3000,
        seconds_to_lock=40,
        chainlink_price=600.0,
    )
    monkeypatch.setattr(worker.db, "get_decision", lambda epoch: None)
    monkeypatch.setattr(worker.db, "get_reference_decision", lambda epoch: None)

    assert worker.create_mirrored_decision(snap) is None
