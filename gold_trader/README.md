# XAU/USD Paper-Trading Supervisor

A Python 3.11+ research and simulation system for XAU/USD. Specialist agents
produce validated macro, technical, and supplied-headline reports; a supervisor
combines them and a deterministic risk gate can approve a simulated order.
Trade events and the paper account ledger are stored in SQLite.

**This project cannot place live trades.** There is no MT5, Alpaca, or other
broker connection in the execution module. Do not use paper results as a
promise of future performance or as financial advice.

## Start

The project dependencies are declared in the workspace `pyproject.toml`.
Create a local `.env` from the root `.env.example`, then run:

```bash
uv run python -m gold_trader.main --once
uv run python -m gold_trader.main
```

The first command runs one cycle; the second repeats at 15-minute intervals.
Press Ctrl+C to stop. An unset FRED key, stale feed, invalid response, or
insufficient history results in a logged HOLD rather than a trade.

## Data inputs

- Yahoo Finance provides the configured XAU/USD 15-minute OHLC bars (`XAUUSD=X`
  by default). The system checks candle age and calculates RSI(14), EMA(50/200),
  normalized EMA slope angles, and recent support/resistance ranges.
- FRED provides DGS10, DFII10, CPIAUCSL, and DTWEXBGS observations. Add a
  `FRED_API_KEY` through Replit Secrets or a local `.env`; do not commit it.
  DTWEXBGS is a broad trade-weighted dollar proxy, not the ICE DXY index.
- Geopolitical analysis only reads headlines explicitly supplied in
  `news/headlines.json`, as either `["headline"]` or `{"headlines":["headline"]}`.
  No news feed is scraped or implied. Without headlines, that report is marked
  unknown.

Market and macro-data access can fail or be delayed. The program records the
reason and holds; it does not substitute synthetic prices or macro values.

## Risk and paper execution

- Maximum account risk per new trade is 1%, sized from stop distance, contract
  size, and the simulated account equity.
- The daily circuit breaker rejects proposals once equity is down 3% or more
  from the UTC day's opening equity.
- Stop-loss and take-profit levels are mandatory; at most one position is open
  by default. The execution ledger labels every trade `PAPER`.
- If both a stop and target are inside a candle, the simulator assumes the
  stop was hit first. This conservative rule avoids optimistic assumptions
  when intrabar tick order is unavailable.
- Daily start equity, realized/unrealized P&L, order decisions, rejections, and
  simulated exits are saved to `trading_bot.db`.

The agent scores are transparent deterministic heuristics, not LLM forecasts.
The geopolitical classifier is keyword-based and should not be treated as a
reliable news or event-risk service.

## Configuration

See the root `.env.example`. Limits are validated at startup: per-trade risk
cannot exceed 1%, and daily drawdown cannot exceed 3%. To reset the paper
account, stop the program and remove `trading_bot.db`; this permanently deletes
its simulated account and trade history.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
