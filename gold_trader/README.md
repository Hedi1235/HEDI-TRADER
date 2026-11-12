# XAU/USD Paper-Trading Supervisor

A Python 3.11+ research and simulation system for XAU/USD. Specialist agents
produce validated macro, technical, and supplied-headline reports; a supervisor
combines them and a deterministic risk gate can approve a simulated order.
Trade events and the paper account ledger are stored in SQLite.

The Replit runner is paper-only. A separate local Windows runner can send
orders to an MT5 **demo account only**; it refuses to start unless MT5 reports
the connected account as demo. It cannot be run from Replit's Linux process.
No live-account mode is implemented. Demo results are not a promise of future
performance or financial advice.

## Start

The project dependencies are declared in the workspace `pyproject.toml`.
Create a local `.env` from the root `.env.example`, then run the paper version:

```bash
uv run python -m gold_trader.main --once
uv run python -m gold_trader.main
```

The first command runs one cycle; the second repeats at 15-minute intervals.
Press Ctrl+C to stop. An unset FRED key, stale feed, invalid response, or
insufficient history results in a logged HOLD rather than a trade.

## Local MT5 demo runner (Windows only)

This runner connects to the MetaTrader 5 desktop terminal on the same Windows
computer. The MetaTrader5 Python package uses that terminal; this runner is not
an MT5 service hosted by Replit.

1. Install MetaTrader 5 and sign into a **demo account** in the desktop terminal.
2. On Windows, install dependencies with `uv sync --extra mt5`.
3. Copy `.env.example` to `.env` and set `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER`, and `TRADING_SYMBOL` to the demo account and the exact symbol
   shown by that broker. Keep `.env` private; do not send credentials in chat
   or commit them.
4. Set `FRED_API_KEY` in that local `.env` if macro reports are required. The
   Replit Secret does not transfer to your computer.
5. Start with one cycle: `uv run --extra mt5 python -m gold_trader.main_mt5 --once`.
   To repeat every 15 minutes, run
   `uv run --extra mt5 python -m gold_trader.main_mt5`.

Before every order, it rechecks the terminal connection and demo-account mode,
the account login, symbol visibility, the open-position limit, current quote,
broker volume/contract limits, free margin, margin level, and order-check
result. It uses the broker's own 15-minute bars and current bid/ask, not Yahoo's
GC=F proxy. Approved order requests include SL and TP; only a successful
`TRADE_RETCODE_DONE` response is counted as executed. Events and the UTC daily
opening equity are recorded in `mt5_demo_trades.db`.

The MT5 bridge has not been connected to a broker from this Linux workspace.
The Python unit tests can validate the demo guard and request construction with
a fake terminal, but only a local Windows MT5 demo terminal can verify the
broker-specific symbol, filling policy, margin, and order response.

## Data inputs

- Yahoo Finance provides 15-minute OHLC bars. `GC=F` (COMEX gold futures) is
  the default because Yahoo returned no history for its `XAUUSD=X` symbol during
  verification. Futures are a proxy, not the spot XAU/USD price; change
  `YAHOO_TICKER` if you have a provider symbol for spot. The system checks
  candle age and calculates RSI(14), EMA(50/200), normalized EMA slope angles,
  and recent support/resistance ranges.
- FRED provides DGS10, DFII10, CPIAUCSL, and DTWEXBGS observations. Add a
  `FRED_API_KEY` through Replit Secrets or a local `.env`; do not commit it.
  DTWEXBGS is a broad trade-weighted dollar proxy, not the ICE DXY index.
- Geopolitical analysis only reads headlines explicitly supplied in
  `news/headlines.json`, as either `["headline"]` or `{"headlines":["headline"]}`.
  No news feed is scraped or implied. Without headlines, that report is marked
  unknown.

Market and macro-data access can fail or be delayed. The program records the
reason and holds; it does not substitute synthetic prices or macro values.

## Risk and execution

- Maximum account risk per new trade is 1%, sized from stop distance, contract
  size, and account equity. The MT5 runner uses the broker's contract and
  volume-step specifications.
- The daily circuit breaker rejects proposals once equity is down 3% or more
  from the UTC day's opening equity.
- Stop-loss and take-profit levels are mandatory; at most one position is open
  by default. The Replit ledger labels trades `PAPER`; MT5 order records are
  stored separately and only after the account is confirmed as a demo account.
- If both a stop and target are inside a candle, the simulator assumes the
  stop was hit first. This conservative rule avoids optimistic assumptions
  when intrabar tick order is unavailable.
- Daily start equity, realized/unrealized P&L, order decisions, rejections, and
  simulated exits are saved to `trading_bot.db`.
- MT5 demo order checks, execution responses, tickets, and daily start equity
  are saved to `mt5_demo_trades.db`.

The agent scores are transparent deterministic heuristics, not LLM forecasts.
The geopolitical classifier is keyword-based and should not be treated as a
reliable news or event-risk service.

## Configuration

See the root `.env.example`. Limits are validated at startup: per-trade risk
cannot exceed 1%, and daily drawdown cannot exceed 3%. To reset the paper
account, stop the program and remove `trading_bot.db`; this permanently deletes
its simulated account and trade history. Do not remove the MT5 demo ledger while
the local runner is active; it stores the daily drawdown baseline.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
