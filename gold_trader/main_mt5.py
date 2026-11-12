"""Local Windows runner for demo-account MT5 execution."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

from gold_trader.agents import (
    GeopoliticalRiskAgent,
    HeadTraderAgent,
    MacroYieldAgent,
    TradeDecision,
    TechnicalAnalystAgent,
)
from gold_trader.config import Settings
from gold_trader.data_feeds import DataUnavailable, FredClient, load_headlines
from gold_trader.execution_mt5 import MT5DemoError, MT5DemoExecution
from gold_trader.main import configure_logging
from gold_trader.risk_manager import RiskManager

LOGGER = logging.getLogger("gold_trader")


def _use_current_broker_quote(
    decision: TradeDecision,
    *,
    bid: float,
    ask: float,
    settings: Settings,
) -> TradeDecision:
    if decision.decision == "HOLD":
        return decision
    entry = ask if decision.decision == "BUY" else bid
    if decision.decision == "BUY":
        stop_loss = entry - settings.default_stop_loss_usd
        take_profit = entry + settings.default_take_profit_usd
    else:
        stop_loss = entry + settings.default_stop_loss_usd
        take_profit = entry - settings.default_take_profit_usd
    return TradeDecision.model_validate(
        {
            **decision.model_dump(),
            "entry_price": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
    )


def run_cycle(settings: Settings, executor: MT5DemoExecution) -> dict[str, object]:
    """Analyze one broker-symbol cycle and send only a demo-account order."""

    try:
        account = executor.account_snapshot()
        if not account.trade_allowed:
            reason = "MT5 terminal or account has disabled algorithmic trading"
            executor.record_event("HOLD", {"reason": reason})
            LOGGER.warning("Cycle ended in HOLD: %s", reason)
            return {"status": "HOLD", "reason": reason}

        market_data = executor.market_data()
        macro = FredClient(settings.fred_api_key).macro_snapshot()
        try:
            headlines = load_headlines(settings.headlines_path)
        except DataUnavailable as exc:
            LOGGER.warning("%s; geopolitical input marked unknown", exc)
            headlines = []

        macro_report = MacroYieldAgent().analyze(macro)
        technical_report = TechnicalAnalystAgent().analyze(market_data.snapshot)
        geopolitical_report = GeopoliticalRiskAgent().analyze(headlines)
        decision = HeadTraderAgent(settings).decide(
            macro_report,
            technical_report,
            geopolitical_report,
        )
        LOGGER.info(
            "MT5 demo agents: macro=%s technical=%s geopolitical=%s/%s; "
            "supervisor=%s (score %.2f, confidence %.0f%%)",
            macro_report.bias,
            technical_report.bias,
            geopolitical_report.risk_level,
            geopolitical_report.bias,
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
                "symbol": settings.trading_symbol,
                "bar_timestamp": market_data.snapshot.latest_bar.timestamp.isoformat(),
            },
        )
        if decision.decision == "HOLD":
            executor.record_event("HOLD", {"reason": decision.rationale})
            return {"status": "HOLD", "reason": decision.rationale}

        # Base levels on the same broker quote that the order will use. Never
        # size an XAUUSD order from the Yahoo GC=F futures proxy.
        decision = _use_current_broker_quote(
            decision,
            bid=market_data.bid,
            ask=market_data.ask,
            settings=settings,
        )
        positions = executor.positions()
        daily_start_equity = executor.daily_opening_equity(account)
        risk = RiskManager(settings).validate(
            decision,
            equity=account.equity,
            daily_start_equity=daily_start_equity,
            open_positions=len(positions),
            contract_size_ounces=market_data.contract_size,
            minimum_lot=market_data.volume_min,
            lot_step=market_data.volume_step,
            maximum_lots=market_data.volume_max,
        )
        if not risk.approved or risk.trade is None:
            executor.record_event(
                "RISK_REJECTED",
                {
                    "reason": risk.reason,
                    "daily_drawdown_pct": risk.daily_drawdown_pct,
                    "risk_amount": risk.risk_amount,
                    "symbol": settings.trading_symbol,
                },
            )
            LOGGER.warning("Risk gate rejected MT5 demo proposal: %s", risk.reason)
            return {"status": "REJECT", "reason": risk.reason}

        outcome = executor.send_mt5_order(risk.trade)
        if not outcome.success:
            LOGGER.warning("MT5 demo order was not executed: %s", outcome.reason)
            return {
                "status": "REJECT",
                "reason": outcome.reason,
                "retcode": outcome.retcode,
            }
        LOGGER.info(
            "MT5 DEMO order completed: ticket=%s deal=%s %s %.2f lots at %.2f, "
            "SL %.2f, TP %.2f, planned risk %.2f",
            outcome.order_ticket,
            outcome.deal_ticket,
            risk.trade.side,
            outcome.volume or risk.trade.lots,
            outcome.fill_price or risk.trade.entry_price,
            risk.trade.stop_loss,
            risk.trade.take_profit,
            risk.trade.risk_amount,
        )
        return {
            "status": "MT5_DEMO_ORDER_EXECUTED",
            "order_ticket": outcome.order_ticket,
            "deal_ticket": outcome.deal_ticket,
            "fill_price": outcome.fill_price,
            "volume": outcome.volume,
            "risk_amount": risk.trade.risk_amount,
        }
    except (DataUnavailable, MT5DemoError) as exc:
        executor.record_event("HOLD", {"reason": str(exc)})
        LOGGER.warning("Cycle ended in HOLD: %s", exc)
        return {"status": "HOLD", "reason": str(exc)}
    except Exception as exc:
        LOGGER.exception("Unexpected MT5 demo cycle error; no new order was attempted")
        reason = f"unexpected_error:{type(exc).__name__}"
        executor.record_event("HOLD", {"reason": reason})
        return {"status": "HOLD", "reason": reason}


def _positive_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be an integer number of minutes") from exc
    if minutes <= 0:
        raise argparse.ArgumentTypeError("interval must be greater than zero")
    return minutes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local, demo-account-only MetaTrader 5 gold supervisor"
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
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
            from dataclasses import replace

            settings = replace(settings, interval_minutes=args.interval_minutes)
        if not settings.mt5_credentials_configured:
            raise MT5DemoError(
                "Add MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER to the local .env file"
            )
        executor = MT5DemoExecution(settings)
    except (MT5DemoError, ValueError) as exc:
        LOGGER.error("MT5 demo runner did not start: %s", exc)
        return 2

    LOGGER.info(
        "Started MT5 DEMO-only runner for %s; interval=%d minutes",
        settings.trading_symbol,
        settings.interval_minutes,
    )
    if not settings.fred_api_key:
        LOGGER.warning("FRED_API_KEY is unset; macro data is unavailable and cycles will HOLD")

    try:
        if args.once:
            run_cycle(settings, executor)
            return 0
        while True:
            started = time.monotonic()
            run_cycle(settings, executor)
            remaining = max(0.0, settings.interval_minutes * 60 - (time.monotonic() - started))
            time.sleep(remaining)
    except KeyboardInterrupt:
        LOGGER.info("Stopped MT5 demo loop by user")
        return 0
    finally:
        executor.shutdown()


if __name__ == "__main__":
    sys.exit(main())
