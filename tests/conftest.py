"""Lightweight test stubs when optional runtime packages are unavailable.

Railway installs psycopg2-binary from requirements.txt. The stub only lets pure
unit tests run in restricted build environments without PostgreSQL drivers.
"""
from __future__ import annotations

import sys
import types

try:
    import psycopg2  # noqa: F401
except ModuleNotFoundError:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("psycopg2 test stub cannot connect")
    )

    extras = types.ModuleType("psycopg2.extras")

    class Json:
        def __init__(self, value):
            self.adapted = value

    class RealDictCursor:
        pass

    extras.Json = Json
    extras.RealDictCursor = RealDictCursor

    sql = types.ModuleType("psycopg2.sql")

    class _SqlToken:
        def __init__(self, value=""):
            self.value = value

        def format(self, *args, **kwargs):
            return self

        def join(self, values):
            return self

    sql.SQL = _SqlToken
    sql.Identifier = _SqlToken
    sql.Placeholder = _SqlToken

    psycopg2.extras = extras
    psycopg2.sql = sql
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras
    sys.modules["psycopg2.sql"] = sql


try:
    import web3  # noqa: F401
except ModuleNotFoundError:
    web3 = types.ModuleType("web3")
    middleware = types.ModuleType("web3.middleware")
    exceptions = types.ModuleType("web3.exceptions")

    class _MiddlewareOnion:
        def inject(self, middleware_item, layer=0):
            return None

    class Web3:
        class HTTPProvider:
            def __init__(self, *args, **kwargs):
                pass

        def __init__(self, *args, **kwargs):
            self.middleware_onion = _MiddlewareOnion()

        @staticmethod
        def to_checksum_address(value):
            return value

        @staticmethod
        def from_wei(value, unit):
            return value / 10**18

        @staticmethod
        def to_wei(value, unit):
            return int(float(value) * 10**18)

    class ExtraDataToPOAMiddleware:
        pass

    class TimeExhausted(Exception):
        pass

    class TransactionNotFound(Exception):
        pass

    web3.Web3 = Web3
    middleware.ExtraDataToPOAMiddleware = ExtraDataToPOAMiddleware
    exceptions.TimeExhausted = TimeExhausted
    exceptions.TransactionNotFound = TransactionNotFound
    sys.modules["web3"] = web3
    sys.modules["web3.middleware"] = middleware
    sys.modules["web3.exceptions"] = exceptions


try:
    import eth_account  # noqa: F401
except ModuleNotFoundError:
    eth_account = types.ModuleType("eth_account")

    class Account:
        @staticmethod
        def from_key(value):
            raise ValueError("eth_account test stub")

    eth_account.Account = Account
    sys.modules["eth_account"] = eth_account
