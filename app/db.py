from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable
from collections.abc import Mapping

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from .config import SETTINGS

_LOCK = threading.RLock()
_RETRO_CACHE_LOCK = threading.RLock()
_RETRO_CACHE: dict[str, Any] = {"cutoff": None, "at": 0.0, "rows": [], "metrics": {}}

_DECISIONS_TABLE = SETTINGS.bot_decisions_table
_STATE_TABLE = SETTINGS.bot_state_table
_ROUNDS_TABLE = SETTINGS.base_rounds_table
_SNAPSHOTS_TABLE = SETTINGS.bot_snapshots_table
_BASE_DECISIONS_TABLE = SETTINGS.base_decisions_table


def enabled() -> bool:
    return bool(SETTINGS.database_url)


@contextmanager
def conn():
    if not enabled():
        raise RuntimeError("DATABASE_URL is required")
    c = psycopg2.connect(SETTINGS.database_url, connect_timeout=10)
    try:
        yield c
    finally:
        c.close()


def _ident(name: str):
    return sql.Identifier(name)


def _first_scalar(row: Any, *, key: str | None = None, default: Any = None) -> Any:
    """Return the first scalar from either a tuple cursor row or RealDictCursor row.

    psycopg2 standard cursors return tuples while RealDictCursor returns mapping-like
    rows. Startup code uses both cursor types, so direct ``row[0]`` access is unsafe
    when a RealDictCursor is passed into a helper. This adapter makes scalar reads
    cursor-type agnostic and prevents startup failures caused by cursor shape.
    """
    if row is None:
        return default
    if isinstance(row, Mapping):
        if key is not None and key in row:
            return row[key]
        try:
            return next(iter(row.values()))
        except StopIteration:
            return default
    try:
        return row[0]
    except (KeyError, IndexError, TypeError):
        return default


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)",
        (name,),
    )
    return bool(_first_scalar(cur.fetchone(), default=False))


def _existing_tables(cur) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    return {str(r[0]) for r in cur.fetchall()}


def _table_columns(cur) -> dict[str, set[str]]:
    cur.execute(
        "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'"
    )
    result: dict[str, set[str]] = {}
    for table, column in cur.fetchall():
        result.setdefault(str(table), set()).add(str(column))
    return result


def _choose_existing_table(
    existing: set[str],
    columns: dict[str, set[str]],
    configured: str,
    candidates: Iterable[str],
    signature: set[str],
    *,
    exclude: set[str] | None = None,
    min_signature_matches: int | None = None,
) -> str | None:
    """Resolve a legacy/main Fusion table without ever selecting FUSION-222 private tables.

    The main Fusion bot historically auto-detected its table names. FUSION-222
    must therefore do the same instead of assuming the physical name is
    literally ``paper_decisions``.
    """
    exclude = set(exclude or set())
    configured = str(configured or "").strip()
    if configured and configured.lower() != "auto" and configured in existing and configured not in exclude:
        return configured
    for name in candidates:
        if name in existing and name not in exclude:
            return name

    scored: list[tuple[int, int, str]] = []
    for table, table_columns in columns.items():
        if table in exclude or table.startswith("fusion222_"):
            continue
        score = len(signature & table_columns)
        if score:
            scored.append((score, len(table_columns), table))
    scored.sort(reverse=True)
    if scored:
        threshold = min_signature_matches if min_signature_matches is not None else max(2, len(signature) // 2)
        if scored[0][0] >= threshold:
            return scored[0][2]
    return None


def _resolve_base_tables(cur) -> None:
    global _BASE_DECISIONS_TABLE, _ROUNDS_TABLE
    existing = _existing_tables(cur)
    columns = _table_columns(cur)
    private = {_DECISIONS_TABLE, _STATE_TABLE, _SNAPSHOTS_TABLE}

    decision_signature = {
        "betting_epoch", "signal", "selected_ev", "probability_up",
        "probability_down", "strategy_version", "settled", "final_winner",
    }
    resolved_decisions = _choose_existing_table(
        existing,
        columns,
        SETTINGS.base_decisions_table,
        ("paper_decisions", "decisions", "fusion_decisions", "paper_history", "fusion_history"),
        decision_signature,
        exclude=private,
        min_signature_matches=5,
    )
    if not resolved_decisions:
        visible = ", ".join(sorted(existing)) or "<none>"
        raise RuntimeError(
            "Main Fusion decisions table could not be auto-detected. "
            f"Configured BASE_DECISIONS_TABLE={SETTINGS.base_decisions_table!r}; "
            f"public tables: {visible}"
        )
    _BASE_DECISIONS_TABLE = resolved_decisions

    round_signature = {"epoch", "lock_price", "close_price", "oracle_called", "actual_winner"}
    resolved_rounds = _choose_existing_table(
        existing,
        columns,
        SETTINGS.base_rounds_table,
        ("round_history", "rounds_history", "fusion_rounds"),
        round_signature,
        exclude=private | {_BASE_DECISIONS_TABLE},
        min_signature_matches=3,
    )
    # Round history is safe to create if no legacy round table exists; the
    # decisions history is not.
    _ROUNDS_TABLE = resolved_rounds or (
        SETTINGS.base_rounds_table
        if str(SETTINGS.base_rounds_table or "").strip().lower() not in {"", "auto"}
        else "round_history"
    )


def _add_columns(cur, table: str, specs: dict[str, str]) -> None:
    for name, ddl in specs.items():
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                _ident(table), _ident(name), sql.SQL(ddl)
            )
        )


