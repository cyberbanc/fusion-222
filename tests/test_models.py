from app.models import RoundData


def test_round_winner_and_coeff():
    r = RoundData(
        epoch=1,start_timestamp=0,lock_timestamp=0,close_timestamp=0,
        lock_price=500.0,close_price=501.0,lock_oracle_id=1,close_oracle_id=2,
        total_amount_bnb=10.0,bull_amount_bnb=4.0,bear_amount_bnb=6.0,
        reward_base_bnb=10.0,reward_amount_bnb=9.7,oracle_called=True,
    )
    assert r.actual_winner == "UP"
    assert r.coefficient_gross("UP") == 2.5
