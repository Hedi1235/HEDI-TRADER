"""15-minute analysis loop. All orders remain simulated in SQLite."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from gold_trader.agents import (
    GeopoliticalRiskAgent,
    HeadTraderAgent,
    MacroYieldAgent,
    TechnicalAnalystAgent,
)
from gold_trader.config import Settings
from gold_trader.data_feeds import (
    DataUnavailable,
    FredClient,
    fetch_market_snapshot,
    load_headlines,
)
from gold_trader.execution import PaperExecutionEngine
from gold_trader.risk_manager import RiskManager

LOGGER = logging.getLogger("gold_trader")


class ColorFormatter(logging.Formatter):
    converter = time.gmtime

    colors = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    reset = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        color = self.colors.get(record.levelno)
        return f"{color}{rendered}{self.reset}" if color else rendered


def configure_logging(log_path: Path = Path("trading_bot.log")) -> None:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        ColorFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S UTC",
        )
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    file_formatter.converter = time.gmtime
    file_handler.setFormatter(file_formatter)
    for existing_handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(existing_handler)
        existing_handler.close()
    LOGGER.addHandler(console_handler)
    LOGGER.addHandler(file_handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def _hold(
    executor: PaperExecutionEngine,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"reason": reason, **(detail or {})}
    executor.record_event("HOLD", payload)
    LOGGER.warning("Cycle ended in HOLD: %s", reason)
    return {"status": "HOLD", "reason": reason}


def run_cycle(
    settings: Settings | None = None,
    executor: PaperExecutionEngine | None = None,
) -> dict[str, Any]:
    """Run one data → agents → risk gate → paper execution cycle."""

    settings = settings or Settings.from_env()
    executor = executor or PaperExecutionEngine(settings)
    try:
        market = fetch_market_snapshot(settings)
        for closed_trade in executor.update_open_positions(market.latest_bar):
            LOGGER.info(
                "Paper trade %s closed at %.2f (%s, P&L %.2f)",
                closed_trade["trade_id"],
                closed_trade["exit_price"],
                closed_trade["status"],
                closed_trade["pnl"],
            )

        macro_snapshot = FredClient(settings.fred_api_key).macro_snapshot()
        try:
            headlines = load_headlines(settings.headlines_path)
        except DataUnavailable as exc:
            LOGGER.warning("%s; geopolitical input marked unknown", exc)
            headlines = []

        macro_report = MacroYieldAgent().analyze(macro_snapshot)
        technical_report = TechnicalAnalystAgent().analyze(market)
        geopolitical_report = GeopoliticalRiskAgent().analyze(headlines)
        decision = HeadTraderAgent(settings).decide(
            macro_report,
            technical_report,
            geopolitical_report,
        )

        LOGGER.info(
            "Agents: macro=%s (%.2f/%.0f%%) technical=%s (%.2f/%.0f%%) "
            "geopolitical=%s/%s (%.2f/%.0f%%); supervisor=%s "
            "(score %.2f, confidence %.0f%%)",
            macro_report.bias,
            macro_report.score,
            macro_report.confidence * 100,
            technical_report.bias,
            technical_report.score,
            technical_report.confidence * 100,
            geopolitical_report.risk_level,
            geopolitical_report.bias,
            geopolitical_report.score,
            geopolitical_report.confidence * 100,
            decision.decision,
            decision.score,
            decision.confidence * 100,
        )
        executor.record_event(
            "SUPERVISOR_DECISION",
            {
                "decision": decision.decision,
                "score": decision.score,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "macro_bias": macro_report.bias,
                "macro_score": macro_report.score,
                "macro_confidence": macro_report.confidence,
                "technical_bias": technical_report.bias,
                "technical_score": technical_report.score,
                "technical_confidence": technical_report.confidence,
                "geopolitical_bias": geopolitical_report.bias,
                "geopolitical_score": geopolitical_report.score,
                "geopolitical_confidence": geopolitical_report.confidence,
                "market_timestamp": market.latest_bar.timestamp.isoformat(),
            },
        )

        if decision.decision == "HOLD":
            executor.record_event(
                "HOLD",
                {"reason": decision.rationale, "score": decision.score},
            )
            return {"status": "HOLD", "reason": decision.rationale}

        account = executor.account_summary()
        day_start = executor.daily_opening_equity()
        result = RiskManager(settings).validate(
            decision,
            equity=account.equity,
            daily_start_equity=day_start,
            open_positions=account.open_positions,
        )
        if not result.approved or result.trade is None:
            executor.record_event(
                "REJECT",
                {
                    "reason": result.reason,
                    "daily_drawdown_pct": result.daily_drawdown_pct,
                    "proposed_risk": result.risk_amount,
                },
            )
            LOGGER.warning("Risk gate rejected proposal: %s", result.reason)
            return {"status": "REJECT", "reason": result.reason}

        trade_id = executor.open_paper_trade(result.trade)
        LOGGER.info(
            "PAPER order %s: %s %.2f lots at %.2f, SL %.2f, TP %.2f, risk %.2f",
            trade_id,
            result.trade.side,
            result.trade.lots,
            result.trade.entry_price,
            result.trade.stop_loss,
            result.trade.take_profit,
            result.trade.risk_amount,
        )
        return {
            "status": "PAPER_ORDER_OPENED",
            "trade_id": trade_id,
            "side": result.trade.side,
            "lots": result.trade.lots,
            "risk_amount": result.trade.risk_amount,
        }
    except DataUnavailable as exc:
        return _hold(executor, str(exc), {"ticker": settings.yahoo_ticker})
    except Exception as exc:
        LOGGER.exception("Unexpected cycle error; no order was submitted")
        return _hold(executor, f"unexpected_error:{type(exc).__name__}")


def _positive_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be an integer number of minutes") from exc
    if minutes <= 0:
        raise argparse.ArgumentTypeError("interval must be greater than zero")
    return minutes


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the XAU/USD paper-trading supervisor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single analysis cycle instead of repeating",
    )
    parser.add_argument(
        "--interval-minutes",
        type=_positive_minutes,
        help="override the configured polling interval (default: 15)",
    )
    args = parser.parse_args()
    configure_logging()
    try:
        settings = Settings.from_env()
        if args.interval_minutes is not None:
            settings = replace(settings, interval_minutes=args.interval_minutes)
    except ValueError as exc:
        LOGGER.error("Invalid configuration: %s", exc)
        return 2

    executor = PaperExecutionEngine(settings)
    LOGGER.info(
        "Started paper mode only for Yahoo %s; interval=%d minutes, database=%s",
        settings.yahoo_ticker,
        settings.interval_minutes,
        settings.database_path,
    )
    if not settings.fred_api_key:
        LOGGER.warning("FRED_API_KEY is unset; macro data is unavailable and cycles will HOLD")

    if args.once:
        run_cycle(settings, executor)
        return 0

    while True:
        started = time.monotonic()
        try:
            run_cycle(settings, executor)
        except Exception:
            LOGGER.exception("Analysis cycle escaped its error boundary; waiting for next cycle")
        remaining = max(0.0, settings.interval_minutes * 60 - (time.monotonic() - started))
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            LOGGER.info("Stopped by user; paper database was closed cleanly")
            return 0


if __name__ == "__main__":
    sys.exit(main())
