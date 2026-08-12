from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .abi import CHAINLINK_ABI, PREDICTION_ABI
from .config import SETTINGS
from .models import MarketSnapshot, RoundData


class PancakeClient:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: list[tuple[str, Web3]] = []
        self._active = 0
        self._last_errors: dict[str, str] = {}
        self._oracle_address: str | None = None
        self._oracle_decimals: int | None = None
        for url in SETTINGS.bsc_rpc_urls:
            web3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
            # BNB Smart Chain is a PoSA/PoA-style EVM chain whose block
            # ``extraData`` field is longer than Ethereum mainnet's 32-byte
            # validation limit.  Web3.py v7 requires this middleware at layer
            # zero before calls such as eth_getBlockByNumber/get_block().
            web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            self._providers.append((url, web3))
        if not self._providers:
            raise RuntimeError("No BSC RPC URL configured")

    @property
    def active_url(self) -> str:
        return self._providers[self._active][0]

    @property
    def web3(self) -> Web3:
        return self._providers[self._active][1]

    def _rotate(self) -> None:
        self._active = (self._active + 1) % len(self._providers)

    def _call(self, fn, attempts: int | None = None):
        attempts = attempts or max(2, len(self._providers) * 2)
        last_error: Exception | None = None
        with self._lock:
            for _ in range(attempts):
                url = self.active_url
                try:
                    return fn(self.web3)
                except Exception as exc:  # RPC failures must trigger fallback.
                    last_error = exc
                    self._last_errors[url] = f"{type(exc).__name__}: {exc}"
                    self._rotate()
                    time.sleep(0.15)
        raise RuntimeError(f"All BSC RPC endpoints failed: {last_error}")

    def is_connected(self) -> bool:
        try:
            return bool(self._call(lambda w3: w3.is_connected()))
        except Exception:
            return False

    def rpc_status(self) -> dict[str, Any]:
        return {
            "active_rpc_url": self.active_url,
            "rpc_count": len(self._providers),
            "last_errors": dict(self._last_errors),
            "poa_middleware": True,
        }

    def _prediction(self, w3: Web3):
        return w3.eth.contract(
            address=Web3.to_checksum_address(SETTINGS.prediction_contract),
            abi=PREDICTION_ABI,
        )

    def current_epoch(self) -> int:
        return int(self._call(lambda w3: self._prediction(w3).functions.currentEpoch().call()))

    def chain_timestamp(self) -> int:
        return int(self._call(lambda w3: w3.eth.get_block("latest")["timestamp"]))

    def round(self, epoch: int) -> RoundData:
        raw = self._call(lambda w3: self._prediction(w3).functions.rounds(int(epoch)).call())
        values = list(raw)
        if len(values) < 14:
            raise RuntimeError(f"Unexpected rounds() response length: {len(values)}")
        price_scale = 10**8
        lock_price = float(values[4]) / price_scale if int(values[4]) > 0 else None
        close_price = float(values[5]) / price_scale if int(values[5]) > 0 else None
        return RoundData(
            epoch=int(values[0]),
            start_timestamp=int(values[1]),
            lock_timestamp=int(values[2]),
            close_timestamp=int(values[3]),
            lock_price=lock_price,
            close_price=close_price,
            lock_oracle_id=int(values[6]),
            close_oracle_id=int(values[7]),
            total_amount_bnb=float(values[8]) / 1e18,
            bull_amount_bnb=float(values[9]) / 1e18,
            bear_amount_bnb=float(values[10]) / 1e18,
            reward_base_bnb=float(values[11]) / 1e18,
            reward_amount_bnb=float(values[12]) / 1e18,
            oracle_called=bool(values[13]),
        )

    def oracle_address(self) -> str:
        if self._oracle_address:
            return self._oracle_address
        address = self._call(lambda w3: self._prediction(w3).functions.oracle().call())
        self._oracle_address = Web3.to_checksum_address(address)
        return self._oracle_address

    def oracle_price(self) -> tuple[float, int, int]:
        address = self.oracle_address()

        def read(w3: Web3):
            contract = w3.eth.contract(address=address, abi=CHAINLINK_ABI)
            decimals = self._oracle_decimals
            if decimals is None:
                decimals = int(contract.functions.decimals().call())
                self._oracle_decimals = decimals
            result = contract.functions.latestRoundData().call()
            return int(result[0]), float(result[1]) / (10**decimals), int(result[3])

        return self._call(read)

    def market_snapshot(self) -> MarketSnapshot:
        current_epoch = self.current_epoch()
        betting_epoch = current_epoch
        live_epoch = current_epoch - 1
        betting_round = self.round(betting_epoch)
        live_round = self.round(live_epoch)
        chain_ts = self.chain_timestamp()
        oracle_round_id, price, oracle_updated_at = self.oracle_price()
        seconds_to_lock = int(betting_round.lock_timestamp - chain_ts)
        decision_window = SETTINGS.min_decision_seconds <= seconds_to_lock <= SETTINGS.prelock_seconds
        safe_to_decide = seconds_to_lock >= SETTINGS.min_decision_seconds
        live_lock = live_round.lock_price or price
        live_move_signed = float(price - live_lock)
        live_move_points = abs(live_move_signed)
        direction = "UP" if live_move_signed >= 0 else "DOWN"
        total = betting_round.total_amount_bnb
        bull = betting_round.bull_amount_bnb
        bear = betting_round.bear_amount_bnb
        gross_up = total / bull if total > 0 and bull > 0 else None
        gross_down = total / bear if total > 0 and bear > 0 else None
        net_up = gross_up * (1 - SETTINGS.treasury_fee) if gross_up else None
        net_down = gross_down * (1 - SETTINGS.treasury_fee) if gross_down else None
        bull_share = bull / total * 100 if total > 0 else None
        bear_share = bear / total * 100 if total > 0 else None
        return MarketSnapshot(
            current_epoch=current_epoch,
            betting_epoch=betting_epoch,
            live_epoch=live_epoch,
            chain_timestamp=chain_ts,
            seconds_to_lock=seconds_to_lock,
            decision_window=decision_window,
            safe_to_decide=safe_to_decide,
            chainlink_price=price,
            oracle_round_id=oracle_round_id,
            oracle_updated_at=oracle_updated_at,
            oracle_age_seconds=max(0, chain_ts - oracle_updated_at),
            live_round=live_round,
            betting_round=betting_round,
            live_move_signed=live_move_signed,
            live_move_points=live_move_points,
            current_direction=direction,
            current_gross_coeff_up=gross_up,
            current_gross_coeff_down=gross_down,
            current_net_coeff_up=net_up,
            current_net_coeff_down=net_down,
            betting_bull_share_pct=bull_share,
            betting_bear_share_pct=bear_share,
            rpc_status=self.rpc_status(),
        )


_CLIENT: PancakeClient | None = None
_CLIENT_LOCK = threading.Lock()


def from_env() -> PancakeClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = PancakeClient()
        return _CLIENT
