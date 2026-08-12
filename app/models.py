from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RoundData:
    epoch: int
    start_timestamp: int
    lock_timestamp: int
    close_timestamp: int
    lock_price: float | None
    close_price: float | None
    lock_oracle_id: int
    close_oracle_id: int
    total_amount_bnb: float
    bull_amount_bnb: float
    bear_amount_bnb: float
    reward_base_bnb: float
    reward_amount_bnb: float
    oracle_called: bool

    @property
    def actual_winner(self) -> str | None:
        if self.lock_price is None or self.close_price is None or not self.oracle_called:
            return None
        if self.close_price > self.lock_price:
            return "UP"
        if self.close_price < self.lock_price:
            return "DOWN"
        return "DRAW"

    def coefficient_gross(self, side: str) -> float | None:
        amount = self.bull_amount_bnb if side == "UP" else self.bear_amount_bnb
        if amount <= 0 or self.total_amount_bnb <= 0:
            return None
        return self.total_amount_bnb / amount

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actual_winner"] = self.actual_winner
        data["winner_coeff_gross"] = (
            self.coefficient_gross(self.actual_winner)
            if self.actual_winner in {"UP", "DOWN"}
            else None
        )
        return data


@dataclass
class MarketSnapshot:
    current_epoch: int
    betting_epoch: int
    live_epoch: int
    chain_timestamp: int
    seconds_to_lock: int
    decision_window: bool
    safe_to_decide: bool
    chainlink_price: float
    oracle_round_id: int
    oracle_updated_at: int
    oracle_age_seconds: int
    live_round: RoundData
    betting_round: RoundData
    live_move_signed: float
    live_move_points: float
    current_direction: str
    current_gross_coeff_up: float | None
    current_gross_coeff_down: float | None
    current_net_coeff_up: float | None
    current_net_coeff_down: float | None
    betting_bull_share_pct: float | None
    betting_bear_share_pct: float | None
    rpc_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "live_round": self.live_round.to_dict(),
            "betting_round": self.betting_round.to_dict(),
        }


@dataclass
class Component:
    name: str
    probability_up: float
    reliability: float
    available: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Forecast:
    signal: str
    probability_up: float
    probability_down: float
    raw_probability_up: float
    raw_expected_coeff_up: float
    raw_expected_coeff_down: float
    payout_correction_up: float
    payout_correction_down: float
    payout_bucket_up: str
    payout_bucket_down: str
    payout_bucket_ready_up: bool
    payout_bucket_ready_down: bool
    expected_coeff_up: float
    expected_coeff_down: float
    ev_up: float
    ev_down: float
    selected_ev: float
    agreement: float
    source_key: str
    selection_reason: str
    components: list[Component]
    weights: dict[str, float]
    features: dict[str, Any]
