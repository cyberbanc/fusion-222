from types import SimpleNamespace

from app import shadow
from app.shadow import summarize


def test_shadow_metrics_newest_first():
    rows = [{"shadow_pnl": -10.0}, {"shadow_pnl": -10.0}, {"shadow_pnl": 9.0}, {"shadow_pnl": 8.0}]
    metrics = summarize(rows)
    assert metrics.samples == 4
    assert metrics.current_loss_streak == 2
    assert metrics.pnl == -3.0


def test_quality_win_rate_is_display_only_but_pf_still_blocks(monkeypatch):
    # 8 recent: positive enough. 10 quality: WR=40% (<45) but PF>0.85.
    recent = [{"shadow_pnl": 30.0}] * 4 + [{"shadow_pnl": -10.0}] * 4
    quality = [{"shadow_pnl": 30.0}] * 4 + [{"shadow_pnl": -10.0}] * 6
    monkeypatch.setattr("app.db.shadow_rows", lambda source, signal, lookback: recent if lookback == 8 else quality)
    allowed, reason, stats = shadow.evaluate("X", "UP", 0.03)
    assert stats["quality"]["win_rate"] == 0.4
    assert allowed is True
    assert reason == "SHADOW_QUALITY_PF_CONFIRMED"


def test_shadow_blocks_low_pf(monkeypatch):
    recent = [{"shadow_pnl": 8.0}] * 5 + [{"shadow_pnl": -10.0}] * 3
    quality = [{"shadow_pnl": 8.0}] * 4 + [{"shadow_pnl": -10.0}] * 6
    monkeypatch.setattr("app.db.shadow_rows", lambda source, signal, lookback: recent if lookback == 8 else quality)
    allowed, reason, _ = shadow.evaluate("X", "DOWN", 0.03)
    assert allowed is False
    assert reason == "QUALITY_PROFIT_FACTOR_BELOW_MINIMUM"


def test_shadow_blocks_ev_below_two_percent(monkeypatch):
    monkeypatch.setattr("app.db.shadow_rows", lambda *args, **kwargs: [])
    allowed, reason, _ = shadow.evaluate("X", "UP", 0.0199)
    assert allowed is False
    assert reason == "EV_BELOW_2_PERCENT"