def init_db() -> None:
    """Create only FUSION-222 private tables inside the existing Fusion DB.

    The historical/main Fusion tables are read-only inputs. FUSION-222 never
    inserts into or rewrites the main paper_decisions table.
    """
    with _LOCK, conn() as c, c.cursor() as cur:
        _resolve_base_tables(cur)

        # Dedicated state: initialized from the historical counterfactual replay
        # exactly once, then continued by live FUSION-222 paper trades.
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    id INTEGER PRIMARY KEY,
                    start_bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    draws INTEGER NOT NULL DEFAULT 0,
                    trades_count INTEGER NOT NULL DEFAULT 0,
                    gross_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
                    gross_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
                    current_loss_streak INTEGER NOT NULL DEFAULT 0,
                    max_loss_streak INTEGER NOT NULL DEFAULT 0,
                    peak_bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    min_bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
                    last_settled_epoch BIGINT,
                    breaker_loss_count INTEGER NOT NULL DEFAULT 0,
                    breaker_signals_remaining INTEGER NOT NULL DEFAULT 0,
                    breaker_trigger_count INTEGER NOT NULL DEFAULT 0,
                    retro_initialized BOOLEAN NOT NULL DEFAULT FALSE,
                    retro_cutoff_epoch BIGINT,
                    retro_trades_count INTEGER NOT NULL DEFAULT 0,
                    retro_wins INTEGER NOT NULL DEFAULT 0,
                    retro_losses INTEGER NOT NULL DEFAULT 0,
                    retro_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                    retro_gross_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
                    retro_gross_loss DOUBLE PRECISION NOT NULL DEFAULT 0,
                    retro_peak_bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    retro_min_bank DOUBLE PRECISION NOT NULL DEFAULT 500,
                    retro_max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
                    retro_scope_version TEXT,
                    strategy_started_at TIMESTAMPTZ,
                    live_started_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ).format(_ident(_STATE_TABLE))
        )
        _add_columns(
            cur,
            _STATE_TABLE,
            {
                "start_bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "wins": "INTEGER NOT NULL DEFAULT 0",
                "losses": "INTEGER NOT NULL DEFAULT 0",
                "draws": "INTEGER NOT NULL DEFAULT 0",
                "trades_count": "INTEGER NOT NULL DEFAULT 0",
                "gross_profit": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "gross_loss": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "current_loss_streak": "INTEGER NOT NULL DEFAULT 0",
                "max_loss_streak": "INTEGER NOT NULL DEFAULT 0",
                "peak_bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "min_bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "max_drawdown": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "last_settled_epoch": "BIGINT",
                "breaker_loss_count": "INTEGER NOT NULL DEFAULT 0",
                "breaker_signals_remaining": "INTEGER NOT NULL DEFAULT 0",
                "breaker_trigger_count": "INTEGER NOT NULL DEFAULT 0",
                "retro_initialized": "BOOLEAN NOT NULL DEFAULT FALSE",
                "retro_cutoff_epoch": "BIGINT",
                "retro_trades_count": "INTEGER NOT NULL DEFAULT 0",
                "retro_wins": "INTEGER NOT NULL DEFAULT 0",
                "retro_losses": "INTEGER NOT NULL DEFAULT 0",
                "retro_pnl": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "retro_gross_profit": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "retro_gross_loss": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "retro_peak_bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "retro_min_bank": "DOUBLE PRECISION NOT NULL DEFAULT 500",
                "retro_max_drawdown": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "retro_scope_version": "TEXT",
                "strategy_started_at": "TIMESTAMPTZ",
                "live_started_at": "TIMESTAMPTZ",
                "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            },
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}(id,start_bank,bank,peak_bank,min_bank) VALUES(1,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING"
            ).format(_ident(_STATE_TABLE)),
            (SETTINGS.start_bank, SETTINGS.start_bank, SETTINGS.start_bank, SETTINGS.start_bank),
        )

        # Dedicated FUSION-222 decisions. Same rich shape as the main bot so
        # existing dashboard/reporting patterns remain easy to inspect.
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    betting_epoch BIGINT PRIMARY KEY,
                    live_epoch BIGINT,
                    locked_at_chain_timestamp BIGINT,
                    locked_at_seconds_to_lock INTEGER,
                    signal TEXT,
                    probability_up DOUBLE PRECISION,
                    probability_down DOUBLE PRECISION,
                    expected_coeff_up DOUBLE PRECISION,
                    expected_coeff_down DOUBLE PRECISION,
                    ev_up DOUBLE PRECISION,
                    ev_down DOUBLE PRECISION,
                    selected_ev DOUBLE PRECISION,
                    agreement DOUBLE PRECISION,
                    decision_quality TEXT,
                    stake DOUBLE PRECISION NOT NULL DEFAULT 0,
                    bank_before DOUBLE PRECISION,
                    components_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    weights_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    features_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    snapshot_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    settled BOOLEAN NOT NULL DEFAULT FALSE,
                    final_winner TEXT,
                    final_coeff_gross DOUBLE PRECISION,
                    final_coeff_net DOUBLE PRECISION,
                    final_move_points DOUBLE PRECISION,
                    outcome TEXT,
                    pnl DOUBLE PRECISION,
                    bank_after DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    settled_at TIMESTAMPTZ,
                    raw_expected_coeff_up DOUBLE PRECISION,
                    raw_expected_coeff_down DOUBLE PRECISION,
                    payout_correction_up DOUBLE PRECISION,
                    payout_correction_down DOUBLE PRECISION,
                    bank_before_settlement DOUBLE PRECISION,
                    final_coeff_up DOUBLE PRECISION,
                    final_coeff_down DOUBLE PRECISION,
                    actual_ev_signal DOUBLE PRECISION,
                    payout_ratio_signal DOUBLE PRECISION,
                    strategy_version TEXT,
                    payout_bucket_up TEXT,
                    payout_bucket_down TEXT,
                    trade_executed BOOLEAN NOT NULL DEFAULT FALSE,
                    no_trade_reason TEXT,
                    source_key TEXT,
                    selection_reason TEXT,
                    shadow_allowed BOOLEAN,
                    shadow_reason TEXT,
                    shadow_stats_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    shadow_pnl DOUBLE PRECISION,
                    stake_mode TEXT,
                    stake_tier TEXT,
                    breaker_applied BOOLEAN NOT NULL DEFAULT FALSE,
                    origin TEXT NOT NULL DEFAULT 'LIVE'
                )
                """
            ).format(_ident(_DECISIONS_TABLE))
        )
        _add_columns(
            cur,
            _DECISIONS_TABLE,
            {
                "live_epoch": "BIGINT",
                "locked_at_chain_timestamp": "BIGINT",
                "locked_at_seconds_to_lock": "INTEGER",
                "signal": "TEXT",
                "probability_up": "DOUBLE PRECISION",
                "probability_down": "DOUBLE PRECISION",
                "expected_coeff_up": "DOUBLE PRECISION",
                "expected_coeff_down": "DOUBLE PRECISION",
                "ev_up": "DOUBLE PRECISION",
                "ev_down": "DOUBLE PRECISION",
                "selected_ev": "DOUBLE PRECISION",
                "agreement": "DOUBLE PRECISION",
                "decision_quality": "TEXT",
                "stake": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "bank_before": "DOUBLE PRECISION",
                "components_json": "JSONB NOT NULL DEFAULT '[]'::jsonb",
                "weights_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
                "features_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
                "snapshot_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
                "settled": "BOOLEAN NOT NULL DEFAULT FALSE",
                "final_winner": "TEXT",
                "final_coeff_gross": "DOUBLE PRECISION",
                "final_coeff_net": "DOUBLE PRECISION",
                "final_move_points": "DOUBLE PRECISION",
                "outcome": "TEXT",
                "pnl": "DOUBLE PRECISION",
                "bank_after": "DOUBLE PRECISION",
                "created_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
                "settled_at": "TIMESTAMPTZ",
                "raw_expected_coeff_up": "DOUBLE PRECISION",
                "raw_expected_coeff_down": "DOUBLE PRECISION",
                "payout_correction_up": "DOUBLE PRECISION",
                "payout_correction_down": "DOUBLE PRECISION",
                "bank_before_settlement": "DOUBLE PRECISION",
                "final_coeff_up": "DOUBLE PRECISION",
                "final_coeff_down": "DOUBLE PRECISION",
                "actual_ev_signal": "DOUBLE PRECISION",
                "payout_ratio_signal": "DOUBLE PRECISION",
                "strategy_version": "TEXT",
                "payout_bucket_up": "TEXT",
                "payout_bucket_down": "TEXT",
                "trade_executed": "BOOLEAN NOT NULL DEFAULT FALSE",
                "no_trade_reason": "TEXT",
                "source_key": "TEXT",
                "selection_reason": "TEXT",
                "shadow_allowed": "BOOLEAN",
                "shadow_reason": "TEXT",
                "shadow_stats_json": "JSONB NOT NULL DEFAULT '{}'::jsonb",
                "shadow_pnl": "DOUBLE PRECISION",
                "stake_mode": "TEXT",
                "stake_tier": "TEXT",
                "breaker_applied": "BOOLEAN NOT NULL DEFAULT FALSE",
                "origin": "TEXT NOT NULL DEFAULT 'LIVE'",
            },
        )
        cur.execute(
            sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {}(betting_epoch)").format(
                _ident(f"{_DECISIONS_TABLE}_epoch_unique"), _ident(_DECISIONS_TABLE)
            )
        )

        # Shared round history. Only create it if the main database did not yet
        # have one; upserts are idempotent and useful to both services.
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    epoch BIGINT PRIMARY KEY,
                    start_timestamp BIGINT,
                    lock_timestamp BIGINT,
                    close_timestamp BIGINT,
                    lock_price DOUBLE PRECISION,
                    close_price DOUBLE PRECISION,
                    lock_oracle_id NUMERIC(78,0),
                    close_oracle_id NUMERIC(78,0),
                    total_amount_bnb DOUBLE PRECISION,
                    bull_amount_bnb DOUBLE PRECISION,
                    bear_amount_bnb DOUBLE PRECISION,
                    reward_base_bnb DOUBLE PRECISION,
                    reward_amount_bnb DOUBLE PRECISION,
                    oracle_called BOOLEAN,
                    actual_winner TEXT,
                    winner_coeff_gross DOUBLE PRECISION,
                    winner_coeff_net DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            ).format(_ident(_ROUNDS_TABLE))
        )

        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    betting_epoch BIGINT NOT NULL,
                    live_epoch BIGINT NOT NULL,
                    seconds_to_lock INTEGER NOT NULL,
                    bucket INTEGER NOT NULL,
                    chain_timestamp BIGINT NOT NULL,
                    chainlink_price DOUBLE PRECISION NOT NULL,
                    live_move_signed DOUBLE PRECISION,
                    bull_amount_bnb DOUBLE PRECISION,
                    bear_amount_bnb DOUBLE PRECISION,
                    snapshot_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(betting_epoch,bucket)
                )
                """
            ).format(_ident(_SNAPSHOTS_TABLE))
        )
        c.commit()

    if SETTINGS.retro_replay_enabled:
        initialize_retro_state()


