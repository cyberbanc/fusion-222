from __future__ import annotations

import json
import logging
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
from .execution_diagnostics import CURRENT_TRACE, ExecutionTrace, span
from .stake_policy import POLICY, stake_quote
from .pancake_client import PancakeClient, from_env as pancake_from_env


# Uvicorn configures this logger's handler; the root logger may have none.
_LOG = logging.getLogger("uvicorn.error")
_LOG.setLevel(logging.INFO)


def window_rejection(snapshot, epoch):
    if snapshot.betting_epoch != epoch:
        return "ROUND_CHANGED"
    if not snapshot.decision_window:
        remaining = getattr(snapshot, "seconds_to_lock", None)
        if remaining is not None and remaining < SETTINGS.min_decision_seconds:
            return "WINDOW_TOO_LATE"
        if remaining is not None and remaining > SETTINGS.prelock_seconds:
            return "WINDOW_TOO_EARLY"
        return "WINDOW_INVALID"
    return None


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
            if account.address.lower() in {a.lower() for a in SETTINGS.retired_wallet_addresses}:
                self._configuration_error = "Previously configured wallet must be replaced after Railway security disclosure 2026-10-21"
                self._account = None
                return
            expected = SETTINGS.expected_wallet_address
            if expected and Web3.to_checksum_address(expected) != account.address:
                raise ValueError("WALLET_ADDRESS does not match WALLET_PRIVATE_KEY")
            self._account = account
            self._configuration_error = None
        except Exception as exc:
            self._account = None
            self._configuration_error = "Invalid wallet signing configuration"

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
            and SETTINGS.worker_enabled
            and SETTINGS.real_execution_enabled
            and SETTINGS.mirror_paper_decisions
            and SETTINGS.reference_decisions_table == "fusion222_uponly_decisions"
            and self._account is not None
            and self._configuration_error is None
        )

    def status(self) -> dict[str, Any]:
        return {
            "last_execution_diagnostics": getattr(self, "_last_execution_diagnostics", None),
            "mode": SETTINGS.execution_mode,
            "up_only": True,
            "stake_policy": POLICY,
            "worker_enabled": SETTINGS.worker_enabled,
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

    def wallet_snapshot(self, bnb_usd_price: float, *, source: str = "POLL", force: bool = False,
                        client: PancakeClient | None = None) -> dict[str, Any]:
        address = self.address
        if not address or not bnb_usd_price or bnb_usd_price <= 0:
            return dict(self._last_wallet)
        now = time.time()
        if not force and self._last_wallet and now - self._last_wallet_sync < SETTINGS.wallet_sync_seconds:
            return dict(self._last_wallet)
        balance_wei, block_number = (client or self.client)._call(
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
        amount_wei = int(raw[1])
        return {
            "amount_wei": amount_wei,
            "amount_bnb": float(Web3.from_wei(amount_wei, "ether")),
            "position": int(raw[0]),
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
        if not self.ready:
            raise RuntimeError(self._configuration_error or "wallet is not configured")
        address = self._account.address

        def build(w3: Web3):
            if int(w3.eth.chain_id) != SETTINGS.chain_id:
                raise RuntimeError("Unexpected RPC chain id")
            nonce = int(w3.eth.get_transaction_count(address, "pending"))
            if nonce != int(w3.eth.get_transaction_count(address, "latest")):
                raise RuntimeError("Wallet has a pending transaction; wait for reconciliation")
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
        if not self.ready:
            raise RuntimeError("Real execution is disarmed")
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
                gas_fee = self._gas_fee_bnb(receipt)
                db.upsert_transaction({"event_type": "BET", "betting_epoch": epoch, "tx_hash": tx_hash, "tx_status": "REVERTED", "wallet_address": self.address, "amount_bnb": 0.0, "gas_fee_bnb": gas_fee, "bnb_usd_price": bnb_usd_price, "metadata_json": {}})
                return db.update_execution(
                    epoch,
                    execution_status="FAILED",
                    reconciliation_status="TX_REVERTED",
                    bet_gas_fee_bnb=gas_fee,
                    actual_pnl_bnb=-gas_fee,
                    actual_pnl_usd=-gas_fee * float(decision.get("bnb_usd_price") or bnb_usd_price),
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
                execution_status="BLOCKED",
                execution_requested=False,
                reconciliation_status="EXTERNAL_BET_DETECTED",
                wallet_address=self.address,
                stake_bnb=ledger["amount_bnb"],
                bnb_usd_price=bnb_usd_price,
                trade_executed=False,
                execution_error="Contract already has a bet without this bot transaction hash; no duplicate or attribution",
            )
        return decision

    def execute_bet(self, decision: dict[str, Any], bnb_usd_price: float,
                    *, tick_context: dict[str, Any] | None = None) -> dict[str, Any]:
        trace = ExecutionTrace(int(decision["betting_epoch"]), tick_context)
        token = CURRENT_TRACE.set(trace)
        result = None
        error_type = None
        try:
            result = self._execute_bet(decision, bnb_usd_price)
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            CURRENT_TRACE.reset(token)
            diagnostic = trace.finish((result or {}).get("execution_status", "ERROR"), error_type)
            self._last_execution_diagnostics = diagnostic
            # Outside the signing lock and network path; diagnostics must never
            # mask a submission result or cause a replacement transaction.
            _LOG.info("execution_diagnostics %s", json.dumps(diagnostic, separators=(",", ":")))
            try:
                db.record_execution_diagnostics(int(decision["betting_epoch"]), diagnostic)
            except Exception as exc:
                _LOG.warning("execution_diagnostics_persist_failed epoch=%s type=%s",
                             decision["betting_epoch"], type(exc).__name__)

    def _execute_bet(self, decision: dict[str, Any], bnb_usd_price: float) -> dict[str, Any]:
        trace = CURRENT_TRACE.get()
        lock_started = time.monotonic()
        with self._lock, db.execution_lock(self.address or "unconfigured"):
            if trace is not None:
                trace.data["wallet_lock_wait_ms"] = round((time.monotonic() - lock_started) * 1000, 3)
            epoch = int(decision["betting_epoch"])
            decision = db.get_decision(epoch) or decision
            if not bool(decision.get("execution_requested")):
                return decision
            with span("reconcile_existing_bet"):
                reconciled = self.reconcile_bet(decision, bnb_usd_price)
            if bool(reconciled.get("trade_executed")) or reconciled.get("tx_hash") or reconciled.get("reconciliation_status") == "EXTERNAL_BET_DETECTED":
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
            if side != "UP":
                return db.update_execution(
                    epoch,
                    execution_status="FAILED",
                    execution_error=f"ONLY_UP blocks signal: {side}",
                    trade_executed=False,
                )
            reference = db.get_reference_decision(epoch)
            if not reference or not reference.get("trade_executed") or reference.get("signal") != "UP" or reference.get("breaker_applied"):
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="No eligible ONLY UP reference for this epoch")
            with span("initial_execution_snapshot"):
                snapshot = self.client.market_snapshot()
            trace.snapshot("initial", snapshot)
            rejection = window_rejection(snapshot, epoch)
            if rejection:
                trace.data["blocked_reason"] = rejection
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error=f"Round changed or betting window closed: {rejection}")
            if not (0 <= snapshot.chain_timestamp - snapshot.oracle_updated_at <= SETTINGS.oracle_max_age_seconds):
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="Stale or future BNB/USD oracle")
            bnb_usd_price = snapshot.chainlink_price
            balance_before_wei = self._balance_wei(self.address or "")
            quote = stake_quote(balance_before_wei, bnb_usd_price)
            value_wei = quote["value_wei"]
            stake_bnb = Decimal(value_wei) / Decimal(10**18)
            paused, min_bet_wei, treasury_bps = self.client._call(
                lambda w3: (
                    bool(self._contract(w3).functions.paused().call()),
                    int(self._contract(w3).functions.minBetAmount().call()),
                    int(self._contract(w3).functions.treasuryFee().call()),
                )
            )
            if paused or value_wei < min_bet_wei:
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="Contract paused or stake below contract minimum")
            reserve_wei = int(Web3.to_wei(SETTINGS.min_gas_reserve_bnb, "ether"))
            if balance_before_wei < value_wei + reserve_wei:
                return db.update_execution(epoch, execution_status="BLOCKED", stake=quote["stake_usd"], stake_tier=quote["stake_tier"], bank_before=quote["bank_usd"], execution_error="Insufficient BNB for selected tier and reserve", trade_executed=False)
            fn = lambda w3: self._contract(w3).functions.betBull(epoch)
            with span("estimate_and_sign"):
                signed, tx_hash, nonce, gas, gas_price = self._build_sign(fn, value_wei=value_wei)
            reserve_wei = int(Web3.to_wei(SETTINGS.min_gas_reserve_bnb, "ether"))
            required_wei = value_wei + gas * gas_price + reserve_wei + SETTINGS.claim_gas_budget * gas_price
            if balance_before_wei < required_wei:
                return db.update_execution(epoch, execution_status="BLOCKED", stake=quote["stake_usd"], stake_tier=quote["stake_tier"], bank_before=quote["bank_usd"], execution_error="Insufficient BNB for selected tier, bet gas, claim budget and reserve", trade_executed=False)
            # Account for our own bet's pool dilution and transaction gas. The
            # fee is already in this payout coefficient: never subtract it twice.
            amount = float(stake_bnb)
            pool = snapshot.betting_round
            corrected = (pool.total_amount_bnb + amount) / (pool.bull_amount_bnb + amount) * (1 - treasury_bps / 10000)
            corrected *= float(decision.get("payout_correction_up") or SETTINGS.payout_haircut)
            coeff = min(float(decision.get("expected_coeff_up") or 0), corrected)
            gas_fraction = (gas + SETTINGS.claim_gas_budget) * gas_price / value_wei
            ev = float(decision.get("probability_up") or 0) * coeff - 1 - gas_fraction
            if not math.isfinite(ev) or not 0 < float(decision.get("probability_up") or 0) <= 1 or not 0 <= treasury_bps <= 1000 or ev < SETTINGS.min_trade_ev:
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="EV below 2% after own-pool impact and gas", trade_executed=False)
            # Recheck after RPC estimation/signing; a changed tier is retried on
            # a later tick, without persisting or broadcasting this signed payload.
            with span("final_execution_snapshot"):
                final = self.client.market_snapshot()
            trace.snapshot("pre_send", final)
            final_balance = self._balance_wei(self.address or "")
            final_quote = stake_quote(final_balance, final.chainlink_price)
            if (final.betting_epoch != epoch or not final.decision_window
                or not 0 <= final.chain_timestamp - final.oracle_updated_at <= SETTINGS.oracle_max_age_seconds
                or final_quote["stake_tier"] != quote["stake_tier"]
                or abs(final.chainlink_price / bnb_usd_price - 1) > 0.001
                or final_balance < required_wei):
                trace.data["blocked_reason"] = window_rejection(final, epoch) or "PRE_SEND_STATE_CHANGED"
                trace.data["pre_send_checks"] = {
                    "oracle_fresh": 0 <= final.chain_timestamp - final.oracle_updated_at <= SETTINGS.oracle_max_age_seconds,
                    "tier_unchanged": final_quote["stake_tier"] == quote["stake_tier"],
                    "price_within_limit": abs(final.chainlink_price / bnb_usd_price - 1) <= 0.001,
                    "funds_sufficient": final_balance >= required_wei,
                }
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error=f"Pre-send window, wallet tier, price or funds changed: {trace.data['blocked_reason']}")
            final_pool = final.betting_round
            final_coeff = (final_pool.total_amount_bnb + amount) / (final_pool.bull_amount_bnb + amount) * (1 - treasury_bps / 10000)
            final_coeff = min(float(decision.get("expected_coeff_up") or 0), final_coeff * float(decision.get("payout_correction_up") or SETTINGS.payout_haircut))
            if float(decision.get("probability_up") or 0) * final_coeff - 1 - gas_fraction < SETTINGS.min_trade_ev:
                return db.update_execution(epoch, execution_status="BLOCKED", execution_error="Pool moved below execution EV threshold")
            balance_before_bnb = float(Web3.from_wei(balance_before_wei, "ether"))
            decision = db.update_execution(
                epoch,
                execution_status="PREPARED",
                stake=quote["stake_usd"],
                stake_mode=POLICY["mode"],
                stake_tier=quote["stake_tier"],
                bank_before=quote["bank_usd"],
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
        with self._lock, db.execution_lock(self.address or "unconfigured"):
            decision = db.get_decision(int(decision["betting_epoch"])) or decision
            if not bool(decision.get("trade_executed")):
                return decision
            epoch = int(decision["betting_epoch"])
            stake_bnb = float(decision.get("stake_bnb") or 0.0)
            bet_gas = float(decision.get("bet_gas_fee_bnb") or 0.0)
            bet_price = float(decision.get("bnb_usd_price") or bnb_usd_price)
            outcome = str(decision.get("outcome") or "").upper()
            if not bool(decision.get("settled")):
                _, refundable = self._claim_flags(epoch)
                if not refundable:
                    return decision
                decision = db.update_execution(epoch, settled=True, outcome="REFUND")
                outcome = "REFUND"
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
                    gas_fee = self._gas_fee_bnb(receipt)
                    db.upsert_transaction({"event_type": "CLAIM", "betting_epoch": epoch, "tx_hash": claim_hash, "tx_status": "REVERTED", "wallet_address": self.address, "amount_bnb": 0.0, "gas_fee_bnb": gas_fee, "bnb_usd_price": bnb_usd_price, "metadata_json": {}})
                    return db.update_execution(
                        epoch,
                        claim_gas_fee_bnb=gas_fee,
                        reconciliation_status="CLAIM_REVERTED",
                        execution_error="On-chain claim transaction reverted",
                    )
                gas_fee = self._gas_fee_bnb(receipt)
                tx = db.get_transaction(claim_hash) or {}
                before = float(tx.get("balance_before_bnb") or 0.0)
                after = self.wallet_snapshot(bnb_usd_price, source="CLAIM", force=True)
                after_bnb = float(after.get("balance_bnb") or 0.0)
                events = self.client._call(lambda w3: self._contract(w3).events.Claim().process_receipt(receipt))
                payouts = [int(e["args"]["amount"]) for e in events if int(e["args"]["epoch"]) == epoch and str(e["args"]["sender"]).lower() == str(self.address).lower() and str(e["address"]).lower() == SETTINGS.prediction_contract.lower()]
                if len(payouts) != 1:
                    return db.update_execution(epoch, reconciliation_status="CLAIM_LOG_MISSING", execution_error="Cannot verify exact claim payout")
                payout = float(Decimal(payouts[0]) / Decimal(10**18))
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
                payout = stake_bnb if outcome == "REFUND" else 0.0
                if outcome != "REFUND" and round_data.reward_base_bnb > 0 and round_data.reward_amount_bnb > 0:
                    payout = stake_bnb * round_data.reward_amount_bnb / round_data.reward_base_bnb
                pnl_bnb = payout - stake_bnb - bet_gas
                return db.update_execution(
                    epoch,
                    actual_payout_bnb=payout,
                    actual_pnl_bnb=None,
                    actual_pnl_usd=None,
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
            return db.get_decision(epoch) or decision


_EXECUTOR: LiveExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def from_env(client: PancakeClient | None = None) -> LiveExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = LiveExecutor(client)
        return _EXECUTOR
