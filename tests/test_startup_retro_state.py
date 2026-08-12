from contextlib import contextmanager

from app import db


class FakeCursor:
    def __init__(self):
        self._last = ""
        self._state_reads = 0

    def execute(self, query, params=None):
        self._last = str(query)

    def fetchone(self):
        # get_state() uses SELECT * ... and needs a mapping row.
        if "SELECT * FROM" in self._last:
            self._state_reads += 1
            return {
                "id": 1,
                "retro_initialized": False,
                "retro_scope_version": None,
                "retro_cutoff_epoch": None,
            }
        # Regression target: RealDictCursor returns mappings, not tuples.
        if "COUNT" in self._last.upper():
            return {"live_trades": 0}
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self, *args, **kwargs):
        return self.cur

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_initialize_retro_state_accepts_realdict_count_row(monkeypatch):
    @contextmanager
    def fake_conn():
        yield FakeConn()

    metrics = {
        "bank": 500.0,
        "wins": 0,
        "losses": 0,
        "trades_count": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "current_loss_streak": 0,
        "max_loss_streak": 0,
        "peak_bank": 500.0,
        "min_bank": 500.0,
        "max_drawdown": 0.0,
        "last_settled_epoch": None,
        "breaker_loss_count": 0,
        "breaker_signals_remaining": 0,
        "breaker_trigger_count": 0,
        "pnl": 0.0,
        "strategy_started_at": None,
    }

    monkeypatch.setattr(db, "conn", fake_conn)
    monkeypatch.setattr(db, "_retro_cutoff_epoch", lambda: 123)
    monkeypatch.setattr(db, "_retro_replay", lambda cutoff: ([], dict(metrics)))

    # v1.0.3 raised KeyError: 0 here when COUNT(*) was returned by RealDictCursor.
    db.initialize_retro_state()
