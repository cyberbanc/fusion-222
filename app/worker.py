from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from . import db
from .config import SETTINGS
from .ensemble import forecast
from .live_execution import from_env as live_executor_from_env
from .pancake_client import PancakeClient, from_env
from .risk import fixed_stake_decision
from .shadow import evaluate as evaluate_shadow

_LAST_TICK: dict[str, Any] = {}
_LAST_TICK_LOCK = threading.Lock()
_LAST_SIGNAL_CACHE: dict[str, Any] = {}
_LAST_SIGNAL_CACHE_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()
_LAST_SYNC_AT = 0.0


def _round_record(round_data) -> dict[str, Any]:
    winner = round_data.actual_winner
    gross = round_data.coefficient_gross(winner) if winner in {"UP", "DOWN"} else None
    return {
        "epoch": round_data.epoch,
        "start_timestamp": round_data.start_timestamp,
        "lock_timestamp": round_data.lock_timestamp,
        "close_timestamp": round_data.close_timestamp,
        "lock_price": round_data.lock_price,
        "close_price": round_data.close_price,
        "lock_oracle_id": round_data.lock_oracle_id,
        "close_oracle_id": round_data.close_oracle_id,
        "total_amount_bnb": round_data.total_amount_bnb,
        "bull_amount_bnb": round_data.bull_amount_bnb,
        "bear_amount_bnb": round_data.bear_amount_bnb,
        "reward_base_bnb": round_data.reward_base_bnb,
        "reward_amount_bnb": round_data.reward_amount_bnb,
        "oracle_called": round_data.oracle_called,
        "actual_winner": winner,
        "winner_coeff_gross": gross,
        "winner_coeff_net": gross * (1 - SETTINGS.treasury_fee) if gross else None,
    }


def bootstrap_rounds(client: PancakeClient) -> dict[str, Any]:
    current = client.current_epoch()
    saved = 0
    start = max(1, current - SETTINGS.bootstrap_lookback)
    for epoch in range(start, max(start, current - 1)):
        try:
            round_data = client.round(epoch)
            if round_data.oracle_called:
                db.upsert_round(_round_record(round_data))
                saved += 1
        except Exception:
            continue
    return {"saved": saved, "current_epoch": current}


def sync_recent_closed(client: PancakeClient, current_epoch: int) -> dict[str, Any] | None:
    global _LAST_SYNC_AT
    now = time.time()
    if now - _LAST_SYNC_AT < SETTINGS.sync_closed_seconds:
        return None
    _LAST_SYNC_AT = now
    saved = 0
    end = current_epoch - 2
    start = max(1, end - SETTINGS.sync_recent_lookback + 1)
    for epoch in range(start, end + 1):
        try:
            round_data = client.round(epoch)
            if round_data.oracle_called:
                db.upsert_round(_round_record(round_data))
                saved += 1
        except Exception:
            continue
    return {"saved": saved, "from_epoch": start, "to_epoch": end}


