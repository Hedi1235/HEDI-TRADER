"""Local Windows runner for MetaTrader 5 execution."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

from gold_trader.agents import (
    GeopoliticalRiskAgent,
    HeadTraderAgent,
    MacroReport,
    MacroYieldAgent,
    TradeDecision,
    TechnicalAnalystAgent,
)
from gold_trader.config import Settings
from gold_trader.data_feeds import DataUnavailable, FredClient, load_headlines
from gold_trader.execution_mt5 import MT5Execution, MT5ExecutionError
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


def run_cycle(settings: Settings, executor: MT5Execution) -> dict[str, object]:
    """Analyze one broker-symbol cycle and pass orders through deterministic risk checks."""

    try:
        account = executor.account_snapshot()
        if not account.trade_allowed:
            reason = "MT5 terminal or account has disabled algorithmic trading"
            executor.record_event("HOLD", {"reason": reason})
            LOGGER.warning("Cycle ended in HOLD: %s", reason)
            return {"status": "HOLD", "reason": reason}

        market_data = executor.market_data()
        try:
            macro = FredClient(settings.fred_api_key).macro_snapshot()
            macro_report = MacroYieldAgent().analyze(macro)
        except DataUnavailable as exc:
            LOGGER.warning("FRED unavailable (%s); macro input marked neutral", exc)
            macro_report = MacroReport(
                agent="macro_yield",
                bias="neutral",
                score=0.0,
                confidence=0.0,
                summary="FRED API key not configured; macro input neutral.",
                real_yield_change_pp=0.0,
                broad_usd_change_pct=0.0,
                cpi_yoy_pct=0.0,
            )
        try:
            headlines = load_headlines(settings.headlines_path)
        except DataUnavailable as exc:
            LOGGER.warning("%s; geopolitical input marked unknown", exc)
            headlines = []
        technical_report = TechnicalAnalystAgent().analyze(market_data.snapshot)
        geopolitical_report = GeopoliticalRiskAgent().analyze(headlines)
        decision = HeadTraderAgent(settings).decide(
            macro_report,
            technical_report,
            geopolitical_report,
        )
        LOGGER.info(
            "MT5 agents: macro=%s (%.2f/%.0f%%), technical=%s (%.2f/%.0f%%), "
            "geopolitical=%s/%s (%.2f/%.0f%%); supervisor=%s (score %.2f, confidence %.0f%%)",
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
                "macro_score": macro_report.score,
                "macro_confidence": macro_report.confidence,
                "technical_score": technical_report.score,
                "technical_confidence": technical_report.confidence,
                "geopolitical_score": geopolitical_report.score,
                "geopolitical_confidence": geopolitical_report.confidence,
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
        if decision.entry_price is None or decision.stop_loss is None:
            raise MT5ExecutionError("Supervisor proposal is missing validated order prices")
        positions = executor.positions()
        daily_start_equity = executor.daily_opening_equity(account)
        risk_per_lot = executor.stop_risk_per_lot(
            decision.decision,
            decision.entry_price,
            decision.stop_loss,
        )
        risk = RiskManager(settings).validate(
            decision,
            equity=account.equity,
            daily_start_equity=daily_start_equity,
            open_positions=len(positions),
            contract_size_ounces=market_data.contract_size,
            minimum_lot=market_data.volume_min,
            lot_step=market_data.volume_step,
            maximum_lots=market_data.volume_max,
            risk_per_lot=risk_per_lot,
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
            LOGGER.warning("Risk gate rejected MT5 proposal: %s", risk.reason)
            return {"status": "REJECT", "reason": risk.reason}

        outcome = executor.send_mt5_order(risk.trade)
        if not outcome.success:
            LOGGER.warning("MT5 order was not executed: %s", outcome.reason)
            return {
                "status": "REJECT",
                "reason": outcome.reason,
                "retcode": outcome.retcode,
            }
        LOGGER.info(
            "MT5 %s order completed: ticket=%s deal=%s %s %.2f lots at %.2f, "
            "SL %.2f, TP %.2f, planned risk %.2f",
            account.trade_mode,
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
            "status": "MT5_ORDER_EXECUTED",
            "order_ticket": outcome.order_ticket,
            "deal_ticket": outcome.deal_ticket,
            "fill_price": outcome.fill_price,
            "volume": outcome.volume,
            "risk_amount": risk.trade.risk_amount,
        }
    except (DataUnavailable, MT5ExecutionError) as exc:
        executor.record_event("HOLD", {"reason": str(exc)})
        LOGGER.warning("Cycle ended in HOLD: %s", exc)
        return {"status": "HOLD", "reason": str(exc)}
    except Exception as exc:
        LOGGER.exception("Unexpected MT5 cycle error; no new order was attempted")
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
        description="Run the local MetaTrader 5 gold supervisor"
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
        executor = MT5Execution(settings)
    except (MT5ExecutionError, ValueError) as exc:
        LOGGER.error("MT5 runner did not start: %s", exc)
        return 2

    LOGGER.info(
        "Started MT5 %s-mode runner for %s; interval=%d minutes",
        settings.mt5_account_mode.upper(),
        settings.trading_symbol,
        settings.interval_minutes,
    )
    if settings.mt5_account_mode == "any":
        LOGGER.warning(
            "MT5_ACCOUNT_MODE=any permits REAL-account orders; verify the selected login and server"
        )
    if not settings.fred_api_key:
        LOGGER.warning("FRED_API_KEY is unset; macro data is unavailable and cycles will HOLD")

    try:
        if args.once:
            try:
                run_cycle(settings, executor)
                return 0
            except Exception:
                LOGGER.exception("MT5 one-shot cycle escaped its error boundary")
                return 1

        # Continuous scan loop: keep running until a trade is executed.
        # Small delay between cycles to avoid hammering the MT5 terminal.
        SCAN_DELAY_SECONDS = 30
        while True:
            started = time.monotonic()
            try:
                result = run_cycle(settings, executor)
            except Exception:
                LOGGER.exception("MT5 cycle escaped its error boundary; retrying after short delay")
                result = None

            # Stop looping once a trade has been successfully executed.
            if isinstance(result, dict) and result.get("status") == "MT5_ORDER_EXECUTED":
                LOGGER.info(
                    "Trade executed: side=%s lots=%.2f entry=%.2f SL=%.2f TP=%.2f. Stopping continuous scan.",
                    result.get("side"),
                    result.get("lots"),
                    result.get("entry_price"),
                    result.get("stop_loss"),
                    result.get("take_profit"),
                )
                break

            elapsed = time.monotonic() - started
            sleep_for = max(0.0, SCAN_DELAY_SECONDS - elapsed)
            try:
                time.sleep(sleep_for)
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("Scan sleep failed; retrying after a short delay")
                time.sleep(min(60, SCAN_DELAY_SECONDS))
    except KeyboardInterrupt:
        LOGGER.info("Stopped MT5 continuous scan by user")
        return 0
    finally:
        executor.shutdown()


if __name__ == "__main__":
    sys.exit(main())
