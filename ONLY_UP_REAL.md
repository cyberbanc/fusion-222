# FUSION-222 ONLY UP real bot · v2

This branch adapts the existing disarmed real service. Paper services, paper
history and the previous real bot tables remain intact. New writes use only
`fusion222_uponly_real_*`, including an isolated rounds cache. Signals are read
from exactly `fusion222_uponly_decisions`; a missing signal never triggers a
standalone forecast. DOWN cannot reach the signing path.

## Stake contract

| Liquid native BNB valued by current Chainlink BNB/USD | USD stake |
| --- | --- |
| Below 1000 | 22 |
| 1000 inclusive to 2000 exclusive | 44 |
| 2000 inclusive and above | 88 |

The rule is stateless in both directions; 88 is the maximum. The tier bank is
liquid native BNB, before gas reserve deduction. Locked bets, unclaimed rewards,
other tokens, paper profits and the legacy virtual 500-dollar state are excluded.
The signed BNB amount is rounded down to integer wei. USD equivalence is based
on the decision-time oracle, not a guaranteed USD amount at block inclusion.

Before each send: fresh wallet balance and oracle (maximum age 60 seconds), same
round and 8–40 second betting window, contract paused/minBetAmount/treasuryFee,
correct chain 56, available nonce, own-pool dilution, corrected payout and gas
adjusted EV >= 2%. Full selected stake plus estimated bet gas, a 250k claim-gas
budget and 0.003 BNB reserve must fit. An unaffordable tier is skipped, not shrunk.
Window, tier, price and funds are checked again after estimation/signing. A
changed tier discards the unsigned-to-network payload and waits for another tick.

A PostgreSQL session advisory lock serializes bet and claim signing for the
wallet. Hash and nonce are persisted before broadcast. Any known hash, pending
submission, or existing contract bet prevents a new bet. An external bet is
flagged and never counted as a bot bet. Uncertain submissions remain pending
for reconciliation; the bot does not invent a replacement.

## Real accounting

Only confirmed receipt/contract operations count as real trades. Claim payout
comes from the exact contract Claim log for wallet and epoch. Bet gas, claim gas
and reverted bet gas are recorded. External claims with unknown gas remain
unreconciled. Equal lock/close prices are a house win (loss), not a refund;
actual cancelled-round refunds are checked through `refundable`.
USD PnL values BNB stake/gas at bet price and payout/claim gas at claim price.
BNB PnL is also exposed. Deposits, withdrawals and BNB repricing are wallet
movements, never claimed as trading PnL. No retrospective trades initialize the
real history. Paper PnL columns are not real accounting; Sites uses only
`actual_*` fields.

## Deployment / owner controls

Production must initially use:

```
WORKER_ENABLED=false
REAL_EXECUTION_ENABLED=false
AUTO_CLAIM_ENABLED=false
EXECUTION_MODE=REAL
MIRROR_PAPER_DECISIONS=true
REFERENCE_DECISIONS_TABLE=fusion222_uponly_decisions
RETRO_REPLAY_ENABLED=false
```

The existing signing secret remains only in Railway. Do not paste it into chat,
Git, Sites or logs. Verify the wallet address and chain, reserve, oracle,
reference bot health and enough liquid BNB before the owner enables the service
in Railway. `WORKER_ENABLED` and `REAL_EXECUTION_ENABLED` must both be true for
signing; `AUTO_CLAIM_ENABLED` additionally governs claim submission. No HTTP
endpoint or Sites button can arm or execute bets. `/preflight` is read-only to
chain (it stores a balance observation); it works while trading is disarmed.

The Sites dashboard polls every 15 seconds. It shows real BNB/USD bank, next
tier, gas, confirmed PnL, reconciliation state and the last 50 bot decisions.
It does not contain the signing secret or any transaction-execution route.

Local verification uses mocked chain/DB objects, never a real wallet transaction.
The real signed/broadcast path is not production-money tested. Model probability
and current pool EV cannot guarantee future profit or the final locked payout.