def settle_finished(client: PancakeClient, current_epoch: int | None = None) -> int:
    current_epoch = current_epoch or client.current_epoch()
    settled_count = 0
    for decision in db.unsettled_decisions(limit=200):
        epoch = int(decision["betting_epoch"])
        if epoch > current_epoch - 2:
            continue
        try:
            round_data = client.round(epoch)
            if not round_data.oracle_called or round_data.actual_winner is None:
                continue
            db.upsert_round(_round_record(round_data))
            winner = round_data.actual_winner
            coeff_up_gross = round_data.coefficient_gross("UP")
            coeff_down_gross = round_data.coefficient_gross("DOWN")
            coeff_up = coeff_up_gross * (1 - SETTINGS.treasury_fee) if coeff_up_gross else None
            coeff_down = coeff_down_gross * (1 - SETTINGS.treasury_fee) if coeff_down_gross else None
            winner_gross = coeff_up_gross if winner == "UP" else coeff_down_gross if winner == "DOWN" else None
            winner_net = coeff_up if winner == "UP" else coeff_down if winner == "DOWN" else None
            signal = str(decision.get("signal") or "")
            trade_executed = bool(decision.get("trade_executed"))
            stake = float(decision.get("stake") or 0.0)

            if winner == "DRAW":
                outcome = "REFUND" if trade_executed else "SKIP"
                pnl = 0.0
                shadow_pnl = 0.0
            elif signal == winner:
                outcome = "WIN" if trade_executed else "SKIP"
                selected_coeff = coeff_up if signal == "UP" else coeff_down
                pnl = stake * ((selected_coeff or 1.0) - 1.0) if trade_executed else 0.0
                shadow_pnl = SETTINGS.shadow_stake * ((selected_coeff or 1.0) - 1.0)
            else:
                outcome = "LOSS" if trade_executed else "SKIP"
                pnl = -stake if trade_executed else 0.0
                shadow_pnl = -SETTINGS.shadow_stake

            probability = (
                float(decision.get("probability_up") or 0.5)
                if signal == "UP"
                else float(decision.get("probability_down") or 0.5)
            )
            selected_final_coeff = coeff_up if signal == "UP" else coeff_down
            actual_ev = probability * selected_final_coeff - 1.0 if selected_final_coeff else None
            raw_expected = (
                float(decision.get("raw_expected_coeff_up") or 0)
                if signal == "UP"
                else float(decision.get("raw_expected_coeff_down") or 0)
            )
            payout_ratio = selected_final_coeff / raw_expected if selected_final_coeff and raw_expected > 0 else None
            move_points = (
                abs(float(round_data.close_price) - float(round_data.lock_price))
                if round_data.close_price is not None and round_data.lock_price is not None
                else None
            )
            changed = db.settle_decision_atomic(
                epoch,
                final_winner=winner,
                final_coeff_gross=winner_gross,
                final_coeff_net=winner_net,
                final_coeff_up=coeff_up,
                final_coeff_down=coeff_down,
                final_move_points=move_points,
                outcome=outcome,
                pnl=pnl,
                shadow_pnl=shadow_pnl,
                actual_ev_signal=actual_ev,
                payout_ratio_signal=payout_ratio,
            )
            if changed:
                settled_count += 1
        except Exception:
            continue
    return settled_count


def create_locked_decision(snapshot) -> dict[str, Any]:
    existing = db.get_decision(snapshot.betting_epoch)
    if existing:
        return existing

    result = forecast(snapshot)
    selected_ready = result.payout_bucket_ready_up if result.signal == "UP" else result.payout_bucket_ready_down
    selected_probability = float(result.probability_up if result.signal == "UP" else result.probability_down)
    shadow_allowed, shadow_reason, shadow_stats = evaluate_shadow(
        result.source_key, result.signal, result.selected_ev
    )

    allowed = True
    no_trade_reason: str | None = None
    if SETTINGS.require_payout_bucket_ready and not selected_ready:
        allowed = False
        no_trade_reason = "PAYOUT_BUCKET_NOT_READY"
    elif float(result.selected_ev) < SETTINGS.min_trade_ev:
        allowed = False
        no_trade_reason = "EV_BELOW_2_PERCENT"
    elif selected_probability < SETTINGS.min_signal_probability:
        allowed = False
        no_trade_reason = "PROBABILITY_BELOW_53_PERCENT"
    elif SETTINGS.trade_filter_enabled and not shadow_allowed:
        allowed = False
        no_trade_reason = shadow_reason

    stake_decision = fixed_stake_decision()
    stake = stake_decision.stake if allowed else 0.0
    execution_requested = allowed and stake_decision.eligible and stake > 0.0
    real_mode = SETTINGS.execution_mode == "REAL"
    trade_executed = execution_requested and not real_mode
    stake_tier = stake_decision.tier if execution_requested else "NO_TRADE"
    prefix = "FUSION_222_TRADE" if execution_requested else "FUSION_222_NO_TRADE"
    decision_quality = f"{prefix}_{no_trade_reason or shadow_reason}_{result.selection_reason}"
    state = db.get_state()

    features = dict(result.features)
    features.update(
        {
            "strategy": "FUSION-222",
            "trade_executed": trade_executed,
            "execution_requested": execution_requested,
            "execution_mode": SETTINGS.execution_mode,
            "no_trade_reason": no_trade_reason,
            "trade_rule": "EV>=2%_P>=53%_SHADOW_RECENT_QUALITY_PF_FIXED22" if execution_requested else "NO_TRADE",
            "min_trade_ev": SETTINGS.min_trade_ev,
            "min_signal_probability": SETTINGS.min_signal_probability,
            "selected_probability": selected_probability,
            "fixed_stake": SETTINGS.fixed_stake,
            "shadow_filter_enabled": SETTINGS.shadow_filter_enabled,
            "shadow_allowed": shadow_allowed,
            "shadow_reason": shadow_reason,
            "shadow_stats": shadow_stats,
            "quality_win_rate_filter_enabled": SETTINGS.quality_win_rate_filter_enabled,
            "quality_min_profit_factor": SETTINGS.quality_min_profit_factor,
            "selected_payout_bucket_ready": selected_ready,
            "stake_mode": "fixed_22",
            "stake_tier": stake_tier,
        }
    )

    return db.insert_decision(
        {
            "betting_epoch": snapshot.betting_epoch,
            "live_epoch": snapshot.live_epoch,
            "locked_at_chain_timestamp": snapshot.chain_timestamp,
            "locked_at_seconds_to_lock": snapshot.seconds_to_lock,
            "signal": result.signal,
            "probability_up": result.probability_up,
            "probability_down": result.probability_down,
            "expected_coeff_up": result.expected_coeff_up,
            "expected_coeff_down": result.expected_coeff_down,
            "ev_up": result.ev_up,
            "ev_down": result.ev_down,
            "selected_ev": result.selected_ev,
            "agreement": result.agreement,
            "decision_quality": decision_quality,
            "stake": stake,
            "bank_before": float(state.get("bank") or SETTINGS.start_bank),
            "components_json": [x.to_dict() for x in result.components],
            "weights_json": result.weights,
            "features_json": features,
            "snapshot_json": snapshot.to_dict(),
            "raw_expected_coeff_up": result.raw_expected_coeff_up,
            "raw_expected_coeff_down": result.raw_expected_coeff_down,
            "payout_correction_up": result.payout_correction_up,
            "payout_correction_down": result.payout_correction_down,
            "strategy_version": SETTINGS.version,
            "payout_bucket_up": result.payout_bucket_up,
            "payout_bucket_down": result.payout_bucket_down,
            "trade_executed": trade_executed,
            "no_trade_reason": no_trade_reason,
            "source_key": result.source_key,
            "selection_reason": result.selection_reason,
            "shadow_allowed": shadow_allowed,
            "shadow_reason": shadow_reason,
            "shadow_stats_json": shadow_stats,
            "stake_mode": "fixed_22",
            "stake_tier": stake_tier,
            "breaker_applied": False,
            "origin": "LIVE",
            "execution_requested": execution_requested,
            "execution_status": "PENDING" if execution_requested and real_mode else "CONFIRMED" if trade_executed else "SKIPPED",
            "wallet_address": live_executor_from_env().address if real_mode else None,
            "stake_bnb": None,
            "bnb_usd_price": float(getattr(snapshot, "chainlink_price", 0.0)) if real_mode else None,
            "reconciliation_status": "AWAITING_ONCHAIN_EXECUTION" if execution_requested and real_mode else None,
        }
    )


