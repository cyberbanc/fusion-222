"""Bounded execution timings; never include RPC URLs, payloads or credentials."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import time

CURRENT_TRACE: ContextVar = ContextVar("execution_trace", default=None)


class ExecutionTrace:
    def __init__(self, epoch: int, context: dict | None = None):
        self.started = time.monotonic()
        self.data = {"version": 1, "epoch": epoch,
                     "checked_at": datetime.now(timezone.utc).isoformat(),
                     "tick": context or {}, "stages": [], "gates": [], "rpc": []}
        self.rpc_count = 0

    def elapsed_ms(self):
        return round((time.monotonic() - self.started) * 1000, 3)

    def snapshot(self, stage, snapshot):
        self.data["gates"].append({
            "stage": stage, "at_ms": self.elapsed_ms(),
            "expected_epoch": self.data["epoch"],
            "observed_epoch": snapshot.betting_epoch,
            "chain_timestamp": snapshot.chain_timestamp,
            "seconds_to_lock": getattr(snapshot, "seconds_to_lock", None),
            "lock_timestamp": getattr(snapshot.betting_round, "lock_timestamp", None),
            "decision_window": snapshot.decision_window,
            "oracle_updated_at": snapshot.oracle_updated_at,
        })

    def rpc(self, entry):
        self.rpc_count += 1
        # Preserve the latest calls and total count, with a fixed storage bound.
        self.data["rpc"] = (self.data["rpc"] + [entry])[-64:]

    def finish(self, status, error_type=None):
        self.data.update(duration_ms=self.elapsed_ms(), status=status,
                         rpc_count=self.rpc_count)
        if error_type:
            self.data["exception_type"] = error_type
        return self.data


@contextmanager
def span(operation):
    trace = CURRENT_TRACE.get()
    started = time.monotonic()
    try:
        yield
    finally:
        if trace is not None:
            trace.data["stages"].append({"operation": operation,
                "duration_ms": round((time.monotonic() - started) * 1000, 3)})
