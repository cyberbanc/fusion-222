from contextlib import contextmanager

from app import db


class _FakeCursor:
    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def cursor(self, *args, **kwargs):
        return _FakeCursor()

    def commit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_init_db_sql_templates_accept_literal_jsonb_braces(monkeypatch):
    monkeypatch.setattr(db, "conn", lambda: _FakeConn())
    monkeypatch.setattr(db, "_resolve_base_tables", lambda cur: None)
    monkeypatch.setattr(db, "_add_columns", lambda cur, table, specs: None)
    monkeypatch.setattr(db, "initialize_retro_state", lambda: None)
    # Regression target: this used to raise IndexError: tuple index out of range
    # while formatting CREATE TABLE statements containing DEFAULT '{}'::jsonb.
    db.init_db()
