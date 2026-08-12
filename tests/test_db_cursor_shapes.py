from app import db


def test_first_scalar_supports_tuple_rows():
    assert db._first_scalar((7,), default=0) == 7


def test_first_scalar_supports_realdict_style_rows():
    assert db._first_scalar({"live_trades": 7}, key="live_trades", default=0) == 7
    assert db._first_scalar({"count": 9}, default=0) == 9


def test_first_scalar_handles_empty_rows():
    assert db._first_scalar(None, default=3) == 3
    assert db._first_scalar({}, default=4) == 4
