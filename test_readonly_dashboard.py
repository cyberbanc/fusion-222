from __future__ import annotations
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import dashboard as target
from app import db, main

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.metrics = db._retro_replay(518737)
        self.state = dict(self.metrics, retro_initialized=True, retro_trades_count=291)
        self.live = dict(
            betting_epoch=518738, signal="UP", trade_executed=True, settled=True,
            outcome="WIN", pnl=10.0, projected_pnl=10.0, stake=22.0,
            bank_after=self.metrics["bank"]+10.0,
            created_at="2026-09-25T12:00:00+00:00",
        )

    @contextmanager
    def fixtures(self):
        @contextmanager
        def connection():
            class Cursor:
                def __enter__(self): return self
                def __exit__(self, *args): return False
            yield SimpleNamespace(cursor=lambda: Cursor())
        metric = dict(
            self.state, bank=self.metrics["bank"]+10.0,
            pnl=self.metrics["pnl"]+10.0, trades_count=292,
            fixed_stake=22.0, retro_pnl=self.metrics["pnl"],
            retro_max_drawdown=self.metrics["max_drawdown"],
        )
        def history(limit=100, offset=0, **kwargs):
            return [self.live][offset:offset+limit]
        with patch.object(db, "conn", connection), \
             patch.object(db, "_resolve_base_tables"), \
             patch.object(db, "get_state", return_value=self.state), \
             patch.object(db, "state_metrics", return_value=metric), \
             patch.object(db, "history", side_effect=history), \
             patch.object(db, "history_count", return_value=1), \
             patch.object(db, "ping", return_value=True), \
             patch.object(db, "init_db", side_effect=AssertionError("no initialization")), \
             patch.object(db, "initialize_retro_state", side_effect=AssertionError("no reseed")), \
             patch.object(db, "insert_decision", side_effect=AssertionError("no trades")), \
             patch.object(main, "bootstrap_rounds", side_effect=AssertionError("no producer")), \
             patch.object(main, "loop", side_effect=AssertionError("no worker")):
            db._RETRO_CACHE.clear()
            yield

    def test_readonly_startup_status_history_and_exports(self):
        original = json.dumps(self.state, default=str, sort_keys=True)
        with self.fixtures(), TestClient(target.app) as client:
            self.assertFalse(main.SETTINGS.worker_enabled)
            self.assertIn("default_transaction_read_only=on", os.environ["PGOPTIONS"])
            for path in ["/", "/dashboard"]:
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertIn("FUSION-222 ONLY UP", response.text)
                self.assertNotIn("fusion-222.up.railway.app", response.text)
            health = client.get("/healthz").json()
            self.assertTrue(health["ok"])
            self.assertTrue(health["reporting_only"])
            self.assertFalse(health["worker_enabled"])
            status = client.get("/status?history=none").json()
            self.assertEqual(status["paper_state"]["trades_count"], 292)
            self.assertEqual(status["last_decision_epoch"], 518738)
            self.assertFalse(status["worker"]["enabled"])
            combined = client.get("/history/combined?limit=1000").json()
            self.assertEqual(combined["count"], 292)
            self.assertEqual(len(combined["history"]), 292)
            self.assertAlmostEqual(sum(x["pnl"] for x in combined["history"]), status["paper_state"]["pnl"])
            self.assertEqual(client.get("/history/combined?limit=50&offset=50").json()["history"], combined["history"][50:100])
            self.assertEqual(client.get("/history/retro?limit=1000").json()["count"], 291)
            csv = client.get("/history/export-combined.csv")
            self.assertEqual(csv.status_code, 200)
            self.assertEqual(len(csv.text.strip().splitlines()), 293)
            self.assertEqual(client.post("/status").status_code, 405)
        self.assertEqual(original, json.dumps(self.state, default=str, sort_keys=True))

    def test_refuses_missing_state_without_reset(self):
        with self.fixtures():
            self.state.clear()
            with self.assertRaisesRegex(RuntimeError, "Existing initialized"):
                with TestClient(target.app): pass

    def test_refuses_different_cutoff_without_replaying(self):
        with self.fixtures():
            self.state["retro_cutoff_epoch"] = 1
            with self.assertRaisesRegex(RuntimeError, "cutoff differs"):
                with TestClient(target.app): pass

    def test_refuses_wrong_retro_count(self):
        with self.fixtures():
            self.state["retro_trades_count"] = 0
            with self.assertRaisesRegex(RuntimeError, "retro count differs"):
                with TestClient(target.app): pass

    def test_html_script_syntax(self):
        script = re.search(r"<script>(.*?)</script>", target.DASHBOARD_HTML, re.S).group(1)
        with tempfile.NamedTemporaryFile(suffix=".js", mode="w") as f:
            f.write(script); f.flush()
            subprocess.run(["node", "--check", f.name], check=True, capture_output=True)

if __name__ == "__main__":
    unittest.main()