def ping() -> bool:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            return bool(_first_scalar(cur.fetchone(), default=False))
    except Exception:
        return False


def get_state(for_update: bool = False, cursor=None) -> dict[str, Any]:
    if cursor is not None:
        cursor.execute(
            sql.SQL("SELECT * FROM {} WHERE id=1{}").format(
                _ident(_STATE_TABLE), sql.SQL(" FOR UPDATE" if for_update else "")
            )
        )
        row = cursor.fetchone()
        return dict(row) if row else {}
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql.SQL("SELECT * FROM {} WHERE id=1").format(_ident(_STATE_TABLE)))
        row = cur.fetchone()
        return dict(row) if row else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _selected_probability(row: dict[str, Any]) -> float:
    signal = str(row.get("signal") or "")
    value = row.get("probability_up") if signal == "UP" else row.get("probability_down")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _shadow_pass_without_wr(row: dict[str, Any]) -> bool:
    stats = _as_dict(row.get("shadow_stats_json"))
    recent = _as_dict(stats.get("recent"))
    quality = _as_dict(stats.get("quality"))
    settings = _as_dict(stats.get("settings"))

    recent_samples = int(recent.get("samples") or 0)
    recent_window = int(settings.get("recent_window") or SETTINGS.shadow_recent_window)
    recent_pnl = float(recent.get("pnl") or 0.0)
    recent_min = float(settings.get("recent_min_pnl") if settings.get("recent_min_pnl") is not None else SETTINGS.shadow_recent_min_pnl)
    if recent_samples >= recent_window and recent_pnl <= recent_min:
        return False

    quality_samples = int(quality.get("samples") or 0)
    quality_min_samples = int(settings.get("quality_min_samples") or SETTINGS.quality_min_samples)
    if quality_samples >= quality_min_samples:
        pf = quality.get("profit_factor")
        quality_pf = float(pf) if pf is not None else 0.0
        pf_min = float(settings.get("quality_min_profit_factor") if settings.get("quality_min_profit_factor") is not None else SETTINGS.quality_min_profit_factor)
        if quality_pf < pf_min:
            return False
    return True


