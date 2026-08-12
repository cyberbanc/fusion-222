from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import SETTINGS


@dataclass
class ShadowMetrics:
    samples: int
    wins: int
    losses: int
    win_rate: float
    pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    current_loss_streak: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_pnl(row: dict[str, Any]) -> float:
    if row.get("shadow_pnl") is not None:
        return float(row["shadow_pnl"])
    signal = str(row.get("signal") or "")
    winner = str(row.get("final_winner") or "")
    if signal not in {"UP", "DOWN"} or winner not in {"UP", "DOWN"}:
        return 0.0
    if signal != winner:
        return -SETTINGS.shadow_stake
    coeff = row.get("final_coeff_up") if signal == "UP" else row.get("final_coeff_down")
    try:
        return SETTINGS.shadow_stake * (float(coeff) - 1.0)
    except (TypeError, ValueError):
        return 0.0


def summarize(rows: list[dict[str, Any]]) -> ShadowMetrics:
    pnls = [_row_pnl(r) for r in rows]
    wins = sum(1 for x in pnls if x > 0)
    losses = sum(1 for x in pnls if x < 0)
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else None)
    streak = 0
    # Database rows are newest first.
    for value in pnls:
        if value < 0:
            streak += 1
        else:
            break
    total = wins + losses
    return ShadowMetrics(
        samples=total,
        wins=wins,
        losses=losses,
        win_rate=wins / total if total else 0.0,
        pnl=sum(pnls),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=pf,
        current_loss_streak=streak,
    )


def evaluate(source_key: str, signal: str, selected_ev: float) -> tuple[bool, str, dict[str, Any]]:
    """FUSION-222 shadow gate.

    EV >= 2% is mandatory. Recent Shadow PnL and Quality PF stay active.
    Quality win-rate is still calculated and exposed, but by default does not
    block trades (QUALITY_WIN_RATE_FILTER_ENABLED=false).
    """
    from . import db

    selected_ev = float(selected_ev)
    recent_rows = db.shadow_rows(source_key, signal, SETTINGS.shadow_recent_window)
    quality_rows = db.shadow_rows(source_key, signal, SETTINGS.quality_window)
    recent = summarize(recent_rows)
    quality = summarize(quality_rows)

    stats = {
        "source_key": source_key,
        "signal": signal,
        "selected_ev": selected_ev,
        "scope": "source_and_direction",
        "recent": recent.to_dict(),
        "quality": quality.to_dict(),
        "settings": {
            "min_trade_ev": SETTINGS.min_trade_ev,
            "recent_window": SETTINGS.shadow_recent_window,
            "recent_min_pnl": SETTINGS.shadow_recent_min_pnl,
            "quality_window": SETTINGS.quality_window,
            "quality_min_samples": SETTINGS.quality_min_samples,
            "quality_min_win_rate": SETTINGS.quality_min_win_rate,
            "quality_win_rate_filter_enabled": SETTINGS.quality_win_rate_filter_enabled,
            "quality_min_profit_factor": SETTINGS.quality_min_profit_factor,
        },
    }

    if selected_ev < SETTINGS.min_trade_ev:
        return False, "EV_BELOW_2_PERCENT", stats

    if not SETTINGS.shadow_filter_enabled:
        return True, "SHADOW_FILTER_DISABLED", stats

    if recent.samples >= SETTINGS.shadow_recent_window and recent.pnl <= SETTINGS.shadow_recent_min_pnl:
        return False, "SHADOW_RECENT_PNL_DEGRADED", stats

    if quality.samples >= SETTINGS.quality_min_samples:
        quality_pf = quality.profit_factor or 0.0
        if SETTINGS.quality_win_rate_filter_enabled and quality.win_rate < SETTINGS.quality_min_win_rate:
            return False, "QUALITY_WIN_RATE_BELOW_MINIMUM", stats
        if quality_pf < SETTINGS.quality_min_profit_factor:
            return False, "QUALITY_PROFIT_FACTOR_BELOW_MINIMUM", stats

    if quality.samples < SETTINGS.quality_min_samples:
        return True, "QUALITY_WARMUP_ALLOWED", stats
    return True, "SHADOW_QUALITY_PF_CONFIRMED", stats