def create_mirrored_decision(snapshot) -> dict[str, Any] | None:
    """Copy the exact locked paper decision, then execute it independently on-chain.

    The paper table is read-only. If the paper worker has not written this epoch
    yet, return ``None`` so a later tick can retry instead of inventing a second
    strategy decision.
    """
    existing = db.get_decision(snapshot.betting_epoch)
    if existing:
        return existing
    reference = db.get_reference_decision(snapshot.betting_epoch)
    if not reference:
        return None

    paper_executed = bool(reference.get("trade_executed"))
    stake = float(reference.get("stake") or 0.0) if paper_executed else 0.0
    execution_requested = paper_executed and stake > 0.0
    features = dict(reference.get("features_json") or {})
    features.update(
        {
            "execution_mode": SETTINGS.execution_mode,
            "execution_requested": execution_requested,
            "mirrored_from_paper": True,
            "paper_reference_table": SETTINGS.reference_decisions_table,
            "paper_strategy_version": reference.get("strategy_version"),
            "paper_created_at": str(reference.get("created_at") or ""),
        }
    )
    return db.insert_decision(
        {
            "betting_epoch": int(reference.get("betting_epoch") or snapshot.betting_epoch),
            "live_epoch": reference.get("live_epoch", snapshot.live_epoch),
            "locked_at_chain_timestamp": reference.get(
                "locked_at_chain_timestamp", snapshot.chain_timestamp
            ),
            "locked_at_seconds_to_lock": reference.get(
                "locked_at_seconds_to_lock", snapshot.seconds_to_lock
            ),
            "signal": reference.get("signal"),
            "probability_up": reference.get("probability_up"),
            "probability_down": reference.get("probability_down"),
            "expected_coeff_up": reference.get("expected_coeff_up"),
            "expected_coeff_down": reference.get("expected_coeff_down"),
            "ev_up": reference.get("ev_up"),
            "ev_down": reference.get("ev_down"),
            "selected_ev": reference.get("selected_ev"),
            "agreement": reference.get("agreement"),
            "decision_quality": reference.get("decision_quality"),
            "stake": stake,
            "bank_before": reference.get("bank_before"),
            "components_json": reference.get("components_json") or [],
            "weights_json": reference.get("weights_json") or {},
            "features_json": features,
            "snapshot_json": reference.get("snapshot_json") or snapshot.to_dict(),
            "raw_expected_coeff_up": reference.get("raw_expected_coeff_up"),
            "raw_expected_coeff_down": reference.get("raw_expected_coeff_down"),
            "payout_correction_up": reference.get("payout_correction_up"),
            "payout_correction_down": reference.get("payout_correction_down"),
            "strategy_version": reference.get("strategy_version"),
            "payout_bucket_up": reference.get("payout_bucket_up"),
            "payout_bucket_down": reference.get("payout_bucket_down"),
            # A real trade becomes executed only after its receipt is confirmed.
            "trade_executed": False,
            "no_trade_reason": reference.get("no_trade_reason"),
            "source_key": reference.get("source_key"),
            "selection_reason": reference.get("selection_reason"),
            "shadow_allowed": reference.get("shadow_allowed"),
            "shadow_reason": reference.get("shadow_reason"),
            "shadow_stats_json": reference.get("shadow_stats_json") or {},
            "stake_mode": reference.get("stake_mode") or "fixed_22",
            "stake_tier": reference.get("stake_tier") or (
                "FIXED_22" if execution_requested else "NO_TRADE"
            ),
            "breaker_applied": bool(reference.get("breaker_applied")),
            "origin": "LIVE_REAL_MIRROR",
            "execution_requested": execution_requested,
            "execution_status": "PENDING" if execution_requested else "SKIPPED",
            "wallet_address": live_executor_from_env().address,
            "stake_bnb": None,
            "bnb_usd_price": float(getattr(snapshot, "chainlink_price", 0.0)),
            "reconciliation_status": (
                "AWAITING_ONCHAIN_EXECUTION" if execution_requested else "PAPER_NO_TRADE"
            ),
        }
    )


