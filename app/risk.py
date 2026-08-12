from __future__ import annotations

from dataclasses import dataclass

from .config import SETTINGS


@dataclass(frozen=True)
class StakeDecision:
    stake: float
    tier: str
    eligible: bool


def fixed_stake_decision() -> StakeDecision:
    stake = max(0.0, float(SETTINGS.fixed_stake))
    return StakeDecision(stake, "FIXED_22", stake > 0.0)
