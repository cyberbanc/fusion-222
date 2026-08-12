from __future__ import annotations

import math
import time
from typing import Any

import requests

from .config import SETTINGS


class BinanceClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "m9-fusion-ev/1.3.6.6"})
        self.last_error: str | None = None
        self.last_base_url: str | None = None

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for base in SETTINGS.binance_base_urls:
            try:
                response = self.session.get(
                    base.rstrip("/") + path,
                    params=params,
                    timeout=SETTINGS.binance_timeout_seconds,
                )
                response.raise_for_status()
                self.last_base_url = base
                self.last_error = None
                return response.json()
            except Exception as exc:
                last = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"Binance unavailable: {last}")

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (period + 1.0)
        value = values[0]
        for x in values[1:]:
            value = alpha * x + (1 - alpha) * value
        return value

    def snapshot(self) -> dict[str, Any]:
        if not SETTINGS.binance_enabled:
            return {"available": False, "reason": "disabled"}
        try:
            symbol = SETTINGS.binance_symbol
            ticker = self._get("/api/v3/ticker/price", {"symbol": symbol})
            price = float(ticker["price"])
            klines = self._get(
                "/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": 60}
            )
            closes = [float(x[4]) for x in klines]
            returns_bp: dict[str, float] = {}
            for seconds, bars in ((15, 1), (30, 1), (60, 1)):
                ref = closes[-2] if len(closes) >= 2 else price
                returns_bp[str(seconds)] = (price / ref - 1) * 10000 if ref else 0.0
            ema9 = self._ema(closes[-30:], 9)
            ema21 = self._ema(closes[-50:], 21)
            ema_spread = (ema9 / ema21 - 1) * 10000 if ema21 else 0.0

            depth = self._get("/api/v3/depth", {"symbol": symbol, "limit": 20})
            bid_notional = sum(float(p) * float(q) for p, q in depth.get("bids", []))
            ask_notional = sum(float(p) * float(q) for p, q in depth.get("asks", []))
            book_imbalance = (
                (bid_notional - ask_notional) / (bid_notional + ask_notional)
                if bid_notional + ask_notional > 0
                else 0.0
            )

            trades = self._get("/api/v3/aggTrades", {"symbol": symbol, "limit": 500})
            cutoff = int(time.time() * 1000) - 60_000
            buy = 0.0
            sell = 0.0
            for trade in trades:
                if int(trade.get("T", 0)) < cutoff:
                    continue
                notional = float(trade["p"]) * float(trade["q"])
                # m=True means buyer was maker, so aggressive taker was seller.
                if bool(trade.get("m")):
                    sell += notional
                else:
                    buy += notional
            taker_imbalance = (buy - sell) / (buy + sell) if buy + sell > 0 else 0.0
            score = (
                max(-1.0, min(1.0, ema_spread / 15.0)) * 0.35
                + max(-1.0, min(1.0, returns_bp["60"] / 10.0)) * 0.25
                + max(-1.0, min(1.0, taker_imbalance)) * 0.20
                + max(-1.0, min(1.0, book_imbalance)) * 0.20
            )
            probability_up = max(0.35, min(0.65, 0.5 + score * 0.15))
            return {
                "available": True,
                "price": price,
                "symbol": symbol,
                "base_url": self.last_base_url,
                "returns_bp": returns_bp,
                "ema_spread_1m_bp": ema_spread,
                "taker_imbalance_60s": taker_imbalance,
                "book_imbalance_top20": book_imbalance,
                "taker_buy_notional_60s": buy,
                "taker_sell_notional_60s": sell,
                "book_bid_notional_top20": bid_notional,
                "book_ask_notional_top20": ask_notional,
                "probability_up": probability_up,
                "updated_at_ms": int(time.time() * 1000),
            }
        except Exception as exc:
            return {
                "available": False,
                "reason": "binance_error",
                "error": f"{type(exc).__name__}: {exc}",
                "probability_up": 0.5,
            }


_CLIENT: BinanceClient | None = None


def from_env() -> BinanceClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = BinanceClient()
    return _CLIENT
