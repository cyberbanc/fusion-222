# FUSION-222 v1.0.5 — NO BREAKER

Independent PAPER bot for PancakeSwap Prediction using the Fusion EV ensemble and the current gate:

- selected EV >= 2%
- selected-side probability >= 53%
- payout bucket must be ready
- Shadow Recent remains active (`8`, minimum PnL `-30`)
- Quality Profit Factor remains active (`>= 0.85`)
- Quality Win Rate is calculated/displayed but does NOT block trades
- **circuit breaker removed: no trade is skipped because of prior losses**
- fixed stake: **$22**

## Database architecture

FUSION-222 uses the **same PostgreSQL database as the main `fusion-ev` bot**.

It auto-detects the existing main Fusion decisions table and round-history table. The historical main Fusion decisions table is read-only for FUSION-222. New v1.0.5 records are isolated in:

- `fusion222_v1366_nobreaker_decisions`
- `fusion222_v1366_nobreaker_state`
- `fusion222_v1366_nobreaker_snapshots`

These new table names are intentional. v1.0.4 already initialized retro accounting with the 3x3 breaker, so reusing the old state table would preserve the wrong 149-trade retro state. v1.0.5 starts a clean NO-BREAKER replay without deleting or altering the old tables.

## Retro initialization — v1.3.6.6 ONLY

On first successful startup, FUSION-222 replays **only rows where `strategy_version = 1.3.6.6`** from the main Fusion database and applies the v1.0.5 NO-BREAKER rules. The virtual bank starts at `$500`. Other historical versions do not enter dashboard Bank/PnL/PF/DD.

The dashboard timer remains anchored to the first stored `1.3.6.6` decision. After initialization, the retro cutoff is frozen and new FUSION-222 paper trades continue from the reconstructed virtual state.

Using the supplied 2026-08-11 v1.3.6.6 snapshot, exact NO-BREAKER validation is:

- 168 trades
- 93 WIN / 75 LOSS
- 55.3571% win rate
- PnL **+$581.063701**
- bank **$1,081.063701** from $500
- PF **1.352160**
- Max DD **$123.417403**
- min bank **$451.857755**
- peak bank **$1,186.318630**
- max loss streak 4

Numbers can move slightly if the main `1.3.6.6` table receives additional settled rows before the first v1.0.5 boot.

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

## v1.0.5 change

The 3-loss / skip-3-signals circuit breaker is removed from both historical replay and forward paper trading. EV/probability/payout/Shadow/Quality-PF filters remain unchanged. Fixed stake remains $22. v1.0.4 startup hardening and PostgreSQL auto-detection fixes are retained.