def _payout_ready(row: dict[str, Any]) -> bool:
    features = _as_dict(row.get("features_json"))
    value = features.get("selected_payout_bucket_ready")
    return True if value is None else bool(value)


def _base_rows(cutoff_epoch: int | None = None, strategy_version: str | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if cutoff_epoch is not None:
        clauses.append("betting_epoch<=%s")
        params.append(int(cutoff_epoch))
    if strategy_version is not None:
        clauses.append("strategy_version=%s")
        params.append(str(strategy_version))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {}{} ORDER BY betting_epoch ASC").format(
                _ident(_BASE_DECISIONS_TABLE), sql.SQL(where)
            ),
            tuple(params),
        )
        return [dict(r) for r in cur.fetchall()]


def _retro_cutoff_epoch() -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT MAX(betting_epoch) FROM {} "
                "WHERE COALESCE(settled,FALSE)=TRUE AND final_winner IN ('UP','DOWN') AND strategy_version=%s"
            ).format(_ident(_BASE_DECISIONS_TABLE)),
            (SETTINGS.retro_scope_version,),
        )
        return int(_first_scalar(cur.fetchone(), default=0) or 0)


def _retro_replay(cutoff_epoch: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # IMPORTANT: dashboard history is reconstructed ONLY from the historical
    # v1.3.6.6 slice (or RETRO_SCOPE_VERSION), never from ALL_VERSIONS.
    rows = _base_rows(cutoff_epoch, SETTINGS.retro_scope_version)
    candidates: list[dict[str, Any]] = []

    for row in rows:
        if not bool(row.get("settled")) or row.get("final_winner") not in {"UP", "DOWN"}:
            continue
        try:
            ev = float(row.get("selected_ev"))
        except (TypeError, ValueError):
            continue
        prob = _selected_probability(row)
        if ev < SETTINGS.min_trade_ev or prob < SETTINGS.min_signal_probability:
            continue

        version = str(row.get("strategy_version") or "unknown")
        if version != SETTINGS.retro_scope_version:
            continue
        if SETTINGS.require_payout_bucket_ready and not _payout_ready(row):
            continue
        if not _shadow_pass_without_wr(row):
            continue
        # Old cooldown and old Quality-WR blocks are intentionally ignored.
        candidates.append(row)

    selected: list[dict[str, Any]] = []
    current_loss_streak = 0
    max_loss_streak = 0

    bank = float(SETTINGS.start_bank)
    peak = bank
    min_bank = bank
    max_drawdown = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    losses = 0

    for row in candidates:
        version = SETTINGS.retro_scope_version

        signal = str(row.get("signal") or "")
        winner = str(row.get("final_winner") or "")
        outcome = "WIN" if signal == winner else "LOSS"
        if outcome == "LOSS":
            pnl = -float(SETTINGS.fixed_stake)
        else:
            coeff = row.get("final_coeff_up") if signal == "UP" else row.get("final_coeff_down")
            try:
                pnl = float(SETTINGS.fixed_stake) * (float(coeff) - 1.0)
            except (TypeError, ValueError):
                continue

        item = dict(row)
        item.update(
            {
                "origin": "RETRO",
                "retro_source_version": version,
                "trade_executed": True,
                "outcome": outcome,
                "stake": float(SETTINGS.fixed_stake),
                "projected_stake": float(SETTINGS.fixed_stake),
                "pnl": pnl,
                "projected_pnl": pnl,
                "stake_mode": "fixed_22",
                "stake_tier": "FIXED_22",
                "decision_quality": "FUSION_222_RETRO_REPLAY",
            }
        )

        bank += pnl
        peak = max(peak, bank)
        min_bank = min(min_bank, bank)
        max_drawdown = max(max_drawdown, peak - bank)
        item["bank_after"] = bank
        item["version_bank_after"] = bank
        selected.append(item)

        if pnl > 0:
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += -pnl

        if outcome == "WIN":
            wins += 1
            current_loss_streak = 0
        else:
            losses += 1
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)

    anchor_started_at = rows[0].get("created_at") if rows else None

    metrics = {
        "start_bank": float(SETTINGS.start_bank),
        "bank": bank,
        "pnl": bank - float(SETTINGS.start_bank),
        "wins": wins,
        "losses": losses,
        "draws": 0,
        "trades_count": wins + losses,
        "win_rate": wins / (wins + losses) if wins + losses else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "current_loss_streak": current_loss_streak,
        "max_loss_streak": max_loss_streak,
        "peak_bank": peak,
        "min_bank": min_bank,
        "max_drawdown": max_drawdown,
        "breaker_loss_count": 0,
        "breaker_signals_remaining": 0,
        "breaker_trigger_count": 0,
        "retro_cutoff_epoch": int(cutoff_epoch),
        "retro_scope_version": SETTINGS.retro_scope_version,
        "last_settled_epoch": selected[-1].get("betting_epoch") if selected else None,
        "strategy_started_at": anchor_started_at,
        "first_retro_trade_epoch": selected[0].get("betting_epoch") if selected else None,
        "last_retro_trade_epoch": selected[-1].get("betting_epoch") if selected else None,
    }
    return selected, metrics


