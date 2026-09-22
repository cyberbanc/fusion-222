# FUSION-222 REAL v1.0.0

Isolated real-money PancakeSwap Prediction service. It mirrors each exact,
already-locked production paper decision without changing the paper service:

- selected EV >= 2%
- selected-side probability >= 53%
- payout bucket ready
- Shadow Recent: 8 trades, minimum PnL -30
- Quality PF >= 0.85; Quality WR is display-only
- no circuit breaker
- fixed stake $22, converted to BNB using the decision-time Chainlink price

## Isolation

The paper service stays on branch `main`. This real service runs from branch
`live-real` and writes only these tables:

- `fusion222_real_decisions`
- `fusion222_real_state`
- `fusion222_real_snapshots`
- `fusion222_real_transactions`
- `fusion222_real_wallet_snapshots`

The live service reads `fusion222_v1366_nobreaker_decisions` as its decision
source and rolling Shadow/Quality reference. It waits for the paper row and does
not independently recalculate the signal. It never updates or deletes paper
history.

## Real execution and reconciliation

- The wallet address is derived from `WALLET_PRIVATE_KEY` and optionally checked
  against `WALLET_ADDRESS`.
- The private key is never returned by the API or written to PostgreSQL.
- Before every send, the bot checks the PancakeSwap `ledger(epoch, wallet)`.
- The deterministic signed transaction hash is stored before broadcast.
- A restart reconciles the stored tx hash and contract ledger instead of sending
  a duplicate bet.
- Winning/refundable epochs are claimed automatically.
- Bet/claim value, gas, before/after balances and tx hashes are stored separately.
- Wallet snapshots expose the actual on-chain BNB balance and its USD value.
- The dashboard reports observed wallet change, known bot cash flow and any
  external/unreconciled difference.

## API

- `/dashboard`
- `/healthz`
- `/health`
- `/signal`
- `/status?history=none`
- `/history/live?limit=100`
- `/history/export-real.csv`
- `/transactions?limit=100`
- `/transactions/export.csv`

## Activation

Deploy with `WORKER_ENABLED=false` first. Add `WALLET_PRIVATE_KEY` directly in
Railway (never in chat or GitHub), optionally add `WALLET_ADDRESS`, verify
`/health` and the displayed address/balance, then set `WORKER_ENABLED=true`.
