from app.config import SETTINGS
from app.risk import fixed_stake_decision


def test_fixed_22_stake_and_defaults():
    d = fixed_stake_decision()
    assert d.eligible is True
    assert d.stake == 22.0
    assert d.tier == "FIXED_22"
    assert SETTINGS.min_trade_ev == 0.02
    assert SETTINGS.min_signal_probability == 0.53
    assert SETTINGS.quality_win_rate_filter_enabled is False
    assert SETTINGS.quality_min_profit_factor == 0.85
    assert SETTINGS.breaker_loss_trigger == 3
    assert SETTINGS.breaker_skip_signals == 3
