"""Windows-only MetaTrader 5 bridge that refuses non-demo trading accounts."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from gold_trader.config import Settings
from gold_trader.data_feeds import (
    DataUnavailable,
    MarketSnapshot,
    market_snapshot_from_frame,
)
from gold_trader.risk_manager import ApprovedTrade


class MT5DemoError(RuntimeError):
    """Raised when the terminal or demo-only safety checks fail."""


@dataclass(frozen=True)
class MT5MarketData:
    snapshot: MarketSnapshot
    bid: float
    ask: float
    point: float
    digits: int
    contract_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    stops_level_points: int


@dataclass(frozen=True)
class MT5Account:
    login: int
    server: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float | None
    trade_allowed: bool


@dataclass(frozen=True)
class MT5OrderOutcome:
    success: bool
    reason: str
    retcode: int | None = None
    order_ticket: int | None = None
    deal_ticket: int | None = None
    fill_price: float | None = None
    volume: float | None = None
    executed_at: str | None = None


class MT5DemoExecution:
    """Local-terminal bridge. The account must be a demo account for every send."""

    def __init__(
        self,
        settings: Settings,
        *,
        mt5_module: Any | None = None,
        enforce_windows: bool = True,
    ) -> None:
        self.settings = settings
        if not settings.mt5_credentials_configured:
            raise MT5DemoError(
                "Set MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER in the local .env file"
            )
        if enforce_windows and sys.platform != "win32":
            raise MT5DemoError(
                "MT5 Python execution requires the Windows computer running the desktop terminal; "
                "this runner cannot connect from Replit's Linux process"
            )
        if mt5_module is None:
            try:
                import MetaTrader5 as mt5_module  # type: ignore[import-not-found,no-redef]
            except ImportError as exc:
                raise MT5DemoError(
                    "MetaTrader5 is not installed; install the optional MT5 dependency on Windows"
                ) from exc
        self.mt5 = mt5_module
        self.settings.mt5_database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_ledger()
        self._initialize_terminal()
        self._assert_demo_account()
        self.symbol_info()

    def _initialize_terminal(self) -> None:
        if not self.settings.mt5_credentials_configured:
            raise MT5DemoError("MT5 demo credentials are not configured")
        login = self.settings.mt5_login
        password = self.settings.mt5_password
        server = self.settings.mt5_server
        assert login is not None and password is not None and server is not None

        args: dict[str, Any] = {
            "login": login,
            "password": password,
            "server": server,
            "timeout": 15_000,
        }
        terminal_path = self.settings.mt5_terminal_path
        if terminal_path is not None:
            if not terminal_path.is_file():
                raise MT5DemoError("Configured MT5 terminal executable was not found")
            initialized = self.mt5.initialize(str(terminal_path), **args)
        else:
            initialized = self.mt5.initialize(**args)
        if not initialized:
            last_error = self.mt5.last_error()
            error_code = last_error[0] if isinstance(last_error, tuple) and last_error else "unknown"
            raise MT5DemoError(f"MT5 terminal initialization failed (error code {error_code})")

        terminal = self.mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            raise MT5DemoError("MT5 terminal is not connected to its trade server")
        self._assert_demo_account()

    def ensure_connected(self) -> None:
        """Heartbeat the terminal and attempt one safe reconnect if disconnected."""

        terminal = self.mt5.terminal_info()
        if terminal is not None and bool(getattr(terminal, "connected", False)):
            self._assert_demo_account()
            return
        self.mt5.shutdown()
        self._initialize_terminal()

    def _assert_demo_account(self) -> Any:
        account = self.mt5.account_info()
        if account is None:
            raise MT5DemoError("MT5 did not return account information")
        demo_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        if demo_mode is None or getattr(account, "trade_mode", None) != demo_mode:
            raise MT5DemoError("Refusing to trade: the connected account is not confirmed as DEMO")
        if int(getattr(account, "login", 0)) != self.settings.mt5_login:
            raise MT5DemoError("Connected MT5 account does not match MT5_LOGIN")
        return account

    def symbol_info(self) -> Any:
        self.ensure_connected()
        symbol = self.settings.trading_symbol
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise MT5DemoError(f"MT5 does not recognize trading symbol {symbol}")
        if not bool(getattr(info, "visible", False)):
            if not self.mt5.symbol_select(symbol, True):
                raise MT5DemoError(f"Could not enable {symbol} in Market Watch")
            info = self.mt5.symbol_info(symbol)
        if info is None or not bool(getattr(info, "visible", False)):
            raise MT5DemoError(f"Trading symbol {symbol} is not visible in Market Watch")
        disabled = getattr(self.mt5, "SYMBOL_TRADE_MODE_DISABLED", None)
        if disabled is not None and getattr(info, "trade_mode", None) == disabled:
            raise MT5DemoError(f"Trading is disabled for {symbol}")
        for field in ("point", "trade_contract_size", "volume_min", "volume_step", "volume_max"):
            if float(getattr(info, field, 0.0) or 0.0) <= 0:
                raise MT5DemoError(f"MT5 returned invalid {field} for {symbol}")
        return info

    def account_snapshot(self) -> MT5Account:
        self.ensure_connected()
        raw = self._assert_demo_account()
        terminal = self.mt5.terminal_info()
        trade_allowed = bool(getattr(raw, "trade_allowed", False)) and bool(
            getattr(terminal, "trade_allowed", False)
        )
        margin_level = getattr(raw, "margin_level", None)
        return MT5Account(
            login=int(raw.login),
            server=str(raw.server),
            balance=float(raw.balance),
            equity=float(raw.equity),
            margin=float(raw.margin or 0.0),
            margin_free=float(raw.margin_free or 0.0),
            margin_level=float(margin_level) if margin_level is not None else None,
            trade_allowed=trade_allowed,
        )

    def positions(self) -> tuple[Any, ...]:
        self.ensure_connected()
        positions = self.mt5.positions_get(symbol=self.settings.trading_symbol)
        if positions is None:
            error = self.mt5.last_error()
            error_code = error[0] if isinstance(error, tuple) and error else "unknown"
            raise MT5DemoError(f"MT5 position query failed (error code {error_code})")
        return tuple(positions)

    def market_data(self) -> MT5MarketData:
        """Use broker OHLC and bid/ask quotes so signal and execution prices match."""

        self.ensure_connected()
        info = self.symbol_info()
        required_bars = max(self.settings.slow_ema_period + 10, 500)
        rates = self.mt5.copy_rates_from_pos(
            self.settings.trading_symbol,
            self.mt5.TIMEFRAME_M15,
            1,
            required_bars,
        )
        if rates is None or len(rates) == 0:
            error = self.mt5.last_error()
            error_code = error[0] if isinstance(error, tuple) and error else "unknown"
            raise DataUnavailable(f"MT5 returned no rates (error code {error_code})")
        frame = pd.DataFrame(rates)
        if "time" not in frame.columns:
            raise DataUnavailable("MT5 rates did not include timestamps")
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frame = frame.rename(
            columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"}
        ).set_index("time")
        snapshot = market_snapshot_from_frame(
            self.settings,
            self.settings.trading_symbol,
            frame,
        )

        tick = self.mt5.symbol_info_tick(self.settings.trading_symbol)
        if tick is None:
            raise DataUnavailable("MT5 did not return a current bid/ask quote")
        bid, ask = float(tick.bid), float(tick.ask)
        if bid <= 0 or ask < bid:
            raise DataUnavailable("MT5 returned an invalid bid/ask quote")
        tick_time = float(getattr(tick, "time", 0) or 0)
        quote_age = datetime.now(UTC) - datetime.fromtimestamp(tick_time, tz=UTC)
        if quote_age > timedelta(minutes=2) or quote_age < timedelta(minutes=-1):
            raise DataUnavailable("MT5 bid/ask quote is stale or has a future timestamp")

        return MT5MarketData(
            snapshot=snapshot,
            bid=bid,
            ask=ask,
            point=float(info.point),
            digits=int(info.digits),
            contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            volume_max=float(info.volume_max),
            stops_level_points=int(getattr(info, "trade_stops_level", 0) or 0),
        )

    def daily_opening_equity(self, account: MT5Account, now: datetime | None = None) -> float:
        timestamp = now or datetime.now(UTC)
        utc_date = timestamp.astimezone(UTC).date().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT opening_equity FROM mt5_daily_equity
                WHERE login = ? AND server = ? AND utc_date = ?
                """,
                (account.login, account.server, utc_date),
            ).fetchone()
            if row is not None:
                return float(row["opening_equity"])
            connection.execute(
                """
                INSERT INTO mt5_daily_equity (login, server, utc_date, opening_equity, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account.login,
                    account.server,
                    utc_date,
                    account.equity,
                    timestamp.astimezone(UTC).isoformat(),
                ),
            )
            return account.equity

    def record_event(self, event_type: str, detail: dict[str, Any]) -> None:
        with self._connection() as connection:
            self._record_event(connection, event_type, detail, datetime.now(UTC).isoformat())

    def send_mt5_order(self, trade: ApprovedTrade) -> MT5OrderOutcome:
        """Check demo/account/symbol/margin again, then send one market order."""

        try:
            self.ensure_connected()
            account = self.account_snapshot()
            if not account.trade_allowed:
                return self._reject("MT5 terminal or account has disabled algorithmic trading")
            if account.margin > 0 and (
                account.margin_level is None
                or account.margin_level < self.settings.minimum_margin_level_pct
            ):
                return self._reject("Current margin level is below the configured minimum")

            positions = self.positions()
            if len(positions) >= self.settings.max_open_positions:
                return self._reject("Maximum open positions already reached")

            info = self.symbol_info()
            tick = self.mt5.symbol_info_tick(self.settings.trading_symbol)
            if tick is None:
                return self._reject("MT5 did not return a current bid/ask quote")
            order_type = (
                self.mt5.ORDER_TYPE_BUY if trade.side == "BUY" else self.mt5.ORDER_TYPE_SELL
            )
            price = float(tick.ask if trade.side == "BUY" else tick.bid)
            if price <= 0:
                return self._reject("MT5 returned an invalid execution price")
            max_price_change = self.settings.mt5_deviation_points * float(info.point)
            if abs(price - trade.entry_price) > max_price_change:
                return self._reject("Quote moved beyond the configured slippage deviation")

            if trade.side == "BUY":
                levels_valid = trade.stop_loss < price < trade.take_profit
                stop_distance = min(float(tick.bid) - trade.stop_loss, trade.take_profit - float(tick.bid))
            else:
                levels_valid = trade.take_profit < price < trade.stop_loss
                stop_distance = min(trade.stop_loss - float(tick.ask), float(tick.ask) - trade.take_profit)
            if not levels_valid or stop_distance <= 0:
                return self._reject("Stop-loss or take-profit is invalid for the current quote")
            minimum_stop_distance = int(getattr(info, "trade_stops_level", 0) or 0) * float(info.point)
            if stop_distance < minimum_stop_distance:
                return self._reject("Stop-loss or take-profit is inside the broker minimum distance")

            margin_required = self.mt5.order_calc_margin(
                order_type,
                self.settings.trading_symbol,
                trade.lots,
                price,
            )
            if margin_required is None or float(margin_required) < 0:
                return self._reject("MT5 could not calculate required margin")
            available_margin = account.margin_free * self.settings.maximum_free_margin_usage_pct
            if float(margin_required) > available_margin:
                return self._reject("Required margin exceeds the configured free-margin budget")

            request = {
                "action": self.mt5.TRADE_ACTION_DEAL,
                "symbol": self.settings.trading_symbol,
                "volume": trade.lots,
                "type": order_type,
                "price": round(price, int(info.digits)),
                "sl": round(trade.stop_loss, int(info.digits)),
                "tp": round(trade.take_profit, int(info.digits)),
                "deviation": self.settings.mt5_deviation_points,
                "magic": 26111206,
                "comment": "gold-demo-supervisor",
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_RETURN,
            }
            check = self.mt5.order_check(request)
            if check is None:
                return self._reject("MT5 order_check returned no result")
            check_code = int(getattr(check, "retcode", -1))
            if check_code != 0:
                return self._reject(
                    f"MT5 pre-trade check rejected the order (retcode {check_code})"
                )

            result = self.mt5.order_send(request)
            if result is None:
                return self._reject("MT5 order_send returned no result")
            retcode = int(getattr(result, "retcode", -1))
            success = retcode == int(self.mt5.TRADE_RETCODE_DONE)
            outcome = MT5OrderOutcome(
                success=success,
                reason="Demo order executed" if success else "MT5 rejected the order",
                retcode=retcode,
                order_ticket=int(result.order) if getattr(result, "order", 0) else None,
                deal_ticket=int(result.deal) if getattr(result, "deal", 0) else None,
                fill_price=float(result.price) if getattr(result, "price", 0) else None,
                volume=float(getattr(result, "volume", trade.lots) or trade.lots),
                executed_at=datetime.now(UTC).isoformat(),
            )
            self.record_event(
                "MT5_DEMO_ORDER_EXECUTED" if success else "MT5_DEMO_ORDER_REJECTED",
                {
                    "symbol": self.settings.trading_symbol,
                    "side": trade.side,
                    "volume": outcome.volume,
                    "requested_price": price,
                    "fill_price": outcome.fill_price,
                    "stop_loss": trade.stop_loss,
                    "take_profit": trade.take_profit,
                    "risk_amount": trade.risk_amount,
                    "retcode": retcode,
                    "order_ticket": outcome.order_ticket,
                    "deal_ticket": outcome.deal_ticket,
                    "comment": str(getattr(result, "comment", ""))[:300],
                    "executed_at": outcome.executed_at,
                },
            )
            return outcome
        except (MT5DemoError, DataUnavailable) as exc:
            return self._reject(str(exc))
        except Exception as exc:
            return self._reject(f"Unexpected MT5 execution error: {type(exc).__name__}")

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def _reject(self, reason: str) -> MT5OrderOutcome:
        self.record_event(
            "MT5_DEMO_ORDER_REJECTED",
            {"symbol": self.settings.trading_symbol, "reason": reason},
        )
        return MT5OrderOutcome(success=False, reason=reason)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.settings.mt5_database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_ledger(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mt5_daily_equity (
                    login INTEGER NOT NULL,
                    server TEXT NOT NULL,
                    utc_date TEXT NOT NULL,
                    opening_equity REAL NOT NULL CHECK (opening_equity > 0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (login, server, utc_date)
                );
                CREATE TABLE IF NOT EXISTS mt5_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        event_type: str,
        detail: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO mt5_events (event_type, created_at, detail_json) VALUES (?, ?, ?)",
            (event_type, created_at, json.dumps(detail, sort_keys=True, default=str)),
        )
