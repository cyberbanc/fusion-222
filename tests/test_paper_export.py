"""The public historical archive never includes REAL tables."""

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


def test_paper_archive_downloads_without_password(monkeypatch):
    monkeypatch.delenv("PAPER_EXPORT_PASSWORD", raising=False)
    monkeypatch.setattr(paper_export, "archive", lambda: "read-only-archive")
    assert export_paper_history() == "read-only-archive"
