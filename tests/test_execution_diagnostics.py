import json
import subprocess
import sys
import threading

import pytest

from app import pancake_client
from app.execution_diagnostics import CURRENT_TRACE, ExecutionTrace, span


def test_rpc_retry_timings_exclude_urls_payloads_and_error_messages(monkeypatch):
    client = pancake_client.PancakeClient.__new__(pancake_client.PancakeClient)
    client._lock = threading.RLock()
    client._providers = [("https://secret-token.rpc.invalid", object()), ("https://backup.invalid", object())]
    client._active = 0
    client._last_errors = {}
    monkeypatch.setattr(pancake_client.time, "sleep", lambda _: None)
    calls = []
    def read(w3):
        calls.append(w3)
        if len(calls) == 1:
            raise ValueError("secret-token provider error with request payload")
        return 123
    trace = ExecutionTrace(123)
    token = CURRENT_TRACE.set(trace)
    try:
        with span("test_read"):
            assert client._call(read, operation="chain_timestamp") == 123
    finally:
        CURRENT_TRACE.reset(token)
    data = trace.finish("BLOCKED")
    assert data["rpc_count"] == 2
    assert [x["exception_type"] for x in data["rpc"]] == ["ValueError", None]
    assert [x["provider_index"] for x in data["rpc"]] == [0, 1]
    assert data["stages"][0]["operation"] == "test_read"
    assert all(x["duration_ms"] >= 0 for x in data["rpc"])
    assert "secret-token" not in json.dumps(data)
    assert "https://" not in json.dumps(data)
    assert CURRENT_TRACE.get() is None


def test_rpc_trace_storage_is_bounded():
    trace = ExecutionTrace(123)
    for i in range(100):
        trace.rpc({"attempt": i})
    data = trace.finish("SUBMITTED")
    assert data["rpc_count"] == 100
    assert len(data["rpc"]) == 64
    assert data["rpc"][0]["attempt"] == 36


def test_trace_context_is_not_shared_with_other_threads():
    trace = ExecutionTrace(123)
    token = CURRENT_TRACE.set(trace)
    observed = []
    try:
        thread = threading.Thread(target=lambda: observed.append(CURRENT_TRACE.get()))
        thread.start()
        thread.join()
    finally:
        CURRENT_TRACE.reset(token)
    assert observed == [None]


def test_execution_info_is_emitted_with_production_uvicorn_logging():
    result = subprocess.run([sys.executable, "-c", """
from logging.config import dictConfig
from uvicorn.config import LOGGING_CONFIG
from app.live_execution import _LOG
dictConfig(LOGGING_CONFIG)
_LOG.info('execution_diagnostics logging_smoke')
"""], capture_output=True, text=True, check=True)
    assert "execution_diagnostics logging_smoke" in result.stderr
