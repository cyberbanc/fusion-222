"""The historical archive stays private and never includes REAL tables."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app import paper_export
from app.main import export_paper_history


class FakeCursor:
    def execute(self, query):
        assert "information_schema.tables" in query

    def fetchall(self):
        return [
            ("paper_decisions",),
            ("fusion_snapshots_v136",),
            ("fusion222_v1366_nobreaker_snapshots",),
            ("fusion222_real_decisions",),
            ("wallet_private_keys",),
            ("paper_state",),
        ]


def test_only_explicit_paper_history_tables_are_exportable():
    assert paper_export._tables_available(FakeCursor()) == [
        "fusion222_v1366_nobreaker_snapshots",
        "fusion_snapshots_v136",
        "paper_decisions",
    ]


def test_paper_archive_cannot_be_downloaded_without_configured_password(monkeypatch):
    monkeypatch.delenv("PAPER_EXPORT_PASSWORD", raising=False)
    with pytest.raises(HTTPException) as exc:
        export_paper_history(credentials=None)
    assert exc.value.status_code == 503


def test_paper_archive_rejects_wrong_password_before_opening_database(monkeypatch):
    monkeypatch.setenv("PAPER_EXPORT_PASSWORD", "a-strong-password-for-export-only")
    monkeypatch.setattr(paper_export, "archive", lambda: pytest.fail("archive called"))
    with pytest.raises(HTTPException) as exc:
        export_paper_history(credentials=HTTPBasicCredentials(username="export", password="wrong"))
    assert exc.value.status_code == 401
    assert "Basic" in exc.value.headers["WWW-Authenticate"]


def test_paper_archive_accepts_correct_credentials(monkeypatch):
    monkeypatch.setenv("PAPER_EXPORT_PASSWORD", "a-strong-password-for-export-only")
    monkeypatch.setattr(paper_export, "archive", lambda: "read-only-archive")
    assert export_paper_history(
        credentials=HTTPBasicCredentials(username="export", password="a-strong-password-for-export-only")
    ) == "read-only-archive"
