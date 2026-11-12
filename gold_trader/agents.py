"""Strictly typed specialist reports and a deterministic supervisor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gold_trader.config import Settings
from gold_trader.data_feeds import MacroSnapshot, MarketSnapshot


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MacroReport(StrictModel):
    agent: Literal["macro_yield"]
    bias: Literal["bullish", "bearish", "neutral"]
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=500)
    real_yield_change_pp: float
    broad_usd_change_pct: float
    cpi_yoy_pct: float


class TechnicalReport(StrictModel):
    agent: Literal["technical"]
    bias: Literal["bullish", "bearish", "neutral"]
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=500)
    momentum: Literal["strong_up", "up", "mixed", "down", "strong_down"]
    price: float = Field(gt=0)
    rsi_14: float = Field(ge=0, le=100)
    support: float = Field(gt=0)
    resistance: float = Field(gt=0)


class GeopoliticalReport(StrictModel):
    agent: Literal["geopolitical_risk"]
    bias: Literal["bullish", "bearish", "neutral"]
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    risk_level: Literal["unknown", "low", "elevated", "high"]
    headlines_considered: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=500)


class TradeDecision(StrictModel):
    decision: Literal["BUY", "SELL", "HOLD"]
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rationale: str = Field(min_length=1, max_length=700)
    generated_at: datetime

    @model_validator(mode="after")
    def validate_order_levels(self) -> "TradeDecision":
        if self.decision == "HOLD":
            if any(value is not None for value in (self.entry_price, self.stop_loss, self.take_profit)):
                raise ValueError("HOLD decisions cannot include order prices")
            return self
        if any(value is None for value in (self.entry_price, self.stop_loss, self.take_profit)):
            raise ValueError("BUY/SELL decisions require entry, stop-loss, and take-profit prices")
        assert self.entry_price is not None
        assert self.stop_loss is not None
        assert self.take_profit is not None
        if self.decision == "BUY" and not (self.stop_loss < self.entry_price < self.take_profit):
            raise ValueError("BUY order levels must satisfy stop-loss < entry < take-profit")
        if self.decision == "SELL" and not (self.take_profit < self.entry_price < self.stop_loss):
            raise ValueError("SELL order levels must satisfy take-profit < entry < stop-loss")
        return self


def _bias(score: float) -> Literal["bullish", "bearish", "neutral"]:
    if score > 0.08:
        return "bullish"
    if score < -0.08:
        return "bearish"
    return "neutral"


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


class MacroYieldAgent:
    """Scores direction from real yields, nominal yields, USD, and CPI trends."""

    def analyze(self, snapshot: MacroSnapshot) -> MacroReport:
        real_yield_signal = _clamp(-snapshot.real_10y_change_pp / 0.15)
        nominal_yield_signal = _clamp(-snapshot.nominal_10y_change_pp / 0.20)
        usd_signal = _clamp(-snapshot.broad_usd_change_pct / 0.50)
        inflation_signal = _clamp(snapshot.cpi_yoy_change_pp / 0.50)
        score = _clamp(
            0.40 * real_yield_signal
            + 0.20 * nominal_yield_signal
            + 0.25 * usd_signal
            + 0.15 * inflation_signal
        )
        confidence = min(0.95, 0.35 + abs(score) * 0.55)
        return MacroReport(
            agent="macro_yield",
            bias=_bias(score),
            score=score,
            confidence=confidence,
            summary=(
                f"Real 10Y {snapshot.real_10y_pct:.2f}% ({snapshot.real_10y_change_pp:+.2f} pp); "
                f"broad USD {snapshot.broad_usd_change_pct:+.2f}% over recent observations."
            ),
            real_yield_change_pp=snapshot.real_10y_change_pp,
            broad_usd_change_pct=snapshot.broad_usd_change_pct,
            cpi_yoy_pct=snapshot.cpi_yoy_pct,
        )


class TechnicalAnalystAgent:
    """Scores trend alignment and momentum from validated OHLC-derived indicators."""

    def analyze(self, snapshot: MarketSnapshot) -> TechnicalReport:
        price = snapshot.latest_bar.close
        alignment = 0.0
        if price > snapshot.ema_50 > snapshot.ema_200:
            alignment = 0.55
        elif price < snapshot.ema_50 < snapshot.ema_200:
            alignment = -0.55
        elif price > snapshot.ema_200:
            alignment = 0.25
        elif price < snapshot.ema_200:
            alignment = -0.25

        slope = _clamp(
            (snapshot.ema_50_angle_deg + snapshot.ema_200_angle_deg) / 2.0 / 0.30
        ) * 0.25
        rsi = snapshot.rsi_14
        if 55 <= rsi < 70:
            rsi_signal = 0.20
        elif rsi >= 70:
            rsi_signal = -0.15
        elif 30 < rsi <= 45:
            rsi_signal = -0.20
        elif rsi <= 30:
            rsi_signal = 0.15
        else:
            rsi_signal = 0.0
        score = _clamp(alignment + slope + rsi_signal)
        confidence = min(0.95, 0.35 + abs(score) * 0.60)

        if score >= 0.55:
            momentum = "strong_up"
        elif score >= 0.18:
            momentum = "up"
        elif score <= -0.55:
            momentum = "strong_down"
        elif score <= -0.18:
            momentum = "down"
        else:
            momentum = "mixed"
        return TechnicalReport(
            agent="technical",
            bias=_bias(score),
            score=score,
            confidence=confidence,
            summary=(
                f"Price {price:.2f}; RSI {rsi:.1f}; EMA50 {snapshot.ema_50:.2f}, "
                f"EMA200 {snapshot.ema_200:.2f}."
            ),
            momentum=momentum,
            price=price,
            rsi_14=rsi,
            support=snapshot.support,
            resistance=snapshot.resistance,
        )


class GeopoliticalRiskAgent:
    """Classifies only headlines supplied in the configured local JSON input."""

    safe_haven_terms = (
        "war",
        "invasion",
        "missile",
        "airstrike",
        "attack",
        "escalation",
        "armed conflict",
        "terror",
        "sanction",
    )
    deescalation_terms = (
        "ceasefire",
        "peace deal",
        "de-escalation",
        "deescalation",
        "truce",
        "withdrawal",
        "talks resume",
    )
    high_risk_terms = ("war", "invasion", "missile", "airstrike", "attack", "armed conflict")

    def analyze(self, headlines: list[str]) -> GeopoliticalReport:
        if not headlines:
            return GeopoliticalReport(
                agent="geopolitical_risk",
                bias="neutral",
                score=0.0,
                confidence=0.0,
                risk_level="unknown",
                headlines_considered=0,
                summary="No supplied headlines; geopolitical input is unknown, not assumed neutral.",
            )

        normalized = [headline.casefold() for headline in headlines]
        safe_haven_hits = sum(
            1 for headline in normalized if any(term in headline for term in self.safe_haven_terms)
        )
        deescalation_hits = sum(
            1 for headline in normalized if any(term in headline for term in self.deescalation_terms)
        )
        acute_hits = sum(
            1 for headline in normalized if any(term in headline for term in self.high_risk_terms)
        )
        raw_score = safe_haven_hits - deescalation_hits
        score = _clamp(raw_score / max(2, len(headlines)))
        confidence = min(0.80, 0.35 + 0.10 * min(4, safe_haven_hits + deescalation_hits))
        risk_level: Literal["low", "elevated", "high"] = (
            "high" if acute_hits >= 2 else "elevated" if safe_haven_hits or deescalation_hits else "low"
        )
        return GeopoliticalReport(
            agent="geopolitical_risk",
            bias=_bias(score),
            score=score,
            confidence=confidence,
            risk_level=risk_level,
            headlines_considered=len(headlines),
            summary=(
                f"Reviewed {len(headlines)} supplied headlines; {safe_haven_hits} escalation and "
                f"{deescalation_hits} de-escalation keyword matches. Rule-based classification only."
            ),
        )


class HeadTraderAgent:
    """Combines specialist scores and emits BUY, SELL, or HOLD with attached levels."""

    weights = {"macro": 0.35, "technical": 0.45, "geopolitical": 0.20}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def decide(
        self,
        macro: MacroReport,
        technical: TechnicalReport,
        geopolitical: GeopoliticalReport,
    ) -> TradeDecision:
        reports = (
            (macro.score, macro.confidence, self.weights["macro"]),
            (technical.score, technical.confidence, self.weights["technical"]),
            (geopolitical.score, geopolitical.confidence, self.weights["geopolitical"]),
        )
        effective_weight = sum(weight * confidence for _, confidence, weight in reports)
        score = (
            sum(value * confidence * weight for value, confidence, weight in reports) / effective_weight
            if effective_weight > 0
            else 0.0
        )
        confidence = min(1.0, effective_weight)
        if (
            confidence < self.settings.minimum_decision_confidence
            or abs(score) < self.settings.minimum_signal_strength
        ):
            return TradeDecision(
                decision="HOLD",
                score=score,
                confidence=confidence,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
                rationale="Combined specialist signal did not meet both confidence and strength thresholds.",
                generated_at=datetime.now(UTC),
            )

        entry = technical.price
        if score > 0:
            decision: Literal["BUY", "SELL", "HOLD"] = "BUY"
            stop_loss = entry - self.settings.default_stop_loss_usd
            take_profit = entry + self.settings.default_take_profit_usd
        else:
            decision = "SELL"
            stop_loss = entry + self.settings.default_stop_loss_usd
            take_profit = entry - self.settings.default_take_profit_usd
        geopolitical_status = (
            "unknown input"
            if geopolitical.risk_level == "unknown"
            else f"{geopolitical.risk_level}/{geopolitical.bias}"
        )
        return TradeDecision(
            decision=decision,
            score=score,
            confidence=confidence,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rationale=(
                f"Weighted score {score:+.2f} at {confidence:.0%} confidence; "
                f"macro {macro.bias}, technical {technical.bias}, geopolitical {geopolitical_status}."
            ),
            generated_at=datetime.now(UTC),
        )
