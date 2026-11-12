from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from gold_trader.config import Settings
from gold_trader.data_feeds import PriceBar
from gold_trader.execution import PaperExecutionEngine
from gold_trader.risk_manager import ApprovedTrade


class PaperExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        database = Path(self.temp_dir.name) / "paper.db"
        self.settings = replace(Settings(), database_path=database)
        self.engine = PaperExecutionEngine(self.settings)

    def sample_trade(self) -> ApprovedTrade:
        return ApprovedTrade(
            side="BUY",
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            lots=0.1,
            risk_amount=100.0,
            confidence=0.8,
            rationale="Test paper order",
        )

    def test_order_is_stored_as_paper_and_changes_open_count(self) -> None:
        trade_id = self.engine.open_paper_trade(self.sample_trade())
        summary = self.engine.account_summary()
        self.assertGreater(trade_id, 0)
        self.assertEqual(summary.open_positions, 1)
        self.assertEqual(summary.equity, 10_000.0)
        events = self.engine.recent_events()
        self.assertEqual(events[0]["event_type"], "PAPER_ORDER_OPENED")

    def test_stop_is_assumed_first_when_both_levels_are_touched(self) -> None:
        self.engine.open_paper_trade(self.sample_trade())
        closed = self.engine.update_open_positions(
            PriceBar(
                timestamp=datetime.now(UTC),
                open=2000.0,
                high=2025.0,
                low=1985.0,
                close=2005.0,
            )
        )
        summary = self.engine.account_summary()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "CLOSED_SL")
        self.assertAlmostEqual(closed[0]["pnl"], -100.0)
        self.assertEqual(summary.open_positions, 0)
        self.assertAlmostEqual(summary.equity, 9900.0)

    def test_daily_opening_equity_is_stable_after_trade_changes(self) -> None:
        today = datetime(2026, 11, 12, 12, tzinfo=UTC)
        first = self.engine.daily_opening_equity(today)
        self.engine.open_paper_trade(self.sample_trade())
        second = self.engine.daily_opening_equity(today)
        self.assertEqual(first, 10_000.0)
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
