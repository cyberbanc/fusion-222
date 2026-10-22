from __future__ import annotations

import asyncio
import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import db
from .config import SETTINGS
from .stake_policy import POLICY, stake_quote
import threading
import time
from .live_execution import from_env as live_executor_from_env
from .pancake_client import from_env
from .shadow import summarize
from .worker import bootstrap_rounds, loop, signal_cache, status as worker_status

_STOP: Optional[asyncio.Event] = None
_TASK: Optional[asyncio.Task] = None
_BUILD_REVISION = "fusion-222-only-up-real-v2-tiers-22-44-88"
_DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "TILDA_FUSION_222_REAL.html"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _STOP, _TASK
    db.init_db()
    try:
        await asyncio.wait_for(asyncio.to_thread(bootstrap_rounds, from_env()), timeout=45)
    except Exception:
        pass
    if SETTINGS.worker_enabled:
        _STOP = asyncio.Event()
        _TASK = asyncio.create_task(loop(_STOP))
    yield
    if _STOP:
        _STOP.set()
    if _TASK:
        try:
            await asyncio.wait_for(_TASK, timeout=8)
        except Exception:
            _TASK.cancel()


# Refuse ambiguous or colliding write targets before any worker can start.
if SETTINGS.execution_mode != "REAL" or not SETTINGS.mirror_paper_decisions:
    raise RuntimeError("This deployment requires REAL mode and the ONLY UP paper mirror")
if SETTINGS.reference_decisions_table != "fusion222_uponly_decisions":
    raise RuntimeError("ONLY UP reference table is required")
for table in (SETTINGS.bot_decisions_table, SETTINGS.bot_state_table, SETTINGS.bot_snapshots_table, SETTINGS.transactions_table, SETTINGS.wallet_snapshots_table, SETTINGS.base_rounds_table):
    if not table.startswith("fusion222_uponly_real_"):
        raise RuntimeError("Dedicated ONLY UP real write tables are required")

