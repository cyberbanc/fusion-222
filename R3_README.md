# FUSION R3 paper bot

Independent branch/service for public OKX USDT perpetual market data and simulated execution. It has no exchange order endpoint, exchange account credentials, or wallet signing code.

32 independent $500 virtual accounts: 24 existing SOL/ETH × 5/15/30/60 minute × long-only/long+short/short-only accounts and 8 new TREND accounts. Trend accounts start at activation with no backfilled performance. From v1.2, new paper entries target +$22 after modeled fees/slippage and plan -$22/3 at the stop, before future funding. Size is derived from 1.5 ATR(14), rounded down to the contract step; the stop is then adjusted for the rounding to retain the dollar risk. The margin and entry fee must fit 90% of bank at the unchanged 2x leverage cap, otherwise the entry is skipped without reducing the cash target. Maximum holding approximately 4 hours. Fee assumption 0.05% each side, adverse slippage 0.01% each side. Published funding rates are included with minute-close notional approximation. Funding, gaps, timeout and polling delay can change actual results. Shadow trials retain normalized $1 risk. Existing positions keep their original levels and history.

Signals use weighted price / microstructure / crowd blocks 40/25/35 and shrink 0.75. The crowd block uses account long/short ratio, funding and changes in open interest. It is not a capital pool probability.

The separate v1.3 TREND profile uses closed own-timeframe candles: EMA20/50, slow EMA slope over 5 candles >=0.05 ATR in magnitude, matching +DI/-DI and ADX14 >=25 establish UP/LONG or DOWN/SHORT. ADX<=20 marks flat. A flat previous candle followed by a close beyond the prior 20-candle high/low plus/minus 0.1 ATR is BREAKOUT_UP/LONG or BREAKOUT_DOWN/SHORT. The signal candle is excluded from the channel. Mixed/20-25 conditions wait. Its forced direction cannot be overridden by opposite-side EV. It has separate bidirectional shadow trials and 15-closed-sample calibration per side. The same probability, EV, quality, cost, stale-data and margin gates apply. See `TREND_METHODS_RU.md` for exact rules, examples, method families and primary references.

The probability of TP before SL/timeout is learned only from completed live shadow trials at the same symbol and timeframe with nearby weighted scores. A 25% prior, 15-sample warmup per direction, p>=53%, EV>=0.02R, recent Shadow -3R/8 and quality PF>=0.85 are explicit. Warmup is visible. No old up/down probability or fabricated historical microstructure is labelled as a TP probability.

Full history is stored exclusively in `fusion_r3_v1_records` and four indexes with the `fusion_r3_` prefix. Existing PancakeSwap tables and histories are not modified. PostgreSQL session advisory lock prevents concurrent workers. All writes for a source tick are transactional; signal keys make decisions idempotent.

Public read-only API:
- `/health`, `/api/summary`
- `/api/history?account=SOL_15_LONG&kind=decision`
- `/api/equity?account=SOL_15_LONG`
- `/api/studies?account=SOL_15_LONG`
- `/exports/history.zip` — complete repeatable-read archive, all accounts, JSONL/CSV, model configuration, source records, candles, studies, counts and SHA256. No UI pagination limit is used for export.

Every actual paper entry has 16 parallel exit studies: TP=1.5/2/2.5/3R × SL=.75/1/1.25/1.5R on the same quantity and entry. Studies continue after the main position closes up to the original 4h horizon. These are paired exit outcomes, not a full alternate-strategy backtest: earlier exits can permit other future entries in a true replay.

Confirmed MFE/MAE, observed milestones, price progress toward TP, possible candle bounds, and 80%-to-TP reversals are separate fields. If a minute contains both barriers, stop is assumed first. The closing minute's other extremes are marked ambiguous rather than assumed to occur before exit. The entry minute uses only observed quotes. During outages complete minute candles are paginated back; gaps freeze causal replay and new account entries.

Environment: `DATABASE_URL`, `TRADING_MODE=PAPER`, `WORKER_ENABLED=true`, `ALLOWED_ORIGIN`, `POLL_SECONDS=20`, `TARGET_NET_PROFIT_USD=22`, `MODEL_VERSION=FUSION-R3-v1.3.0`. No private keys are needed or read.

Run `python -m unittest discover -s tests -v` and `python -m r3_bot`. Docker installs psycopg only; all other runtime modules are Python standard library.
