from pathlib import Path


def test_no_direct_fetchone_zero_index_in_db_source():
    source = Path("app/db.py").read_text()
    assert "fetchone()[0]" not in source


def test_alter_jsonb_defaults_use_single_literal_braces():
    source = Path("app/db.py").read_text()
    # CREATE TABLE templates must escape braces because those strings are .format()'ed.
    assert "DEFAULT '{{}}'::jsonb" in source
    # _add_columns ddl fragments are inserted as SQL objects and must contain valid JSON '{}'.
    assert '\"weights_json\": \"JSONB NOT NULL DEFAULT \'{}\'::jsonb\"' in source
    assert '\"shadow_stats_json\": \"JSONB NOT NULL DEFAULT \'{}\'::jsonb\"' in source
