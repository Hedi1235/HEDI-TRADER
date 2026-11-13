"""Deterministic position sizing and account-level circuit breakers."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gold_trader.agents import TradeDecision
from gold_trader.config import Settings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ApprovedTrade(StrictModel):
    side: Literal["BUY", "SELL"]
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    lots: float = Field(gt=0)
    risk_amount: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=700)


class RiskResult(StrictModel):
    approved: bool
    reason: str = Field(min_length=1, max_length=500)
    daily_drawdown_pct: float = Field(ge=0)
    risk_amount: float = Field(ge=0)
    trade: ApprovedTrade | None = None


class RiskManager:
    """Rejects unsafe proposals and sizes orders at or below 1% account risk."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(
        self,
        decision: TradeDecision,
        *,
        equity: float,
        daily_start_equity: float,
        open_positions: int,
        contract_size_ounces: float | None = None,
        minimum_lot: float | None = None,
        lot_step: float | None = None,
        maximum_lots: float | None = None,
        risk_per_lot: float | None = None,
    ) -> RiskResult:
        if decision.decision == "HOLD":
            return self._reject("Supervisor decision is HOLD")
        if not math.isfinite(decision.confidence):
            return self._reject("Decision confidence must be finite")
        if decision.confidence < self.settings.minimum_decision_confidence:
            return self._reject("Decision confidence is below the configured minimum")
        if open_positions >= self.settings.max_open_positions:
            return self._reject("Maximum open positions already reached")
        if (
            not math.isfinite(equity)
            or not math.isfinite(daily_start_equity)
            or equity <= 0
            or daily_start_equity <= 0
        ):
            return self._reject("Account equity must be positive")

        drawdown = max(0.0, (daily_start_equity - equity) / daily_start_equity)
        if drawdown >= self.settings.max_daily_drawdown_pct:
            return RiskResult(
                approved=False,
                reason="Daily drawdown circuit breaker is active",
                daily_drawdown_pct=drawdown,
                risk_amount=0.0,
                trade=None,
            )

        entry = decision.entry_price
        stop = decision.stop_loss
        target = decision.take_profit
        if entry is None or stop is None or target is None:
            return self._reject("A trade proposal must include entry, stop-loss, and take-profit")
        if not all(math.isfinite(value) for value in (entry, stop, target)):
            return self._reject("Order prices must be finite")
        if entry <= 0 or stop <= 0 or target <= 0:
            return self._reject("Order prices must be positive")
        if decision.decision == "BUY" and not stop < entry < target:
            return self._reject("BUY order levels are invalid")
        if decision.decision == "SELL" and not target < entry < stop:
            return self._reject("SELL order levels are invalid")

        stop_distance = abs(entry - stop)
        risk_budget = equity * self.settings.max_risk_per_trade_pct
        contract_size = (
            self.settings.contract_size_ounces
            if contract_size_ounces is None
            else contract_size_ounces
        )
        effective_minimum_lot = (
            self.settings.minimum_lot if minimum_lot is None else minimum_lot
        )
        effective_lot_step = self.settings.lot_step if lot_step is None else lot_step
        configured_maximum = self.settings.maximum_lots
        broker_maximum = configured_maximum if maximum_lots is None else maximum_lots
        if (
            not all(
                math.isfinite(value)
                for value in (
                    contract_size,
                    effective_minimum_lot,
                    effective_lot_step,
                    configured_maximum,
                    broker_maximum,
                )
            )
            or contract_size <= 0
            or effective_lot_step <= 0
            or effective_minimum_lot <= 0
        ):
            return self._reject("Broker volume and contract specifications are invalid")
        effective_maximum_lots = min(configured_maximum, broker_maximum)
        if effective_maximum_lots < effective_minimum_lot:
            return self._reject("Broker maximum volume is below its minimum volume")
        calculated_risk_per_lot = (
            stop_distance * contract_size if risk_per_lot is None else risk_per_lot
        )
        if (
            not math.isfinite(calculated_risk_per_lot)
            or stop_distance <= 0
            or calculated_risk_per_lot <= 0
        ):
            return self._reject("Stop-loss distance must be positive")

        raw_lots = min(risk_budget / calculated_risk_per_lot, effective_maximum_lots)
        step = Decimal(str(effective_lot_step))
        lots = (Decimal(str(raw_lots)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        lot_value = float(lots)
        if lot_value < effective_minimum_lot:
            return self._reject("Risk budget is too small for the minimum lot size")

        actual_risk = lot_value * calculated_risk_per_lot
        if actual_risk > risk_budget + 1e-8:
            return self._reject("Rounded position size exceeds the per-trade risk budget")

        trade = ApprovedTrade(
            side=decision.decision,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            lots=lot_value,
            risk_amount=actual_risk,
            confidence=decision.confidence,
            rationale=decision.rationale,
        )
        return RiskResult(
            approved=True,
            reason="Passed deterministic risk checks",
            daily_drawdown_pct=drawdown,
            risk_amount=actual_risk,
            trade=trade,
        )

    @staticmethod
    def _reject(reason: str) -> RiskResult:
        return RiskResult(
            approved=False,
            reason=reason,
            daily_drawdown_pct=0.0,
            risk_amount=0.0,
            trade=None,
        )
