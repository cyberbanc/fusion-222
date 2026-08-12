from app.config import SETTINGS
from app import db


def test_private_write_tables_and_main_read_source():
    names = db.table_names()
    assert names["base_decisions_read_only"] == "paper_decisions"
    assert names["decisions"] == "fusion222_v1366_decisions"
    assert names["state"] == "fusion222_v1366_state"
    assert names["snapshots"] == "fusion222_v1366_snapshots"
    assert names["decisions"] != names["base_decisions_read_only"]
