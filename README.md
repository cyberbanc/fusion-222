# FUSION-222

Independent PAPER bot for PancakeSwap Prediction using the Fusion EV ensemble and the new gate:

- selected EV >= 2%
- selected-side probability >= 53%
- payout bucket must be ready
- Shadow Recent remains active (`8`, minimum PnL `-30`)
- Quality Profit Factor remains active (`>= 0.85`)
- Quality Win Rate is calculated/displayed but does NOT block trades
- after 3 executed losses, skip the next 3 otherwise-eligible signals
- fixed stake: **$22**

## Database architecture

FUSION-222 uses the **same PostgreSQL database as the main `fusion-ev` bot**.

It reads the historical main Fusion decisions from `paper_decisions` and round history from `round_history`, but writes its own records only to:

- `fusion222_v1366_decisions`
- `fusion222_v1366_state`
- `fusion222_v1366_snapshots`

The main `paper_decisions` history is read-only for FUSION-222. The corrected private table names are intentionally different from the earlier draft so an accidentally initialized ALL_VERSIONS state cannot contaminate the corrected accounting.

## Retro initialization — v1.3.6.6 ONLY

On the first successful startup, FUSION-222 replays **only rows where `strategy_version = 1.3.6.6`** from the main Fusion database, applies the new FUSION-222 rules, and initializes a virtual bank from `$500`. It does **not** use legacy/1.2/1.3.1/1.3.2/1.3.3/1.3.6.4 for the dashboard bank, PnL, PF or drawdown.

The dashboard timer is anchored to the first stored `1.3.6.6` decision. After initialization, the v1.3.6.6 retro cutoff is frozen and all new FUSION-222 paper trades continue from that reconstructed virtual state.

Using the supplied 2026-08-11 v1.3.6.6 snapshot, the exact validation result is:

- 149 trades
- 84 WIN / 65 LOSS
- 56.3758% win rate
- PnL +$590.385622
- bank $1,090.385622 from $500
- PF 1.412857
- Max DD $109.713372
- min bank $447.796731
- peak bank $1,156.385622

The timer starts at the first v1.3.6.6 stored decision (`2026-07-23T09:35:57Z`), therefore it is around 19 days at 2026-08-12. Numbers can move slightly if the main v1.3.6.6 table receives additional settled rows before the first FUSION-222 boot.

## API

- `/healthz`
- `/health`
- `/signal`
- `/status?history=none`
- `/history/combined?limit=100`
- `/history/retro?limit=100`
- `/history/live?limit=100`
- `/history/export-combined.csv`
- `/history/export-retro.csv`
- `/history/export-live.csv`
- `/shadow/performance`
