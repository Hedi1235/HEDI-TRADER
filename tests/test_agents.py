from datetime import UTC, datetime
import unittest

from pydantic import ValidationError

from gold_trader.agents import GeopoliticalRiskAgent, MacroReport, TradeDecision


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


if __name__ == "__main__":
    unittest.main()
