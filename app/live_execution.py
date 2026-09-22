from __future__ import annotations

import math
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from . import db
from .config import SETTINGS
from .pancake_client import PancakeClient, from_env as pancake_from_env


def _hex(value: Any) -> str:
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else f"0x{text}"


class LiveExecutor:
    """Idempotent real-money executor for one dedicated Railway service.

    The signal engine remains unchanged. This class only converts the fixed USD
    stake to BNB, signs the PancakeSwap transaction and reconciles it with the
    on-chain ledger. A persisted transaction hash and the contract ledger guard
    against duplicate bets after a process restart.
    """

    def __init__(self, client: PancakeClient | None = None) -> None:
        self.client = client or pancake_from_env()
        self._lock = threading.RLock()
        self._account = None
        self._configuration_error: str | None = None
        self._last_wallet_sync = 0.0
        self._last_wallet: dict[str, Any] = {}
        self._load_account()

    def _load_account(self) -> None:
        key = SETTINGS.wallet_private_key
        if not key:
            self._configuration_error = "WALLET_PRIVATE_KEY is not configured"
            return
        try:
            account = Account.from_key(key)
            expected = SETTINGS.expected_wallet_address
            if expected and Web3.to_checksum_address(expected) != account.address:
                raise ValueError("WALLET_ADDRESS does not match WALLET_PRIVATE_KEY")
            self._account = account
            self._configuration_error = None
        except Exception as exc:
            self._account = None
            self._configuration_error = f"{type(exc).__name__}: {exc}"

    @property
    def address(self) -> str | None:
        if self._account is not None:
            return str(self._account.address)
        if SETTINGS.expected_wallet_address:
            try:
                return Web3.to_checksum_address(SETTINGS.expected_wallet_address)
            except Exception:
                return SETTINGS.expected_wallet_address
        return None

    @property
    def ready(self) -> bool:
        return bool(
            SETTINGS.execution_mode == "REAL"
            and SETTINGS.real_execution_enabled
            and self._account is not None
            and self._configuration_error is None
        )

    def status(self) -> dict[str, Any]:
        return {
            "mode": SETTINGS.execution_mode,
            "enabled": SETTINGS.real_execution_enabled,
            "ready": self.ready,
            "private_key_configured": bool(SETTINGS.wallet_private_key),
            "wallet_address": self.address,
            "configuration_error": self._configuration_error,
            "chain_id": SETTINGS.chain_id,
            "auto_claim_enabled": SETTINGS.auto_claim_enabled,
            "min_gas_reserve_bnb": SETTINGS.min_gas_reserve_bnb,
        }

    def _contract(self, w3: Web3):
        return self.client._prediction(w3)

    def _balance_wei(self, address: str) -> int:
        return int(self.client._call(lambda w3: w3.eth.get_balance(address)))

    def wallet_snapshot(self, bnb_usd_price: float, *, source: str = "POLL", force: bool = False) -> dict[str, Any]:
        address = self.address
        if not address or not bnb_usd_price or bnb_usd_price <= 0:
            return dict(self._last_wallet)
        now = time.time()
        if not force and self._last_wallet and now - self._last_wallet_sync < SETTINGS.wallet_sync_seconds:
            return dict(self._last_wallet)
        balance_wei, block_number = self.client._call(
            lambda w3: (w3.eth.get_balance(address), w3.eth.block_number)
        )
        balance_bnb = float(Web3.from_wei(int(balance_wei), "ether"))
        item = db.save_wallet_snapshot(
            {
                "wallet_address": address,
                "block_number": int(block_number),
                "balance_bnb": balance_bnb,
                "bnb_usd_price": float(bnb_usd_price),
                "balance_usd": balance_bnb * float(bnb_usd_price),
                "source": source,
                "details_json": {"execution_ready": self.ready},
            }
        )
        self._last_wallet = dict(item)
        self._last_wallet_sync = now
        return dict(item)

    def ledger(self, epoch: int) -> dict[str, Any]:
        address = self.address
        if not address:
            return {"amount_wei": 0, "amount_bnb": 0.0, "position": None, "claimed": False}

        def read(w3: Web3):
            return self._contract(w3).functions.ledger(int(epoch), address).call()

        raw = self.client._call(read)
        amount_wei = int(raw[0])
        return {
            "amount_wei": amount_wei,
            "amount_bnb": float(Web3.from_wei(amount_wei, "ether")),
            "position": int(raw[1]),
            "claimed": bool(raw[2]),
        }

    def _receipt(self, tx_hash: str) -> dict[str, Any] | None:
        try:
            receipt = self.client._call(lambda w3: w3.eth.get_transaction_receipt(tx_hash), attempts=2)
            return dict(receipt)
        except TransactionNotFound:
            return None
        except RuntimeError as exc:
            if "not found" in str(exc).lower():
                return None
            raise

    @staticmethod
    def _gas_fee_bnb(receipt: dict[str, Any]) -> float:
        used = int(receipt.get("gasUsed") or 0)
        price = int(receipt.get("effectiveGasPrice") or 0)
        return float(Web3.from_wei(used * price, "ether"))

    def _build_sign(self, fn, *, value_wei: int = 0) -> tuple[Any, str, int, int, int]:
        if self._account is None:
            raise RuntimeError(self._configuration_error or "wallet is not configured")
        address = self._account.address

        def build(w3: Web3):
            nonce = int(w3.eth.get_transaction_count(address, "pending"))
            gas_price = int(w3.eth.gas_price)
            base = {
                "from": address,
                "value": int(value_wei),
                "nonce": nonce,
                "chainId": SETTINGS.chain_id,
                "gasPrice": gas_price,
            }
            estimate = int(fn(w3).estimate_gas(base))
            gas = max(estimate, int(math.ceil(estimate * SETTINGS.gas_limit_multiplier)))
            tx = fn(w3).build_transaction({**base, "gas": gas})
            signed = self._account.sign_transaction(tx)
            return signed, nonce, gas, gas_price

        signed, nonce, gas, gas_price = self.client._call(build)
        return signed, _hex(signed.hash), nonce, gas, gas_price

    def _broadcast_and_wait(self, signed: Any, tx_hash: str) -> dict[str, Any] | None:
        try:
            self.client._call(lambda w3: w3.eth.send_raw_transaction(signed.raw_transaction))
        except Exception:
            # The RPC can return an error after accepting the payload. The
            # deterministic signed hash is already persisted, so never create a
            # second transaction here.
            found = self._receipt(tx_hash)
            if found is None:
                raise
            return found
        try:
            return dict(
                self.client._call(
                    lambda w3: w3.eth.wait_for_transaction_receipt(
                        tx_hash,
                        timeout=SETTINGS.transaction_timeout_seconds,
                        poll_latency=2,
                    ),
                    attempts=1,
                )
            )
        except TimeExhausted:
            return None
        except RuntimeError as exc:
            if "time" in str(exc).lower() and "exhaust" in str(exc).lower():
                return None
            raise

    def reconcile_bet(self, decision: dict[str, Any], bnb_usd_price: float) -> dict[str, Any]:
        epoch = int(decision["betting_epoch"])
        tx_hash = str(decision.get("tx_hash") or "")
        if tx_hash:
            receipt = self._receipt(tx_hash)
            if receipt is None:
                return db.update_execution(epoch, execution_status="SUBMITTED", reconciliation_status="TX_PENDING")
            if int(receipt.get("status") or 0) != 1:
                return db.update_execution(
                    epoch,
                    execution_status="FAILED",
                    reconciliation_status="TX_REVERTED",
                    execution_error="On-chain bet transaction reverted",
                    trade_executed=False,
                )
            ledger = self.ledger(epoch)
            gas_fee = self._gas_fee_bnb(receipt)
            if ledger["amount_wei"] <= 0:
                db.upsert_transaction(
                    {
                        "event_type": "BET",
                        "betting_epoch": epoch,
                        "tx_hash": tx_hash,
                        "tx_status": "CONFIRMED_LEDGER_MISSING",
                        "wallet_address": self.address,
                        "gas_fee_bnb": gas_fee,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {"block_number": int(receipt.get("blockNumber") or 0)},
                    }
                )
                return db.update_execution(
                    epoch,
                    execution_status="RECONCILIATION_ERROR",
                    reconciliation_status="TX_CONFIRMED_LEDGER_MISSING",
                    bet_gas_fee_bnb=gas_fee,
                    execution_error="Bet receipt succeeded but contract ledger amount is zero",
                    trade_executed=False,
                )
            db.upsert_transaction(
                {
                    "event_type": "BET",
                    "betting_epoch": epoch,
                    "tx_hash": tx_hash,
                    "tx_status": "CONFIRMED",
                    "wallet_address": self.address,
                    "amount_bnb": ledger["amount_bnb"],
                    "gas_fee_bnb": gas_fee,
                    "bnb_usd_price": bnb_usd_price,
                    "metadata_json": {"block_number": int(receipt.get("blockNumber") or 0)},
                }
            )
            return db.update_execution(
                epoch,
                execution_status="CONFIRMED",
                reconciliation_status="ONCHAIN_CONFIRMED",
                stake_bnb=ledger["amount_bnb"],
                bet_gas_fee_bnb=gas_fee,
                trade_executed=True,
                execution_error=None,
            )

        ledger = self.ledger(epoch)
        if ledger["amount_wei"] > 0:
            return db.update_execution(
                epoch,
                execution_status="RECONCILED",
                reconciliation_status="CONTRACT_LEDGER_MATCH",
                wallet_address=self.address,
                stake_bnb=ledger["amount_bnb"],
                bnb_usd_price=bnb_usd_price,
                trade_executed=True,
                execution_error=None,
            )
        return decision

    def execute_bet(self, decision: dict[str, Any], bnb_usd_price: float) -> dict[str, Any]:
        with self._lock:
            epoch = int(decision["betting_epoch"])
            if not bool(decision.get("execution_requested")):
                return decision
            reconciled = self.reconcile_bet(decision, bnb_usd_price)
            if bool(reconciled.get("trade_executed")) or reconciled.get("tx_hash"):
                return reconciled
            if not self.ready:
                return db.update_execution(
                    epoch,
                    execution_status="BLOCKED",
                    reconciliation_status="WALLET_NOT_READY",
                    execution_error=self._configuration_error or "Real execution is disabled",
                    trade_executed=False,
                )
            side = str(decision.get("signal") or "").upper()
            if side not in {"UP", "DOWN"}:
                return db.update_execution(
                    epoch,
                    execution_status="FAILED",
                    execution_error=f"Unsupported signal: {side}",
                    trade_executed=False,
                )
            if bnb_usd_price <= 0:
                return db.update_execution(
                    epoch,
                    execution_status="BLOCKED",
                    execution_error="Invalid BNB/USD price",
                    trade_executed=False,
                )

            stake_usd = Decimal(str(decision.get("stake") or SETTINGS.fixed_stake))
            stake_bnb = (stake_usd / Decimal(str(bnb_usd_price))).quantize(
                Decimal("0.000000000000000001"), rounding=ROUND_DOWN
            )
            value_wei = int(stake_bnb * Decimal(10**18))

            paused, min_bet_wei = self.client._call(
                lambda w3: (
                    bool(self._contract(w3).functions.paused().call()),
                    int(self._contract(w3).functions.minBetAmount().call()),
                )
            )
            if paused:
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="Prediction contract is paused")
            if value_wei < min_bet_wei:
                return db.update_execution(
                    epoch,
                    execution_status="BLOCKED",
                    execution_error=f"Stake is below contract minimum ({min_bet_wei} wei)",
                )

            balance_before_wei = self._balance_wei(self.address or "")
            fn = (
                (lambda w3: self._contract(w3).functions.betBull(epoch))
                if side == "UP"
                else (lambda w3: self._contract(w3).functions.betBear(epoch))
            )
            signed, tx_hash, nonce, gas, gas_price = self._build_sign(fn, value_wei=value_wei)
            reserve_wei = int(Web3.to_wei(SETTINGS.min_gas_reserve_bnb, "ether"))
            required_wei = value_wei + gas * gas_price + reserve_wei
            if balance_before_wei < required_wei:
                return db.update_execution(
                    epoch,
                    execution_status="BLOCKED",
                    wallet_address=self.address,
                    stake_bnb=float(stake_bnb),
                    bnb_usd_price=bnb_usd_price,
                    execution_error=(
                        f"Insufficient BNB balance: need {float(Web3.from_wei(required_wei, 'ether')):.8f} BNB "
                        "including gas reserve"
                    ),
                    trade_executed=False,
                )

            balance_before_bnb = float(Web3.from_wei(balance_before_wei, "ether"))
            decision = db.update_execution(
                epoch,
                execution_status="PREPARED",
                wallet_address=self.address,
                tx_hash=tx_hash,
                tx_nonce=nonce,
                stake_bnb=float(stake_bnb),
                bnb_usd_price=bnb_usd_price,
                reconciliation_status="SIGNED_HASH_PERSISTED",
                execution_error=None,
            )
            db.upsert_transaction(
                {
                    "event_type": "BET",
                    "betting_epoch": epoch,
                    "tx_hash": tx_hash,
                    "tx_status": "PREPARED",
                    "wallet_address": self.address,
                    "amount_bnb": float(stake_bnb),
                    "balance_before_bnb": balance_before_bnb,
                    "bnb_usd_price": bnb_usd_price,
                    "metadata_json": {"side": side, "nonce": nonce, "gas_limit": gas, "gas_price_wei": gas_price},
                }
            )
            try:
                receipt = self._broadcast_and_wait(signed, tx_hash)
            except Exception as exc:
                db.upsert_transaction(
                    {
                        "event_type": "BET",
                        "betting_epoch": epoch,
                        "tx_hash": tx_hash,
                        "tx_status": "SUBMISSION_UNKNOWN",
                        "wallet_address": self.address,
                        "amount_bnb": float(stake_bnb),
                        "balance_before_bnb": balance_before_bnb,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {"error": f"{type(exc).__name__}: {exc}"},
                    }
                )
                return db.update_execution(
                    epoch,
                    execution_status="SUBMISSION_UNKNOWN",
                    execution_error=f"{type(exc).__name__}: {exc}",
                    trade_executed=False,
                )
            if receipt is None:
                db.upsert_transaction(
                    {
                        "event_type": "BET",
                        "betting_epoch": epoch,
                        "tx_hash": tx_hash,
                        "tx_status": "SUBMITTED",
                        "wallet_address": self.address,
                        "amount_bnb": float(stake_bnb),
                        "balance_before_bnb": balance_before_bnb,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {},
                    }
                )
                return db.update_execution(epoch, execution_status="SUBMITTED", reconciliation_status="TX_PENDING")
            return self.reconcile_bet(db.get_decision(epoch) or decision, bnb_usd_price)

    def _claim_flags(self, epoch: int) -> tuple[bool, bool]:
        address = self.address
        if not address:
            return False, False
        return self.client._call(
            lambda w3: (
                bool(self._contract(w3).functions.claimable(epoch, address).call()),
                bool(self._contract(w3).functions.refundable(epoch, address).call()),
            )
        )

    def finalize_actual(self, decision: dict[str, Any], bnb_usd_price: float) -> dict[str, Any]:
        with self._lock:
            if not bool(decision.get("trade_executed")) or not bool(decision.get("settled")):
                return decision
            epoch = int(decision["betting_epoch"])
            stake_bnb = float(decision.get("stake_bnb") or 0.0)
            bet_gas = float(decision.get("bet_gas_fee_bnb") or 0.0)
            bet_price = float(decision.get("bnb_usd_price") or bnb_usd_price)
            outcome = str(decision.get("outcome") or "").upper()
            if outcome == "LOSS":
                pnl_bnb = -(stake_bnb + bet_gas)
                return db.update_execution(
                    epoch,
                    actual_payout_bnb=0.0,
                    actual_pnl_bnb=pnl_bnb,
                    actual_pnl_usd=-(stake_bnb + bet_gas) * bet_price,
                    reconciliation_status="LOSS_FINAL",
                )
            if outcome not in {"WIN", "REFUND"} or not SETTINGS.auto_claim_enabled or not self.ready:
                return decision

            claim_hash = str(decision.get("claim_tx_hash") or "")
            if claim_hash:
                receipt = self._receipt(claim_hash)
                if receipt is None:
                    return db.update_execution(epoch, reconciliation_status="CLAIM_PENDING")
                if int(receipt.get("status") or 0) != 1:
                    return db.update_execution(
                        epoch,
                        reconciliation_status="CLAIM_REVERTED",
                        execution_error="On-chain claim transaction reverted",
                    )
                gas_fee = self._gas_fee_bnb(receipt)
                tx = db.get_transaction(claim_hash) or {}
                before = float(tx.get("balance_before_bnb") or 0.0)
                after = self.wallet_snapshot(bnb_usd_price, source="CLAIM", force=True)
                after_bnb = float(after.get("balance_bnb") or 0.0)
                payout = max(0.0, after_bnb - before + gas_fee)
                pnl_bnb = payout - stake_bnb - bet_gas - gas_fee
                pnl_usd = payout * bnb_usd_price - (stake_bnb + bet_gas) * bet_price - gas_fee * bnb_usd_price
                db.upsert_transaction(
                    {
                        "event_type": "CLAIM",
                        "betting_epoch": epoch,
                        "tx_hash": claim_hash,
                        "tx_status": "CONFIRMED",
                        "wallet_address": self.address,
                        "amount_bnb": payout,
                        "gas_fee_bnb": gas_fee,
                        "balance_before_bnb": before,
                        "balance_after_bnb": after_bnb,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {"block_number": int(receipt.get("blockNumber") or 0)},
                    }
                )
                return db.update_execution(
                    epoch,
                    claim_gas_fee_bnb=gas_fee,
                    actual_payout_bnb=payout,
                    actual_pnl_bnb=pnl_bnb,
                    actual_pnl_usd=pnl_usd,
                    reconciliation_status="CLAIM_CONFIRMED",
                    execution_error=None,
                )

            ledger = self.ledger(epoch)
            if ledger["claimed"]:
                round_data = self.client.round(epoch)
                payout = 0.0
                if round_data.reward_base_bnb > 0 and round_data.reward_amount_bnb > 0:
                    payout = stake_bnb * round_data.reward_amount_bnb / round_data.reward_base_bnb
                pnl_bnb = payout - stake_bnb - bet_gas
                return db.update_execution(
                    epoch,
                    actual_payout_bnb=payout,
                    actual_pnl_bnb=pnl_bnb,
                    actual_pnl_usd=payout * bnb_usd_price - (stake_bnb + bet_gas) * bet_price,
                    reconciliation_status="EXTERNAL_CLAIM_PAYOUT_RECONSTRUCTED_GAS_UNKNOWN",
                )
            claimable, refundable = self._claim_flags(epoch)
            if not claimable and not refundable:
                return decision
            balance_before = self.wallet_snapshot(bnb_usd_price, source="PRE_CLAIM", force=True)
            before_bnb = float(balance_before.get("balance_bnb") or 0.0)
            fn = lambda w3: self._contract(w3).functions.claim([epoch])
            signed, tx_hash, nonce, gas, gas_price = self._build_sign(fn)
            db.update_execution(epoch, claim_tx_hash=tx_hash, reconciliation_status="CLAIM_PREPARED")
            db.upsert_transaction(
                {
                    "event_type": "CLAIM",
                    "betting_epoch": epoch,
                    "tx_hash": tx_hash,
                    "tx_status": "PREPARED",
                    "wallet_address": self.address,
                    "amount_bnb": None,
                    "balance_before_bnb": before_bnb,
                    "bnb_usd_price": bnb_usd_price,
                    "metadata_json": {"nonce": nonce, "gas_limit": gas, "gas_price_wei": gas_price},
                }
            )
            try:
                receipt = self._broadcast_and_wait(signed, tx_hash)
            except Exception as exc:
                db.upsert_transaction(
                    {
                        "event_type": "CLAIM",
                        "betting_epoch": epoch,
                        "tx_hash": tx_hash,
                        "tx_status": "SUBMISSION_UNKNOWN",
                        "wallet_address": self.address,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {"error": f"{type(exc).__name__}: {exc}"},
                    }
                )
                return db.update_execution(epoch, reconciliation_status="CLAIM_SUBMISSION_UNKNOWN", execution_error=str(exc))
            if receipt is None:
                db.upsert_transaction(
                    {
                        "event_type": "CLAIM",
                        "betting_epoch": epoch,
                        "tx_hash": tx_hash,
                        "tx_status": "SUBMITTED",
                        "wallet_address": self.address,
                        "balance_before_bnb": before_bnb,
                        "bnb_usd_price": bnb_usd_price,
                        "metadata_json": {},
                    }
                )
                return db.update_execution(epoch, reconciliation_status="CLAIM_PENDING")
            return self.finalize_actual(db.get_decision(epoch) or decision, bnb_usd_price)


_EXECUTOR: LiveExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def from_env(client: PancakeClient | None = None) -> LiveExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = LiveExecutor(client)
        return _EXECUTOR