def retro_replay(force: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = get_state()
    cutoff = state.get("retro_cutoff_epoch")
    if cutoff is None:
        cutoff = _retro_cutoff_epoch()
    cutoff = int(cutoff or 0)
    now = time.time()
    with _RETRO_CACHE_LOCK:
        if not force and _RETRO_CACHE.get("cutoff") == cutoff and now - float(_RETRO_CACHE.get("at") or 0) < 300:
            return list(_RETRO_CACHE["rows"]), dict(_RETRO_CACHE["metrics"])
    rows, metrics = _retro_replay(cutoff)
    with _RETRO_CACHE_LOCK:
        _RETRO_CACHE.update({"cutoff": cutoff, "at": now, "rows": rows, "metrics": metrics})
    return list(rows), dict(metrics)


def initialize_retro_state() -> None:
    with _LOCK, conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        state = get_state(for_update=True, cursor=cur)
        scope_ok = str(state.get("retro_scope_version") or "") == SETTINGS.retro_scope_version
        if bool(state.get("retro_initialized")) and scope_ok:
            c.commit()
            return
        # Corrected FUSION-222 uses dedicated v1.3.6.6 private tables. If an
        # operator intentionally points this build at a pre-existing state table
        # with a different retro scope, only auto-reinitialize when no settled
        # live trades exist; otherwise fail loudly rather than silently mixing
        # incompatible accounting histories.
        cur.execute(
            sql.SQL("SELECT COUNT(*) AS live_trades FROM {} WHERE COALESCE(settled,FALSE)=TRUE AND COALESCE(trade_executed,FALSE)=TRUE").format(_ident(_DECISIONS_TABLE))
        )
        live_trades = int(_first_scalar(cur.fetchone(), key="live_trades", default=0) or 0)
        if bool(state.get("retro_initialized")) and not scope_ok and live_trades > 0:
            c.rollback()
            raise RuntimeError(
                "Existing FUSION-222 state was initialized with a different retro scope and already has live trades. "
                "Use the v1.3.6.6-specific BOT_* table names from the corrected Railway variables."
            )
        cutoff = _retro_cutoff_epoch()
        c.commit()

    rows, metrics = _retro_replay(cutoff)
    with _RETRO_CACHE_LOCK:
        _RETRO_CACHE.update({"cutoff": cutoff, "at": time.time(), "rows": rows, "metrics": metrics})

    with _LOCK, conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        state = get_state(for_update=True, cursor=cur)
        scope_ok = str(state.get("retro_scope_version") or "") == SETTINGS.retro_scope_version
        if bool(state.get("retro_initialized")) and scope_ok:
            c.commit()
            return
        cur.execute(
            sql.SQL(
                """
                UPDATE {} SET
                    start_bank=%s,bank=%s,wins=%s,losses=%s,draws=0,trades_count=%s,
                    gross_profit=%s,gross_loss=%s,current_loss_streak=%s,max_loss_streak=%s,
                    peak_bank=%s,min_bank=%s,max_drawdown=%s,last_settled_epoch=%s,
                    breaker_loss_count=%s,breaker_signals_remaining=%s,breaker_trigger_count=%s,
                    retro_initialized=TRUE,retro_cutoff_epoch=%s,retro_trades_count=%s,
                    retro_wins=%s,retro_losses=%s,retro_pnl=%s,retro_gross_profit=%s,
                    retro_gross_loss=%s,retro_peak_bank=%s,retro_min_bank=%s,
                    retro_max_drawdown=%s,retro_scope_version=%s,strategy_started_at=%s,live_started_at=NOW(),updated_at=NOW()
                WHERE id=1
                """
            ).format(_ident(_STATE_TABLE)),
            (
                SETTINGS.start_bank,
                metrics["bank"], metrics["wins"], metrics["losses"], metrics["trades_count"],
                metrics["gross_profit"], metrics["gross_loss"], metrics["current_loss_streak"], metrics["max_loss_streak"],
                metrics["peak_bank"], metrics["min_bank"], metrics["max_drawdown"], metrics["last_settled_epoch"],
                metrics["breaker_loss_count"], metrics["breaker_signals_remaining"], metrics["breaker_trigger_count"],
                cutoff, metrics["trades_count"], metrics["wins"], metrics["losses"], metrics["pnl"],
                metrics["gross_profit"], metrics["gross_loss"], metrics["peak_bank"], metrics["min_bank"], metrics["max_drawdown"],
                SETTINGS.retro_scope_version, metrics.get("strategy_started_at"),
            ),
        )
        c.commit()


def get_decision(betting_epoch: int) -> dict[str, Any] | None:
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE betting_epoch=%s LIMIT 1").format(_ident(_DECISIONS_TABLE)),
            (int(betting_epoch),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def insert_decision(data: dict[str, Any]) -> dict[str, Any]:
    columns = [
        "betting_epoch", "live_epoch", "locked_at_chain_timestamp", "locked_at_seconds_to_lock",
        "signal", "probability_up", "probability_down", "expected_coeff_up", "expected_coeff_down",
        "ev_up", "ev_down", "selected_ev", "agreement", "decision_quality", "stake", "bank_before",
        "components_json", "weights_json", "features_json", "snapshot_json", "raw_expected_coeff_up",
        "raw_expected_coeff_down", "payout_correction_up", "payout_correction_down", "strategy_version",
        "payout_bucket_up", "payout_bucket_down", "trade_executed", "no_trade_reason", "source_key",
        "selection_reason", "shadow_allowed", "shadow_reason", "shadow_stats_json", "stake_mode",
        "stake_tier", "breaker_applied", "origin",
    ]
    json_cols = {"components_json", "weights_json", "features_json", "snapshot_json", "shadow_stats_json"}
    values = []
    for col in columns:
        value = data.get(col)
        if col in json_cols:
            value = Json(value if value is not None else ([] if col == "components_json" else {}))
        values.append(value)
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(betting_epoch) DO NOTHING RETURNING *").format(
                _ident(_DECISIONS_TABLE),
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
            ),
            values,
        )
        row = cur.fetchone()
        c.commit()
    return dict(row) if row else (get_decision(int(data["betting_epoch"])) or {})


