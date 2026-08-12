from unittest.mock import patch
from app.ensemble import forecast
from app.models import MarketSnapshot, RoundData


def make_round(epoch: int, winner: str | None = None):
    lock=500.0; close=None if winner is None else (501.0 if winner=='UP' else 499.0)
    return RoundData(epoch=epoch,start_timestamp=0,lock_timestamp=100,close_timestamp=400,lock_price=lock,close_price=close,lock_oracle_id=1,close_oracle_id=2,total_amount_bnb=1.0,bull_amount_bnb=0.5,bear_amount_bnb=0.5,reward_base_bnb=1.0,reward_amount_bnb=0.97,oracle_called=winner is not None)


def test_forecast_returns_valid_signal():
    history=[{'epoch':i,'actual_winner':'UP' if i%2==0 else 'DOWN','winner_coeff_net':1.9} for i in range(40)]
    snapshot=MarketSnapshot(current_epoch=100,betting_epoch=100,live_epoch=99,chain_timestamp=60,seconds_to_lock=40,decision_window=True,safe_to_decide=True,chainlink_price=500.5,oracle_round_id=1,oracle_updated_at=59,oracle_age_seconds=1,live_round=make_round(99),betting_round=make_round(100),live_move_signed=0.5,live_move_points=0.5,current_direction='UP',current_gross_coeff_up=2.0,current_gross_coeff_down=2.0,current_net_coeff_up=1.94,current_net_coeff_down=1.94,betting_bull_share_pct=50.0,betting_bear_share_pct=50.0,rpc_status={})
    with patch('app.ensemble.db.snapshots_for_epoch',return_value=[]), patch('app.ensemble.db.recent_rounds',return_value=history), patch('app.ensemble.db.payout_ratios',return_value=[0.9]*20), patch('app.ensemble.binance_from_env') as b:
        b.return_value.snapshot.return_value={'available':True,'probability_up':0.52,'price':500.5}
        result=forecast(snapshot)
    assert result.signal in {'UP','DOWN'}