def reconcile_actual_results(client: PancakeClient, bnb_usd_price: float) -> int:
    if SETTINGS.execution_mode != "REAL":
        return 0
    executor = live_executor_from_env(client)
    changed = 0
    for decision in db.actual_reconciliation_candidates(limit=50):
        try:
            before = decision.get("actual_pnl_bnb")
            updated = executor.finalize_actual(decision, bnb_usd_price)
            if before is None and updated.get("actual_pnl_bnb") is not None:
                changed += 1
        except Exception:
            continue
    return changed


def tick() -> dict[str, Any]:
    with _TICK_LOCK:
        started = time.time()
        client = from_env()
        snapshot = client.market_snapshot()
        settled = settle_finished(client, snapshot.current_epoch)
        sync = sync_recent_closed(client, snapshot.current_epoch)
        snapshot_saved = False
        if SETTINGS.prelock_seconds <= snapshot.seconds_to_lock <= SETTINGS.snapshot_start_seconds:
            snapshot_saved = db.save_snapshot(
                {
                    **snapshot.to_dict(),
                    "bull_amount_bnb": snapshot.betting_round.bull_amount_bnb,
                    "bear_amount_bnb": snapshot.betting_round.bear_amount_bnb,
                }
            )
        decision = db.get_decision(snapshot.betting_epoch)
        created = False
        if decision is None and snapshot.decision_window:
            if SETTINGS.execution_mode == "REAL" and SETTINGS.mirror_paper_decisions:
                decision = create_mirrored_decision(snapshot)
            else:
                decision = create_locked_decision(snapshot)
            created = decision is not None
        executor_status: dict[str, Any] | None = None
        wallet: dict[str, Any] = {}
        if SETTINGS.execution_mode == "REAL":
            executor = live_executor_from_env(client)
            if decision and bool(decision.get("execution_requested")):
                has_tx = bool(decision.get("tx_hash"))
                if has_tx or snapshot.decision_window:
                    try:
                        decision = executor.execute_bet(decision, float(snapshot.chainlink_price))
                    except Exception as exc:
                        decision = db.update_execution(
                            int(decision["betting_epoch"]),
                            execution_status="ERROR",
                            execution_error=f"{type(exc).__name__}: {exc}",
                            trade_executed=False,
                        )
            try:
                wallet = executor.wallet_snapshot(float(snapshot.chainlink_price))
            except Exception as exc:
                wallet = {"error": f"{type(exc).__name__}: {exc}"}
            executor_status = executor.status()
        reconciled_actual = reconcile_actual_results(client, float(snapshot.chainlink_price))
        state = db.get_state()
        result = {
            "ok": True,
            "message": "tick_complete",
            "chain_timestamp": snapshot.chain_timestamp,
            "current_epoch": snapshot.current_epoch,
            "betting_epoch": snapshot.betting_epoch,
            "live_epoch": snapshot.live_epoch,
            "seconds_to_lock": snapshot.seconds_to_lock,
            "decision_window": snapshot.decision_window,
            "mirror_paper_decisions": SETTINGS.mirror_paper_decisions,
            "snapshot_saved": snapshot_saved,
            "decision_locked": decision is not None,
            "trade_executed": decision.get("trade_executed") if decision else None,
            "decision_signal": decision.get("signal") if decision else None,
            "no_trade_reason": decision.get("no_trade_reason") if decision else None,
            "stake": float(decision.get("stake") or 0) if decision else 0.0,
            "stake_mode": "fixed_22",
            "fixed_stake": SETTINGS.fixed_stake,
            "min_trade_ev": SETTINGS.min_trade_ev,
            "min_signal_probability": SETTINGS.min_signal_probability,
            "settled_now": settled,
            "actual_results_reconciled_now": reconciled_actual,
            "sync": sync,
            "rpc": client.rpc_status(),
            "execution": executor_status,
            "wallet": wallet,
            "created_or_existing_decision": created,
            "duration_ms": round((time.time() - started) * 1000, 2),
            "updated_at": int(time.time()),
        }
        with _LAST_TICK_LOCK:
            _LAST_TICK.clear()
            _LAST_TICK.update(result)
        with _LAST_SIGNAL_CACHE_LOCK:
            _LAST_SIGNAL_CACHE.clear()
            _LAST_SIGNAL_CACHE.update(
                {
                    "ok": True,
                    "snapshot": snapshot.to_dict(),
                    "decision": dict(decision) if decision else None,
                    "worker_tick": dict(result),
                    "updated_at": int(time.time()),
                }
            )
        return result


