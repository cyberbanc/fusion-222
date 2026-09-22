from app.config import SETTINGS
from app import db


def test_private_write_tables_and_main_read_source():
    names = db.table_names()
    assert names["base_decisions_read_only"] in {"auto", "paper_decisions", "decisions", "fusion_decisions", "paper_history", "fusion_history"}
    assert names["decisions"] == "fusion222_real_decisions"
    assert names["state"] == "fusion222_real_state"
    assert names["snapshots"] == "fusion222_real_snapshots"
    assert names["paper_reference_read_only"] == "fusion222_v1366_nobreaker_decisions"
    assert names["decisions"] != names["base_decisions_read_only"]
