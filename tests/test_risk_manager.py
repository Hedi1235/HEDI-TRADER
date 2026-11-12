from datetime import UTC, datetime
import unittest

from gold_trader.agents import TradeDecision
from gold_trader.config import Settings
from gold_trader.risk_manager import RiskManager


def buy_decision(
    entry: float = 2000.0,
    stop: float = 1990.0,
    target: float = 2020.0,
) -> TradeDecision:
    return TradeDecision(
        decision="BUY",
        score=0.7,
        confidence=0.8,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        rationale="Test signal",
        generated_at=datetime.now(UTC),
    )


class RiskManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()
        self.manager = RiskManager(self.settings)

    def test_sizes_trade_to_one_percent_risk(self) -> None:
        result = self.manager.validate(
            buy_decision(),
            equity=10_000.0,
            daily_start_equity=10_000.0,
            open_positions=0,
        )
        self.assertTrue(result.approved)
        self.assertIsNotNone(result.trade)
        assert result.trade is not None
        self.assertAlmostEqual(result.trade.lots, 0.1)
        self.assertAlmostEqual(result.trade.risk_amount, 100.0)
        self.assertLessEqual(result.trade.risk_amount, 100.0)

    def test_rejects_at_three_percent_daily_drawdown(self) -> None:
        result = self.manager.validate(
            buy_decision(),
            equity=9700.0,
            daily_start_equity=10_000.0,
            open_positions=0,
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.reason, "Daily drawdown circuit breaker is active")
        self.assertAlmostEqual(result.daily_drawdown_pct, 0.03)

    def test_rejects_when_another_position_is_open(self) -> None:
        result = self.manager.validate(
            buy_decision(),
            equity=10_000.0,
            daily_start_equity=10_000.0,
            open_positions=1,
        )
        self.assertFalse(result.approved)
        self.assertIn("Maximum open", result.reason)

    def test_rejects_incorrect_stop_geometry(self) -> None:
        decision = TradeDecision.model_construct(
            decision="BUY",
            score=0.7,
            confidence=0.8,
            entry_price=2000.0,
            stop_loss=2005.0,
            take_profit=2020.0,
            rationale="Intentionally malformed proposal for defense-in-depth test",
            generated_at=datetime.now(UTC),
        )
        result = self.manager.validate(
            decision,
            equity=10_000.0,
            daily_start_equity=10_000.0,
            open_positions=0,
        )
        self.assertFalse(result.approved)
        self.assertIn("BUY order levels", result.reason)

    def test_hold_is_not_executable(self) -> None:
        decision = TradeDecision(
            decision="HOLD",
            score=0.0,
            confidence=0.0,
            entry_price=None,
            stop_loss=None,
            take_profit=None,
            rationale="No valid signal",
            generated_at=datetime.now(UTC),
        )
        result = self.manager.validate(
            decision,
            equity=10_000.0,
            daily_start_equity=10_000.0,
            open_positions=0,
        )
        self.assertFalse(result.approved)
        self.assertIsNone(result.trade)


if __name__ == "__main__":
    unittest.main()