def signal_cache() -> dict[str, Any]:
    with _LAST_SIGNAL_CACHE_LOCK:
        return dict(_LAST_SIGNAL_CACHE)


def status() -> dict[str, Any]:
    with _LAST_TICK_LOCK:
        last_tick = dict(_LAST_TICK)
    state = db.get_state() if db.enabled() else {}
    return {
        "enabled": SETTINGS.worker_enabled,
        "strategy": "fusion_222_ev2_prob53_shadow_recent_quality_pf_fixed22",
        "version": SETTINGS.version,
        "stake_mode": "fixed_22",
        "fixed_stake": SETTINGS.fixed_stake,
        "min_trade_ev": SETTINGS.min_trade_ev,
        "min_signal_probability": SETTINGS.min_signal_probability,
        "shadow_filter_enabled": SETTINGS.shadow_filter_enabled,
        "shadow_recent_window": SETTINGS.shadow_recent_window,
        "shadow_recent_min_pnl": SETTINGS.shadow_recent_min_pnl,
        "quality_window": SETTINGS.quality_window,
        "quality_min_samples": SETTINGS.quality_min_samples,
        "quality_min_win_rate": SETTINGS.quality_min_win_rate,
        "quality_win_rate_filter_enabled": SETTINGS.quality_win_rate_filter_enabled,
        "quality_min_profit_factor": SETTINGS.quality_min_profit_factor,
        "require_payout_bucket_ready": SETTINGS.require_payout_bucket_ready,
        "execution": live_executor_from_env().status() if SETTINGS.execution_mode == "REAL" else {"mode": "PAPER"},
        "last_tick": last_tick,
    }


async def loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception as exc:
            error_result = {
                "ok": False,
                "message": "tick_error",
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": int(time.time()),
            }
            with _LAST_TICK_LOCK:
                _LAST_TICK.clear()
                _LAST_TICK.update(error_result)
            with _LAST_SIGNAL_CACHE_LOCK:
                _LAST_SIGNAL_CACHE["worker_tick"] = dict(error_result)
                _LAST_SIGNAL_CACHE["error"] = error_result["error"]
                _LAST_SIGNAL_CACHE["updated_at"] = error_result["updated_at"]
        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS.poll_seconds)
        except asyncio.TimeoutError:
            pass
