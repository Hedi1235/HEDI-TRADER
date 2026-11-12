from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gold_trader.config import Settings
from gold_trader.data_feeds import MarketSnapshot, PriceBar
from gold_trader.execution import PaperExecutionEngine
from gold_trader.main import run_cycle


class MainLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings = Settings()
        self.settings = replace(
            settings,
            database_path=Path(self.temp_dir.name) / "paper.db",
            fred_api_key=None,
        )
        self.engine = PaperExecutionEngine(self.settings)

    def test_missing_fred_key_records_hold_without_opening_trade(self) -> None:
        market = MarketSnapshot(
            ticker="XAUUSD=X",
            latest_bar=PriceBar(
                timestamp=datetime.now(UTC),
                open=2000.0,
                high=2005.0,
                low=1995.0,
                close=2002.0,
            ),
            rsi_14=55.0,
            ema_50=2001.0,
            ema_200=1998.0,
            ema_50_angle_deg=0.1,
            ema_200_angle_deg=0.05,
            support=1980.0,
            resistance=2020.0,
            candles_available=250,
        )
        with patch("gold_trader.main.fetch_market_snapshot", return_value=market):
            result = run_cycle(self.settings, self.engine)

        self.assertEqual(result["status"], "HOLD")
        self.assertIn("FRED_API_KEY", result["reason"])
        self.assertEqual(self.engine.account_summary().open_positions, 0)
        self.assertEqual(self.engine.recent_events()[0]["event_type"], "HOLD")


if __name__ == "__main__":
    unittest.main()