app = FastAPI(title="FUSION-222 ONLY UP Real Bot", version=SETTINGS.version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": SETTINGS.service_name,
        "version": SETTINGS.version,
        "build_revision": _BUILD_REVISION,
        "mode": "REAL",
        "strategy": "EV>=2%, selected probability>=53%, payout ready, Shadow Recent, Quality PF>=0.85, Quality WR display-only, ONLY UP; USD stake tiers 22/44/88 from liquid BNB; circuit breaker disabled",
        "stake_mode": POLICY["mode"],
        "stake_policy": POLICY,
        "fixed_stake": None,
        "database_mode": "shared Fusion PostgreSQL; paper/base history read-only; real bot writes only fusion222_uponly_real_* tables",
        "wallet_accounting": "dashboard bank is the current on-chain BNB balance converted at the current Chainlink BNB/USD price",
        "signal_url": "/signal",
        "status_url": "/status?history=none",
        "real_history_url": "/history/live?limit=100&trades_only=true",
        "transactions_url": "/transactions?limit=100",
        "real_csv_url": "/history/export-real.csv",
        "dashboard_url": "/dashboard",
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": SETTINGS.service_name, "version": SETTINGS.version, "build_revision": _BUILD_REVISION}


@app.get("/health")
def health():
    client = from_env()
    return {
        "ok": True,
        "service": SETTINGS.service_name,
        "version": SETTINGS.version,
        "build_revision": _BUILD_REVISION,
        "connected": client.is_connected(),
        "database_connected": db.ping(),
        "worker": worker_status(),
        "execution": live_executor_from_env(client).status(),
        "tables": db.table_names(),
        "fixed_stake": None,
        "min_trade_ev": SETTINGS.min_trade_ev,
        "min_signal_probability": SETTINGS.min_signal_probability,
        "quality_win_rate_filter_enabled": SETTINGS.quality_win_rate_filter_enabled,
        "quality_min_profit_factor": SETTINGS.quality_min_profit_factor,
    }


@app.get("/signal")
def signal():
    cache = signal_cache()
    worker_tick = cache.get("worker_tick") or {}
    snapshot = cache.get("snapshot") or {}
    decision = cache.get("decision")
    epoch = worker_tick.get("betting_epoch") or snapshot.get("betting_epoch")
    if decision is None and epoch is not None and db.enabled():
        try:
            decision = db.get_decision(int(epoch))
        except Exception:
            decision = None
    if decision:
        return _json_safe(
            {
                "ok": True,
                "status": "LOCKED",
                "decision_locked": True,
                **decision,
                "snapshot": snapshot or decision.get("snapshot_json") or {},
                "betting_epoch": decision.get("betting_epoch", epoch),
                "live_epoch": worker_tick.get("live_epoch", decision.get("live_epoch")),
                "seconds_to_lock": worker_tick.get("seconds_to_lock", (snapshot or decision.get("snapshot_json") or {}).get("seconds_to_lock")),
                "decision_window": worker_tick.get("decision_window"),
                "worker_tick": worker_tick,
                "cache_updated_at": cache.get("updated_at"),
            }
        )
    return _json_safe(
        {
            "ok": bool(cache.get("ok", True)),
            "status": "WAIT",
            "decision_locked": False,
            "signal": None,
            "trade_executed": None,
            "stake": 0.0,
            "betting_epoch": epoch,
            "live_epoch": worker_tick.get("live_epoch") or snapshot.get("live_epoch"),
            "seconds_to_lock": worker_tick.get("seconds_to_lock") or snapshot.get("seconds_to_lock"),
            "decision_window": worker_tick.get("decision_window") or snapshot.get("decision_window"),
            "snapshot": snapshot,
            "worker_tick": worker_tick,
            "error": cache.get("error"),
            "cache_updated_at": cache.get("updated_at"),
        }
    )


def _uptime(started_at: Any) -> int:
    if not started_at:
        return 0
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except Exception:
            return 0
    if isinstance(started_at, datetime):
        dt = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    return 0


@app.get("/status")
def status(
    history: str = Query("none", pattern="^(recent|all|none)$"),
    limit: int = Query(30, ge=1),
    offset: int = Query(0, ge=0),
):
    metrics = db.state_metrics()
    live_count = db.history_count()
    rows: list[dict[str, Any]] = []
    if history == "recent":
        rows = db.history(min(limit, SETTINGS.history_api_max_limit), offset)
    elif history == "all":
        rows = db.history(SETTINGS.history_api_max_limit, offset)
    started_at = metrics.get("live_started_at") or metrics.get("created_at")
    live_started_at = metrics.get("live_started_at")
    worker = worker_status()
    wallet = (worker.get("last_tick") or {}).get("wallet") or db.latest_wallet_snapshot() or {}
    execution = worker.get("execution") or live_executor_from_env().status()
    actual_metrics = db.actual_trade_metrics()
    wallet_accounting = db.wallet_summary()
    return _json_safe(
        {
            "ok": True,
            "service": SETTINGS.service_name,
            "version": SETTINGS.version,
            "build_revision": _BUILD_REVISION,
            "mode": "REAL",
            "strategy_state": metrics,
            "wallet": wallet,
            "wallet_accounting": wallet_accounting,
            "actual_metrics": actual_metrics,
            "execution": execution,
            "worker": worker,
            "version_started_at": started_at,
            "uptime_seconds": _uptime(started_at),
            "live_started_at": live_started_at,
            "live_uptime_seconds": _uptime(live_started_at),
            "signal_reference": {
                "source": "current FUSION-222 ONLY UP paper history (read-only)",
                "base_decisions_table": db.table_names().get("base_decisions_read_only"),
                "paper_reference_table": db.table_names().get("paper_reference_read_only"),
                "base_cutoff_epoch": SETTINGS.base_history_cutoff_epoch,
                "fixed_stake": None,
                "min_trade_ev": SETTINGS.min_trade_ev,
                "min_signal_probability": SETTINGS.min_signal_probability,
                "quality_win_rate_is_gate": SETTINGS.quality_win_rate_filter_enabled,
                "quality_pf_min": SETTINGS.quality_min_profit_factor,
            },
            "history_storage": "separate fusion222_uponly_real_* tables; paper tables are read-only",
            "live_decision_count": live_count,
            "real_trade_count": db.history_count(trades_only=True),
            "transaction_count": db.transaction_count(),
            "history": rows,
            "history_download_csv": "/history/export-real.csv",
            "transactions_download_csv": "/transactions/export.csv",
        }
    )


@app.get("/history/live")
def history_live(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0), trades_only: bool = False):
    rows = db.history(limit, offset, trades_only=trades_only)
    return _json_safe({"ok": True, "count": db.history_count(trades_only=trades_only), "history": rows})


@app.get("/history/retro")
def history_retro(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0)):
    return {"ok": True, "count": 0, "history": [], "message": "REAL service has no retro trade history"}


@app.get("/history/combined")
def history_combined(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0)):
    rows = db.history(limit, offset, trades_only=True)
    return _json_safe({"ok": True, "count": db.history_count(trades_only=True), "history": rows})


@app.get("/transactions")
def transactions(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0)):
    return _json_safe({"ok": True, "count": db.transaction_count(), "transactions": db.transactions(limit, offset)})


def _csv_response(rows: list[dict[str, Any]], filename: str) -> Response:
    output = io.StringIO()
    if not rows:
        output.write("betting_epoch\n")
    else:
        columns: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _json_safe(v) for k, v in row.items()})
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/history/export-combined.csv")
def export_combined():
    return _csv_response(db.history(SETTINGS.history_api_max_limit, 0), "fusion_222_real_history.csv")