def unsettled_decisions(limit: int = 100) -> list[dict[str, Any]]:
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE COALESCE(settled,FALSE)=FALSE ORDER BY betting_epoch ASC LIMIT %s").format(_ident(_DECISIONS_TABLE)),
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]


def settle_decision_atomic(
    epoch: int,
    *,
    final_winner: str,
    final_coeff_gross: float | None,
    final_coeff_net: float | None,
    final_coeff_up: float | None,
    final_coeff_down: float | None,
    final_move_points: float | None,
    outcome: str,
    pnl: float,
    shadow_pnl: float,
    actual_ev_signal: float | None,
    payout_ratio_signal: float | None,
) -> bool:
    with _LOCK, conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE betting_epoch=%s FOR UPDATE").format(_ident(_DECISIONS_TABLE)),
            (int(epoch),),
        )
        decision = cur.fetchone()
        if not decision or decision.get("settled"):
            c.rollback()
            return False
        state = get_state(for_update=True, cursor=cur)
        bank_before = float(state.get("bank") or SETTINGS.start_bank)
        trade_executed = bool(decision.get("trade_executed"))
        realized = float(pnl) if trade_executed else 0.0
        bank = bank_before + realized

        wins = int(state.get("wins") or 0)
        losses = int(state.get("losses") or 0)
        draws = int(state.get("draws") or 0)
        trades = int(state.get("trades_count") or 0)
        gp = float(state.get("gross_profit") or 0.0)
        gl = float(state.get("gross_loss") or 0.0)
        current_loss = int(state.get("current_loss_streak") or 0)
        max_loss = int(state.get("max_loss_streak") or 0)
        # Legacy columns remain in the state schema, but the circuit breaker is disabled.
        breaker_losses = 0
        breaker_remaining = 0
        breaker_triggers = 0

        if trade_executed:
            trades += 1
            if realized > 0:
                gp += realized
            elif realized < 0:
                gl += -realized
            if outcome == "WIN":
                wins += 1
                current_loss = 0
            elif outcome == "LOSS":
                losses += 1
                current_loss += 1
                max_loss = max(max_loss, current_loss)
            else:
                draws += 1
                current_loss = 0

        peak = max(float(state.get("peak_bank") or bank_before), bank)
        min_bank = min(float(state.get("min_bank") or bank_before), bank)
        max_dd = max(float(state.get("max_drawdown") or 0.0), peak - bank)

        cur.execute(
            sql.SQL(
                """
                UPDATE {} SET bank=%s,wins=%s,losses=%s,draws=%s,trades_count=%s,
                    gross_profit=%s,gross_loss=%s,current_loss_streak=%s,max_loss_streak=%s,
                    peak_bank=%s,min_bank=%s,max_drawdown=%s,breaker_loss_count=%s,
                    breaker_signals_remaining=%s,breaker_trigger_count=%s,
                    last_settled_epoch=GREATEST(COALESCE(last_settled_epoch,0),%s),updated_at=NOW()
                WHERE id=1
                """
            ).format(_ident(_STATE_TABLE)),
            (
                bank, wins, losses, draws, trades, gp, gl, current_loss, max_loss,
                peak, min_bank, max_dd, breaker_losses, breaker_remaining, breaker_triggers, int(epoch),
            ),
        )
        cur.execute(
            sql.SQL(
                """
                UPDATE {} SET settled=TRUE,final_winner=%s,final_coeff_gross=%s,final_coeff_net=%s,
                    final_coeff_up=%s,final_coeff_down=%s,final_move_points=%s,outcome=%s,pnl=%s,
                    shadow_pnl=%s,bank_before_settlement=%s,bank_after=%s,actual_ev_signal=%s,
                    payout_ratio_signal=%s,settled_at=NOW(),updated_at=NOW()
                WHERE betting_epoch=%s AND COALESCE(settled,FALSE)=FALSE
                """
            ).format(_ident(_DECISIONS_TABLE)),
            (
                final_winner, final_coeff_gross, final_coeff_net, final_coeff_up, final_coeff_down,
                final_move_points, outcome, realized, float(shadow_pnl), bank_before, bank,
                actual_ev_signal, payout_ratio_signal, int(epoch),
            ),
        )
        changed = cur.rowcount == 1
        c.commit()
        return changed


