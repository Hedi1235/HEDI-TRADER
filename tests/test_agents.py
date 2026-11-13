from datetime import UTC, datetime
import unittest

from pydantic import ValidationError

from gold_trader.agents import (
    GeopoliticalReport,
    GeopoliticalRiskAgent,
    HeadTraderAgent,
    MacroReport,
    TechnicalReport,
    TradeDecision,
)
from gold_trader.config import Settings


class AgentSchemaTests(unittest.TestCase):
    def test_macro_report_rejects_unexpected_fields(self) -> None:
        with self.assertRaises(ValidationError):
            MacroReport.model_validate(
                {
                    "agent": "macro_yield",
                    "bias": "bullish",
                    "score": 0.4,
                    "confidence": 0.7,
                    "summary": "Validated signal",
                    "real_yield_change_pp": -0.05,
                    "broad_usd_change_pct": -0.1,
                    "cpi_yoy_pct": 2.5,
                    "untrusted_extra": "discard this",
                }
            )

    def test_hold_cannot_include_order_prices(self) -> None:
        with self.assertRaises(ValidationError):
            TradeDecision(
                decision="HOLD",
                score=0.0,
                confidence=0.0,
                entry_price=2000.0,
                stop_loss=None,
                take_profit=None,
                rationale="Invalid hold",
                generated_at=datetime.now(UTC),
            )

    def test_missing_headlines_are_unknown_not_neutral(self) -> None:
        report = GeopoliticalRiskAgent().analyze([])
        self.assertEqual(report.risk_level, "unknown")
        self.assertEqual(report.confidence, 0.0)

    def test_supervisor_uses_expected_weights_and_accepts_exact_confidence_floor(self) -> None:
        macro = MacroReport(
            agent="macro_yield",
            bias="bullish",
            score=0.5,
            confidence=0.6875,
            summary="Macro test",
            real_yield_change_pp=-0.1,
            broad_usd_change_pct=-0.1,
            cpi_yoy_pct=2.5,
        )
        technical = TechnicalReport(
            agent="technical",
            bias="bullish",
            score=0.5,
            confidence=0.6875,
            summary="Technical test",
            momentum="up",
            price=2000.0,
            rsi_14=60.0,
            support=1980.0,
            resistance=2020.0,
        )
        geopolitical = GeopoliticalReport(
            agent="geopolitical_risk",
            bias="neutral",
            score=0.0,
            confidence=0.0,
            risk_level="unknown",
            headlines_considered=0,
            summary="No headlines",
        )
        supervisor = HeadTraderAgent(Settings())

        self.assertEqual(
            supervisor.weights,
            {"macro": 0.35, "technical": 0.45, "geopolitical": 0.20},
        )
        decision = supervisor.decide(macro, technical, geopolitical)
        self.assertAlmostEqual(decision.confidence, 0.55)
        self.assertEqual(decision.decision, "BUY")


if __name__ == "__main__":
    unittest.main()
