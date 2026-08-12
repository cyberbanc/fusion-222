from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any

from . import db
from .binance_client import from_env as binance_from_env
from .config import SETTINGS
from .models import Component, Forecast, MarketSnapshot


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _bucket(coeff: float) -> tuple[int, str]:
    edges = SETTINGS.payout_bucket_edges
    names = ["< 1.50", "1.50-2.00", "2.00-2.50", "2.50-3.00", ">= 3.00"]
    for index, edge in enumerate(edges):
        if coeff < edge:
            return index, names[index]
    return len(edges), names[-1]


def _quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("empty values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * _clip(q, 0.0, 1.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def payout_correction(side: str, coeff: float) -> tuple[float, str, bool, dict[str, Any]]:
    idx, name = _bucket(coeff)
    initial = SETTINGS.payout_bucket_initial_up if side == "UP" else SETTINGS.payout_bucket_initial_down
    maximum = SETTINGS.payout_bucket_max_up if side == "UP" else SETTINGS.payout_bucket_max_down
    base = initial[min(idx, len(initial) - 1)]
    cap = maximum[min(idx, len(maximum) - 1)]
    ratios = db.payout_ratios(side, name, SETTINGS.payout_calibration_lookback)
    ready = len(ratios) >= SETTINGS.payout_bucket_min_samples
    learned = _quantile(ratios, SETTINGS.payout_calibration_quantile) if ready else base
    correction = _clip(min(cap, learned), SETTINGS.payout_correction_min, SETTINGS.payout_correction_max)
    return correction, name, ready, {
        "name": name,
        "index": idx,
        "ready": ready,
        "sample_count": len(ratios),
        "initial": base,
        "maximum": cap,
        "learned_quantile": learned,
        "correction": correction,
    }


def _price_component(snapshot: MarketSnapshot, snapshots: list[dict[str, Any]]) -> Component:
    moves = [float(x.get("live_move_signed") or 0) for x in snapshots]
    moves.append(snapshot.live_move_signed)
    deltas = [moves[i] - moves[i - 1] for i in range(1, len(moves))]
    typical = statistics.median([abs(x) for x in deltas if abs(x) > 1e-9]) if deltas else 0.10
    typical = max(typical, 0.03)
    move_norm = snapshot.live_move_signed / typical
    momentum = (moves[-1] - moves[0]) / typical if len(moves) > 1 else move_norm
    acceleration = (deltas[-1] - deltas[-2]) / typical if len(deltas) > 1 else 0.0
    score = math.tanh(move_norm / 4.0) * 0.50 + math.tanh(momentum / 4.0) * 0.35 + math.tanh(acceleration / 4.0) * 0.15
    p_up = _clip(0.5 + score * 0.16, 0.34, 0.66)
    reliability = _clip(0.70 + min(abs(move_norm), 4.0) * 0.075, 0.70, 1.0)
    return Component(
        name="price",
        probability_up=p_up,
        reliability=reliability,
        available=True,
        reason="chainlink_live_move_momentum",
        details={
            "move_norm": move_norm,
            "momentum_norm": momentum,
            "acceleration": acceleration,
            "typical_move": typical,
        },
    )


def _crowd_component(snapshot: MarketSnapshot, snapshots: list[dict[str, Any]]) -> Component:
    total = snapshot.betting_round.total_amount_bnb
    bull = snapshot.betting_round.bull_amount_bnb
    share = bull / total if total > 0 else 0.5
    old_share = share
    if snapshots:
        first = snapshots[0]
        old_bull = float(first.get("bull_amount_bnb") or 0)
        old_bear = float(first.get("bear_amount_bnb") or 0)
        old_total = old_bull + old_bear
        if old_total > 0:
            old_share = old_bull / old_total
    share_change = share - old_share
    imbalance = (share - 0.5) * 2
    price_sign = 1.0 if snapshot.live_move_signed >= 0 else -1.0
    # Moderate crowd follows price; extreme crowd gets a mild contrarian haircut.
    if abs(imbalance) >= 0.70:
        score = imbalance * -0.18 + share_change * 0.35 + price_sign * 0.08
        reason = "extreme_crowd_mild_contrarian"
    else:
        score = imbalance * 0.28 + share_change * 0.40 + price_sign * 0.08
        reason = "moderate_crowd_mild_follow"
    p_up = _clip(0.5 + score * 0.22, 0.37, 0.63)
    return Component(
        name="crowd",
        probability_up=p_up,
        reliability=1.0 if total >= SETTINGS.min_pool_bnb else 0.55,
        available=total > 0,
        reason=reason,
        details={
            "imbalance": imbalance,
            "price_sign": price_sign,
            "bull_share": share,
            "bull_share_change": share_change,
            "pool_bnb": total,
        },
    )


def _binance_component() -> Component:
    data = binance_from_env().snapshot()
    return Component(
        name="binance",
        probability_up=float(data.get("probability_up", 0.5)),
        reliability=1.0 if data.get("available") else 0.0,
        available=bool(data.get("available")),
        reason="binance_microstructure" if data.get("available") else str(data.get("reason", "unavailable")),
        details={k: v for k, v in data.items() if k != "probability_up"},
    )


def _m9_component(rounds: list[dict[str, Any]], coeff: float) -> Component:
    if len(rounds) < 8:
        return Component("m9", 0.5, 0.0, False, "not_enough_history", {})
    state = tuple(str(x["actual_winner"]) for x in rounds[-4:])
    _, zone = _bucket(coeff)
    ups = 1.0
    downs = 1.0
    matches = 0
    for i in range(4, len(rounds)):
        prior = tuple(str(x["actual_winner"]) for x in rounds[i - 4 : i])
        prior_coeff = float(rounds[i - 1].get("winner_coeff_net") or SETTINGS.neutral_net_coefficient)
        _, prior_zone = _bucket(prior_coeff)
        if prior == state and prior_zone == zone:
            matches += 1
            age = len(rounds) - i
            weight = math.exp(-age / 400.0)
            if rounds[i]["actual_winner"] == "UP":
                ups += weight
            else:
                downs += weight
    if matches == 0:
        return Component("m9", 0.5, 0.0, False, "no_exact_state", {"state": state, "zone": zone})
    probability = ups / (ups + downs)
    reliability = _clip(matches / 50.0, 0.10, 1.0)
    return Component(
        "m9", probability, reliability, True, "bayesian_exact_state_with_decay",
        {"state": state, "matches": matches, "up_weight": ups, "down_weight": downs, "coefficient_zone": zone},
    )


def _pattern_component(rounds: list[dict[str, Any]]) -> Component:
    winners = [str(x["actual_winner"]) for x in rounds]
    best: tuple[float, int, int, float] | None = None
    for length in range(3, SETTINGS.pattern_max_length + 1):
        if len(winners) <= length:
            continue
        pattern = tuple(winners[-length:])
        following = [winners[i] for i in range(length, len(winners)) if tuple(winners[i-length:i]) == pattern]
        if len(following) < SETTINGS.pattern_min_count:
            continue
        up_p = following.count("UP") / len(following)
        edge = abs(up_p - 0.5)
        if best is None or edge > best[0]:
            best = (edge, length, len(following), up_p)
    if best is None:
        return Component("pattern", 0.5, 0.0, False, "no_stable_pattern", {})
    edge, length, count, up_p = best
    return Component(
        "pattern", up_p, _clip(count / 80.0, 0.20, 1.0), True,
        "stable_multilength_pattern",
        {"length": length, "count": count, "probability": up_p, "edge": edge},
    )


def forecast(snapshot: MarketSnapshot) -> Forecast:
    snapshots = db.snapshots_for_epoch(snapshot.betting_epoch, limit=30)
    rounds = db.recent_rounds(SETTINGS.m9_history_limit)
    raw_up = snapshot.current_net_coeff_up or SETTINGS.neutral_net_coefficient
    raw_down = snapshot.current_net_coeff_down or SETTINGS.neutral_net_coefficient
    raw_up = _clip(raw_up, 1.01, SETTINGS.payout_cap)
    raw_down = _clip(raw_down, 1.01, SETTINGS.payout_cap)

    components = [
        _price_component(snapshot, snapshots),
        _binance_component(),
        _crowd_component(snapshot, snapshots),
        _m9_component(rounds, raw_up),
        _pattern_component(rounds),
    ]
    configured = {
        "price": SETTINGS.weight_price,
        "binance": SETTINGS.weight_binance,
        "crowd": SETTINGS.weight_crowd,
        "m9": SETTINGS.weight_m9,
        "pattern": SETTINGS.weight_pattern,
    }
    effective_raw: dict[str, float] = {}
    for component in components:
        effective_raw[component.name] = (
            configured.get(component.name, 0.0) * component.reliability
            if component.available
            else 0.0
        )
    total_weight = sum(effective_raw.values())
    if total_weight <= 0:
        effective_raw = {"price": 1.0}
        total_weight = 1.0
    weights = {name: value / total_weight for name, value in effective_raw.items()}
    raw_probability_up = sum(c.probability_up * weights.get(c.name, 0.0) for c in components)
    probability_up = _clip(
        0.5 + (raw_probability_up - 0.5) * SETTINGS.probability_shrink,
        0.30,
        0.70,
    )
    probability_down = 1.0 - probability_up

    corr_up, bucket_up, ready_up, bucket_info_up = payout_correction("UP", raw_up)
    corr_down, bucket_down, ready_down, bucket_info_down = payout_correction("DOWN", raw_down)
    expected_up = raw_up * corr_up
    expected_down = raw_down * corr_down
    ev_up = probability_up * expected_up - 1.0
    ev_down = probability_down * expected_down - 1.0

    by_name = {c.name: c for c in components}
    binance_side = "UP" if by_name["binance"].probability_up >= 0.5 else "DOWN"
    crowd_side = "UP" if by_name["crowd"].probability_up >= 0.5 else "DOWN"
    if max(ev_up, ev_down) >= 0:
        signal = "UP" if ev_up >= ev_down else "DOWN"
        source_key = "EV_PRIMARY"
        selection_reason = "POSITIVE_EV_BEST_SIDE"
    elif by_name["binance"].available and by_name["crowd"].available and binance_side == crowd_side:
        signal = binance_side
        source_key = "CROWD_BINANCE_FALLBACK"
        selection_reason = "WEAK_EV_CROWD_BINANCE_FALLBACK"
    else:
        signal = "UP" if probability_up >= 0.5 else "DOWN"
        source_key = "PROBABILITY_FALLBACK"
        selection_reason = "NEGATIVE_EV_PROBABILITY_FALLBACK"
    selected_ev = ev_up if signal == "UP" else ev_down
    agreement = sum(
        weights.get(c.name, 0.0)
        for c in components
        if (c.probability_up >= 0.5) == (signal == "UP")
    )
    features = {
        "raw_probability_up": raw_probability_up,
        "probability_shrink": SETTINGS.probability_shrink,
        "probability_signal": "UP" if probability_up >= 0.5 else "DOWN",
        "ev_candidate_signal": "UP" if ev_up >= ev_down else "DOWN",
        "selection_reason": selection_reason,
        "source_key": source_key,
        "payout_bucket_up": bucket_info_up,
        "payout_bucket_down": bucket_info_down,
        "payout_bucket_ready_up": ready_up,
        "payout_bucket_ready_down": ready_down,
        "binance_available": by_name["binance"].available,
        "crowd_binance_consensus": binance_side if binance_side == crowd_side else None,
    }
    return Forecast(
        signal=signal,
        probability_up=probability_up,
        probability_down=probability_down,
        raw_probability_up=raw_probability_up,
        raw_expected_coeff_up=raw_up,
        raw_expected_coeff_down=raw_down,
        payout_correction_up=corr_up,
        payout_correction_down=corr_down,
        payout_bucket_up=bucket_up,
        payout_bucket_down=bucket_down,
        payout_bucket_ready_up=ready_up,
        payout_bucket_ready_down=ready_down,
        expected_coeff_up=expected_up,
        expected_coeff_down=expected_down,
        ev_up=ev_up,
        ev_down=ev_down,
        selected_ev=selected_ev,
        agreement=agreement,
        source_key=source_key,
        selection_reason=selection_reason,
        components=components,
        weights=weights,
        features=features,
    )
