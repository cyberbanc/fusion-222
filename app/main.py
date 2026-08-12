from __future__ import annotations

import asyncio
import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .config import SETTINGS
from .pancake_client import from_env
from .shadow import summarize
from .worker import bootstrap_rounds, loop, signal_cache, status as worker_status

_STOP: Optional[asyncio.Event] = None
_TASK: Optional[asyncio.Task] = None
_BUILD_REVISION = "fusion-222-v1.0.4-startup-hardening-v1366-only"


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


app = FastAPI(title="FUSION-222 Paper Bot", version=SETTINGS.version, lifespan=lifespan)
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
        "mode": "PAPER",
        "strategy": "EV>=2%, selected probability>=53%, payout ready, Shadow Recent, Quality PF>=0.85, Quality WR display-only, 3 losses -> skip next 3 eligible signals, fixed $22",
        "stake_mode": "fixed_22",
        "fixed_stake": SETTINGS.fixed_stake,
        "database_mode": "shared Fusion PostgreSQL; main history read-only; FUSION-222 writes only private fusion222_* tables",
        "retro_reporting": "virtual bank/PnL/PF/DD are initialized ONLY from the historical v1.3.6.6 slice replayed with FUSION-222 logic; timer uses the same v1.3.6.6 start",
        "signal_url": "/signal",
        "status_url": "/status?history=none",
        "combined_history_url": "/history/combined?limit=100",
        "combined_csv_url": "/history/export-combined.csv",
    }


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
        "tables": db.table_names(),
        "fixed_stake": SETTINGS.fixed_stake,
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
    retro_count = int(metrics.get("retro_trades_count") or db.retro_history_count())
    rows: list[dict[str, Any]] = []
    if history == "recent":
        rows = db.combined_trade_history(min(limit, SETTINGS.history_api_max_limit), offset)
    elif history == "all":
        rows = db.combined_trade_history(SETTINGS.history_api_max_limit, offset)
    started_at = metrics.get("strategy_started_at")
    live_started_at = metrics.get("live_started_at")
    return _json_safe(
        {
            "ok": True,
            "service": SETTINGS.service_name,
            "version": SETTINGS.version,
            "build_revision": _BUILD_REVISION,
            "paper_state": metrics,
            "worker": worker_status(),
            "version_started_at": started_at,
            "uptime_seconds": _uptime(started_at),
            "live_started_at": live_started_at,
            "live_uptime_seconds": _uptime(live_started_at),
            "retro_scope": {
                "source": "main Fusion database",
                "base_decisions_table": db.table_names().get("base_decisions_read_only"),
                "cutoff_epoch": metrics.get("retro_cutoff_epoch"),
                "anchor_timer_version": SETTINGS.retro_anchor_version,
                "scope_version": SETTINGS.retro_scope_version,
                "all_versions_replayed": False,
                "fixed_stake": SETTINGS.fixed_stake,
                "min_trade_ev": SETTINGS.min_trade_ev,
                "min_signal_probability": SETTINGS.min_signal_probability,
                "quality_win_rate_is_gate": SETTINGS.quality_win_rate_filter_enabled,
                "quality_pf_min": SETTINGS.quality_min_profit_factor,
                "retro_trades": retro_count,
                "retro_pnl": metrics.get("retro_pnl"),
                "retro_max_drawdown": metrics.get("retro_max_drawdown"),
            },
            "history_storage": "shared PostgreSQL; base read-only + private FUSION-222 tables",
            "combined_trade_count": db.combined_trade_count(),
            "retro_trade_count": retro_count,
            "live_decision_count": live_count,
            "history": rows,
            "history_download_csv": "/history/export-combined.csv",
            "retro_history_download_csv": "/history/export-retro.csv",
            "live_history_download_csv": "/history/export-live.csv",
        }
    )


@app.get("/history/live")
def history_live(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0), trades_only: bool = False):
    rows = db.history(limit, offset, trades_only=trades_only)
    return _json_safe({"ok": True, "count": db.history_count(trades_only=trades_only), "history": rows})


@app.get("/history/retro")
def history_retro(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0)):
    return _json_safe({"ok": True, "count": db.retro_history_count(), "history": db.retro_history(limit, offset)})


@app.get("/history/combined")
def history_combined(limit: int = Query(1000, ge=1), offset: int = Query(0, ge=0)):
    return _json_safe({"ok": True, "count": db.combined_trade_count(), "history": db.combined_trade_history(limit, offset)})


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
    return _csv_response(db.combined_trade_history(SETTINGS.history_api_max_limit, 0), "fusion_222_history_COMBINED.csv")


@app.get("/history/export-retro.csv")
def export_retro():
    return _csv_response(db.retro_history(SETTINGS.history_api_max_limit, 0), "fusion_222_history_RETRO.csv")


@app.get("/history/export-live.csv")
def export_live():
    return _csv_response(db.history(SETTINGS.history_api_max_limit, 0), "fusion_222_history_LIVE.csv")


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
                "breaker_loss_trigger": SETTINGS.breaker_loss_trigger,
                "breaker_skip_signals": SETTINGS.breaker_skip_signals,
            },
            "sources": result,
        }
    )
