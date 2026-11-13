from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gold_trader.config import Settings
from gold_trader.execution_mt5 import MT5Execution, MT5ExecutionError


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_DISABLED = 0
    TIMEFRAME_M15 = 15
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def __init__(self, *, trade_mode: int = ACCOUNT_TRADE_MODE_DEMO) -> None:
        self.trade_mode = trade_mode
        self.connected = False
        self.initialize_failures = 0
        self.initialize_calls = 0
        self.order_send_calls = 0
        self.positions_value: tuple[object, ...] = ()
        self.drop_next_positions_call = False

    def initialize(self, *_args: object, **_kwargs: object) -> bool:
        self.initialize_calls += 1
        if self.initialize_failures:
            self.initialize_failures -= 1
            return False
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=self.connected, trade_allowed=True)

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(
            trade_mode=self.trade_mode,
            login=123456,
            server="MetaQuotes-Demo",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            margin_free=10_000.0,
            margin_level=0.0,
            trade_allowed=True,
        )

    def symbol_info(self, _symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            visible=True,
            trade_mode=1,
            point=0.01,
            digits=2,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=2.0,
            trade_stops_level=0,
        )

    def positions_get(self) -> tuple[object, ...] | None:
        if self.drop_next_positions_call:
            self.drop_next_positions_call = False
            self.connected = False
            return None
        return self.positions_value

    def order_calc_profit(
        self,
        _order_type: int,
        _symbol: str,
        volume: float,
        entry: float,
        stop: float,
    ) -> float:
        return -abs(entry - stop) * 100.0 * volume

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake error")

    def order_send(self, _request: dict[str, object]) -> None:
        self.order_send_calls += 1
        self.connected = False
        return None


class MT5ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "trading_bot.db"

    def settings(self, mode: str = "demo") -> Settings:
        return replace(
            Settings(),
            mt5_login=123456,
            mt5_password="test-password",
            mt5_server="MetaQuotes-Demo",
            mt5_account_mode=mode,
            mt5_database_path=self.database,
        )

    def settings_for_logged_in_terminal(self) -> Settings:
        return replace(
            Settings(),
            mt5_database_path=self.database,
        )

    def make_executor(
        self,
        *,
        trade_mode: int = FakeMT5.ACCOUNT_TRADE_MODE_DEMO,
        account_mode: str = "demo",
        positions: tuple[object, ...] = (),
    ) -> tuple[MT5Execution, FakeMT5]:
        terminal = FakeMT5(trade_mode=trade_mode)
        terminal.positions_value = positions
        executor = MT5Execution(
            self.settings(account_mode),
            mt5_module=terminal,
            enforce_windows=False,
        )
        return executor, terminal

    def test_demo_mode_rejects_real_account(self) -> None:
        terminal = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
        with self.assertRaisesRegex(MT5ExecutionError, "not confirmed as DEMO"):
            MT5Execution(
                self.settings(),
                mt5_module=terminal,
                enforce_windows=False,
            )

    def test_any_mode_accepts_confirmed_real_account(self) -> None:
        executor, _terminal = self.make_executor(
            trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL,
            account_mode="any",
        )
        self.assertEqual(executor.account_snapshot().trade_mode, "REAL")
        executor.shutdown()

    def test_uses_already_logged_in_demo_terminal_without_saved_credentials(self) -> None:
        terminal = FakeMT5()
        executor = MT5Execution(
            self.settings_for_logged_in_terminal(),
            mt5_module=terminal,
            enforce_windows=False,
        )
        self.assertEqual(executor.account_snapshot().trade_mode, "DEMO")
        executor.shutdown()

    def test_requires_matching_account_server(self) -> None:
        settings = replace(self.settings(), mt5_server="Different-Server")
        with self.assertRaisesRegex(MT5ExecutionError, "does not match MT5_SERVER"):
            MT5Execution(
                settings,
                mt5_module=FakeMT5(),
                enforce_windows=False,
            )

    def test_reads_stop_risk_in_account_currency_from_broker(self) -> None:
        executor, _terminal = self.make_executor()
        self.assertEqual(executor.stop_risk_per_lot("BUY", 2000.0, 1990.0), 1000.0)
        executor.shutdown()

    def test_startup_reconciles_broker_positions_to_sqlite(self) -> None:
        open_position = SimpleNamespace(
            ticket=987,
            symbol="XAUUSD",
            type=FakeMT5.ORDER_TYPE_BUY,
            volume=0.1,
            price_open=2000.0,
            sl=1990.0,
            tp=2020.0,
        )
        executor, _terminal = self.make_executor(positions=(open_position,))
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT login, server, ticket, symbol, volume FROM mt5_open_positions"
            ).fetchone()
            events = connection.execute(
                "SELECT event_type FROM mt5_events ORDER BY id"
            ).fetchall()
            daily_equity = connection.execute(
                "SELECT opening_equity FROM mt5_daily_equity"
            ).fetchone()[0]
        self.assertEqual(row, (123456, "MetaQuotes-Demo", 987, "XAUUSD", 0.1))
        self.assertIn(("MT5_POSITION_RECONCILED",), events)
        self.assertEqual(daily_equity, 10_000.0)
        executor.shutdown()

    def test_position_reconciliation_removes_closed_broker_positions(self) -> None:
        open_position = SimpleNamespace(
            ticket=987,
            symbol="XAUUSD",
            type=FakeMT5.ORDER_TYPE_BUY,
            volume=0.1,
            price_open=2000.0,
            sl=1990.0,
            tp=2020.0,
        )
        executor, terminal = self.make_executor(positions=(open_position,))
        terminal.positions_value = ()
        self.assertEqual(executor.positions(), ())
        with sqlite3.connect(self.database) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM mt5_open_positions"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        executor.shutdown()

    def test_reconnect_uses_exponential_backoff(self) -> None:
        executor, terminal = self.make_executor()
        terminal.connected = False
        terminal.initialize_failures = 2
        with patch("gold_trader.execution_mt5.time.sleep") as sleep:
            executor.ensure_connected()
        self.assertTrue(terminal.connected)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0],
        )
        executor.shutdown()

    def test_read_only_call_retries_after_connection_drop(self) -> None:
        executor, terminal = self.make_executor()
        terminal.drop_next_positions_call = True
        with patch("gold_trader.execution_mt5.time.sleep"):
            self.assertEqual(executor.positions(), ())
        self.assertTrue(terminal.connected)
        executor.shutdown()

    def test_order_send_is_not_replayed_after_connection_drop(self) -> None:
        executor, terminal = self.make_executor()
        result = executor._mt5_call("order_send", {}, retry_safe=False)
        self.assertIsNone(result)
        self.assertEqual(terminal.order_send_calls, 1)
        executor.shutdown()


if __name__ == "__main__":
    unittest.main()
