# FUSION R3 paper bot

Independent branch/service for public OKX USDT perpetual market data and simulated execution. It has no exchange order endpoint, exchange account credentials, or wallet signing code.

24 active independent $500 virtual accounts: SOL/ETH × 5/15/30/60 minute × long-only/long+short/short-only. From v1.3.1 the earlier 8 TREND accounts are retired from new entries and the dashboard; their complete history is preserved in the export. Trend scoring runs in the separate fusion-trend-paper service/branch paper-trend-score with its own table, history and dashboard at https://fusion-trend-lab.cyberbanc.chatgpt.site. No previous classic accounts are reset. From v1.2, new paper entries target +$22 after modeled fees/slippage and plan -$22/3 at the stop, before future funding. Size is derived from 1.5 ATR(14), rounded down to the contract step; the stop is then adjusted for the rounding to retain the dollar risk. The margin and entry fee must fit 90% of bank at the unchanged 2x leverage cap, otherwise the entry is skipped without reducing the cash target. Maximum holding approximately 4 hours. Fee assumption 0.05% each side, adverse slippage 0.01% each side. Published funding rates are included with minute-close notional approximation. Funding, gaps, timeout and polling delay can change actual results. Shadow trials retain normalized $1 risk. Existing positions keep their original levels and history.

Signals use weighted price / microstructure / crowd blocks 40/25/35 and shrink 0.75. The crowd block uses account long/short ratio, funding and changes in open interest. It is not a capital pool probability.

Archived v1.3 TREND profile: closed own-timeframe EMA20/50, ADX14/DMI and prior-20-bar breakout with 0.1 ATR buffer. It is no longer scheduled by process(). Its original code remains replayable for history verification; original pending observations can finish. See TREND_METHODS_RU.md for the historical rules. The new score ensemble is documented on branch paper-trend-score.

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

Environment: `DATABASE_URL`, `TRADING_MODE=PAPER`, `WORKER_ENABLED=true`, `ALLOWED_ORIGIN`, `POLL_SECONDS=20`, `TARGET_NET_PROFIT_USD=22`, `MODEL_VERSION=FUSION-R3-v1.3.1`. No private keys are needed or read.

Run `python -m unittest discover -s tests -v` and `python -m r3_bot`. Docker installs psycopg only; all other runtime modules are Python standard library.
