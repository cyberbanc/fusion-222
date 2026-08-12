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
    assert d["trade_executed"] is True
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