@app.get("/history/export-retro.csv")
def export_retro():
    return _csv_response([], "fusion_222_real_history_RETRO_NOT_USED.csv")


@app.get("/history/export-live.csv")
def export_live():
    return _csv_response(db.history(SETTINGS.history_api_max_limit, 0), "fusion_222_history_LIVE.csv")


@app.get("/history/export-real.csv")
def export_real():
    return _csv_response(
        db.history(SETTINGS.history_api_max_limit, 0),
        "fusion_222_real_history.csv",
    )


@app.get("/transactions/export.csv")
def export_transactions():
    return _csv_response(
        db.transactions(SETTINGS.history_api_max_limit, 0),
        "fusion_222_real_transactions.csv",
    )


@app.get("/shadow/performance")
def shadow_performance():
    source_keys = ["EV_PRIMARY", "CROWD_BINANCE_FALLBACK", "PROBABILITY_FALLBACK"]
    result: dict[str, Any] = {}
    for source in source_keys:
        source_rows = db.shadow_rows(source, None, SETTINGS.shadow_source_lookback)
        result[source] = {
            "overall": summarize(source_rows).to_dict(),
            "UP": summarize(db.shadow_rows(source, "UP", SETTINGS.shadow_side_lookback)).to_dict(),
            "DOWN": summarize(db.shadow_rows(source, "DOWN", SETTINGS.shadow_side_lookback)).to_dict(),
        }
    return _json_safe(
        {
            "ok": True,
            "filter_settings": {
                "recent_window": SETTINGS.shadow_recent_window,
                "recent_min_pnl": SETTINGS.shadow_recent_min_pnl,
                "quality_window": SETTINGS.quality_window,
                "quality_min_samples": SETTINGS.quality_min_samples,
                "quality_min_win_rate": SETTINGS.quality_min_win_rate,
                "quality_win_rate_filter_enabled": SETTINGS.quality_win_rate_filter_enabled,
                "quality_min_profit_factor": SETTINGS.quality_min_profit_factor,
            },
            "sources": result,
        }
    )


_PREFLIGHT_LOCK = threading.Lock()
_PREFLIGHT_CACHE: dict[str, Any] = {}
_PREFLIGHT_AT = 0.0


@app.get("/preflight")
def preflight():
    """Read-only chain inspection; never creates a bet or claim.

    Native BNB only. The bank excludes locked stakes and unclaimed payouts.
    The final executor performs fresh balance, price, round and gas checks.
    """
    global _PREFLIGHT_CACHE, _PREFLIGHT_AT
    with _PREFLIGHT_LOCK:
        if _PREFLIGHT_CACHE and time.monotonic() - _PREFLIGHT_AT < 10:
            return _PREFLIGHT_CACHE
        try:
            client = from_env()
            snap = client.market_snapshot()
            executor = live_executor_from_env(client)
            wallet = executor.wallet_snapshot(snap.chainlink_price, source="READ_ONLY_MONITOR", force=True)
            if "balance_bnb" not in wallet:
                raise RuntimeError("Wallet address or balance is unavailable")
            quote = stake_quote(int(Decimal(str(wallet["balance_bnb"])) * Decimal(10**18)), snap.chainlink_price)
            reference = db.get_reference_decision(snap.betting_epoch)
            paused, minimum = client._call(lambda w3: (bool(client._prediction(w3).functions.paused().call()), int(client._prediction(w3).functions.minBetAmount().call())))
            fresh = 0 <= snap.chain_timestamp - snap.oracle_updated_at <= SETTINGS.oracle_max_age_seconds
            result = {
                "ok": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                "chain_timestamp": snap.chain_timestamp, "oracle_updated_at": snap.oracle_updated_at,
                "oracle_fresh": fresh, "wallet": wallet, "quote": quote,
                "stake_policy": POLICY, "execution": executor.status(),
                "contract_paused": paused, "contract_min_bet_bnb": minimum / 1e18,
                "funds_above_stake_and_reserve": float(wallet["balance_bnb"]) >= quote["stake_bnb"] + SETTINGS.min_gas_reserve_bnb,
                "affordability_is_preliminary": True,
                "round": {"epoch": snap.betting_epoch, "lock_timestamp": snap.betting_round.lock_timestamp, "seconds_to_lock": snap.seconds_to_lock, "decision_window": snap.decision_window},
                "paper_reference": {"available": bool(reference), "signal": (reference or {}).get("signal"), "eligible": bool((reference or {}).get("trade_executed")) and (reference or {}).get("signal") == "UP", "probability_up": (reference or {}).get("probability_up"), "ev_up": (reference or {}).get("ev_up"), "reason": (reference or {}).get("no_trade_reason")},
            }
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "execution": live_executor_from_env().status()}
        _PREFLIGHT_CACHE = _json_safe(result)
        _PREFLIGHT_AT = time.monotonic()
        return _PREFLIGHT_CACHE