def upsert_round(data: dict[str, Any]) -> None:
    cols = [
        "epoch", "start_timestamp", "lock_timestamp", "close_timestamp", "lock_price", "close_price",
        "lock_oracle_id", "close_oracle_id", "total_amount_bnb", "bull_amount_bnb", "bear_amount_bnb",
        "reward_base_bnb", "reward_amount_bnb", "oracle_called", "actual_winner", "winner_coeff_gross",
        "winner_coeff_net",
    ]
    values = [data.get(c) for c in cols]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(epoch) DO UPDATE SET {}").format(
                _ident(_ROUNDS_TABLE),
                sql.SQL(",").join(map(sql.Identifier, cols)),
                sql.SQL(",").join(sql.Placeholder() for _ in cols),
                sql.SQL(",").join(
                    sql.SQL("{}=EXCLUDED.{}").format(_ident(x), _ident(x)) for x in cols if x != "epoch"
                ) + sql.SQL(",updated_at=NOW()"),
            ),
            values,
        )
        c.commit()


def recent_rounds(limit: int = 1200) -> list[dict[str, Any]]:
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE actual_winner IN ('UP','DOWN') ORDER BY epoch DESC LIMIT %s").format(_ident(_ROUNDS_TABLE)),
            (int(limit),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows


def save_snapshot(data: dict[str, Any]) -> bool:
    bucket = int(data["seconds_to_lock"]) // max(1, SETTINGS.snapshot_bucket_seconds)
    with conn() as c, c.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {}(betting_epoch,live_epoch,seconds_to_lock,bucket,chain_timestamp,
                    chainlink_price,live_move_signed,bull_amount_bnb,bear_amount_bnb,snapshot_json)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(betting_epoch,bucket) DO NOTHING
                """
            ).format(_ident(_SNAPSHOTS_TABLE)),
            (
                int(data["betting_epoch"]), int(data["live_epoch"]), int(data["seconds_to_lock"]), bucket,
                int(data["chain_timestamp"]), float(data["chainlink_price"]), float(data["live_move_signed"]),
                float(data.get("bull_amount_bnb") or 0), float(data.get("bear_amount_bnb") or 0), Json(data),
            ),
        )
        changed = cur.rowcount == 1
        c.commit()
        return changed


def snapshots_for_epoch(betting_epoch: int, limit: int = 30) -> list[dict[str, Any]]:
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE betting_epoch=%s ORDER BY seconds_to_lock DESC LIMIT %s").format(_ident(_SNAPSHOTS_TABLE)),
            (int(betting_epoch), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]


def history(limit: int = 100, offset: int = 0, *, settled_only: bool = False, trades_only: bool = False) -> list[dict[str, Any]]:
    clauses = []
    if settled_only:
        clauses.append("COALESCE(settled,FALSE)=TRUE")
    if trades_only:
        clauses.append("COALESCE(trade_executed,FALSE)=TRUE")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    safe_limit = max(1, min(int(limit), SETTINGS.history_api_max_limit))
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {}{} ORDER BY betting_epoch DESC LIMIT %s OFFSET %s").format(
                _ident(_DECISIONS_TABLE), sql.SQL(where)
            ),
            (safe_limit, max(0, int(offset))),
        )
        return [dict(r) for r in cur.fetchall()]


def history_count(*, settled_only: bool = False, trades_only: bool = False) -> int:
    clauses = []
    if settled_only:
        clauses.append("COALESCE(settled,FALSE)=TRUE")
    if trades_only:
        clauses.append("COALESCE(trade_executed,FALSE)=TRUE")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with conn() as c, c.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}{}").format(_ident(_DECISIONS_TABLE), sql.SQL(where)))
        return int(_first_scalar(cur.fetchone(), default=0) or 0)


def retro_history(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    rows, _ = retro_replay()
    rows = list(reversed(rows))
    return rows[max(0, int(offset)): max(0, int(offset)) + max(1, int(limit))]


def retro_history_count() -> int:
    rows, _ = retro_replay()
    return len(rows)


def combined_trade_history(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    retro, _ = retro_replay()
    live = history(SETTINGS.history_api_max_limit, 0, settled_only=True, trades_only=True)
    combined = [dict(r) for r in retro] + [dict(r) for r in live]
    combined.sort(key=lambda r: int(r.get("betting_epoch") or 0), reverse=True)
    start = max(0, int(offset))
    return combined[start:start + max(1, min(int(limit), SETTINGS.history_api_max_limit))]


def combined_trade_count() -> int:
    return retro_history_count() + history_count(settled_only=True, trades_only=True)


def _source_cutoff() -> int:
    state = get_state()
    return int(state.get("retro_cutoff_epoch") or 0)


def shadow_rows(source_key: str, signal: str | None, lookback: int) -> list[dict[str, Any]]:
    cutoff = _source_cutoff()
    clauses = ["COALESCE(settled,FALSE)=TRUE", "final_winner IN ('UP','DOWN')", "source_key=%s"]
    params: list[Any] = [source_key]
    if signal:
        clauses.append("signal=%s")
        params.append(signal)
    where = " AND ".join(clauses)
    with conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT betting_epoch,signal,final_winner,final_coeff_up,final_coeff_down,shadow_pnl
                FROM (
                    SELECT betting_epoch,signal,final_winner,final_coeff_up,final_coeff_down,shadow_pnl,source_key,settled
                    FROM {} WHERE betting_epoch<=%s
                    UNION ALL
                    SELECT betting_epoch,signal,final_winner,final_coeff_up,final_coeff_down,shadow_pnl,source_key,settled
                    FROM {}
                ) q
                WHERE {} ORDER BY betting_epoch DESC LIMIT %s
                """
            ).format(_ident(_BASE_DECISIONS_TABLE), _ident(_DECISIONS_TABLE), sql.SQL(where)),
            (cutoff, *params, int(lookback)),
        )
        return [dict(r) for r in cur.fetchall()]


def payout_ratios(side: str, bucket: str, lookback: int) -> list[float]:
    raw_col = "raw_expected_coeff_up" if side == "UP" else "raw_expected_coeff_down"
    final_col = "final_coeff_up" if side == "UP" else "final_coeff_down"
    bucket_col = "payout_bucket_up" if side == "UP" else "payout_bucket_down"
    cutoff = _source_cutoff()
    with conn() as c, c.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT final_value,raw_value FROM (
                    SELECT betting_epoch,{final} AS final_value,{raw} AS raw_value,{bucket} AS bucket_value,settled
                    FROM {base} WHERE betting_epoch<=%s
                    UNION ALL
                    SELECT betting_epoch,{final} AS final_value,{raw} AS raw_value,{bucket} AS bucket_value,settled
                    FROM {bot}
                ) q
                WHERE COALESCE(settled,FALSE)=TRUE AND bucket_value=%s
                    AND final_value>0 AND raw_value>0
                ORDER BY betting_epoch DESC LIMIT %s
                """
            ).format(
                final=_ident(final_col), raw=_ident(raw_col), bucket=_ident(bucket_col),
                base=_ident(_BASE_DECISIONS_TABLE), bot=_ident(_DECISIONS_TABLE),
            ),
            (cutoff, bucket, int(lookback)),
        )
        ratios: list[float] = []
        for final_value, raw_value in cur.fetchall():
            try:
                ratio = float(final_value) / float(raw_value)
                if math.isfinite(ratio) and ratio > 0:
                    ratios.append(ratio)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return ratios


def state_metrics() -> dict[str, Any]:
    state = get_state()
    wins = int(state.get("wins") or 0)
    losses = int(state.get("losses") or 0)
    gp = float(state.get("gross_profit") or 0.0)
    gl = float(state.get("gross_loss") or 0.0)
    bank = float(state.get("bank") or SETTINGS.start_bank)
    start_bank = float(state.get("start_bank") or SETTINGS.start_bank)
    result = dict(state)
    result.update(
        {
            "pnl": bank - start_bank,
            "win_rate": wins / (wins + losses) if wins + losses else 0.0,
            "profit_factor": gp / gl if gl > 0 else None,
            "fixed_stake": float(SETTINGS.fixed_stake),
            "min_trade_ev": float(SETTINGS.min_trade_ev),
            "min_signal_probability": float(SETTINGS.min_signal_probability),
        }
    )
    return result


def table_names() -> dict[str, str]:
    return {
        "base_decisions_read_only": _BASE_DECISIONS_TABLE,
        "decisions": _DECISIONS_TABLE,
        "state": _STATE_TABLE,
        "rounds_shared": _ROUNDS_TABLE,
        "snapshots": _SNAPSHOTS_TABLE,
    }
