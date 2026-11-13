"""Windows-only MetaTrader 5 bridge with configurable account-mode safeguards."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import sys
import time
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

LOGGER = logging.getLogger("gold_trader")


class MT5ExecutionError(RuntimeError):
    """Raised when the terminal connection or account safety checks fail."""


MT5DemoError = MT5ExecutionError


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
    trade_mode: str
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


class MT5Execution:
    """Local-terminal execution bridge with explicit account-mode checks."""

    reconnect_attempts = 5
    reconnect_initial_delay_seconds = 1.0

    def __init__(
        self,
        settings: Settings,
        *,
        mt5_module: Any | None = None,
        enforce_windows: bool = True,
    ) -> None:
        self.settings = settings
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
        self.ensure_connected()
        self.symbol_info()
        reconciled = self.positions()
        account = self.account_snapshot()
        self.daily_opening_equity(account)
        LOGGER.info(
            "Reconciled %d open MT5 position(s); account mode=%s",
            len(reconciled),
            account.trade_mode,
        )

    def _initialize_terminal(self) -> None:
        args: dict[str, Any] = {"timeout": 15_000}
        if self.settings.mt5_credentials_configured:
            login = self.settings.mt5_login
            password = self.settings.mt5_password
            server = self.settings.mt5_server
            assert login is not None and password is not None and server is not None
            args.update(login=login, password=password, server=server)
        terminal_path = self.settings.mt5_terminal_path
        if terminal_path is not None:
            if not terminal_path.is_file():
                raise MT5ExecutionError("Configured MT5 terminal executable was not found")
            initialized = self.mt5.initialize(str(terminal_path), **args)
        else:
            initialized = self.mt5.initialize(**args)
        if not initialized:
            last_error = self.mt5.last_error()
            error_code = last_error[0] if isinstance(last_error, tuple) and last_error else "unknown"
            raise MT5ExecutionError(
                f"MT5 terminal initialization failed (error code {error_code})"
            )

        terminal = self.mt5.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", False)):
            raise MT5ExecutionError("MT5 terminal is not connected to its trade server")
        self._assert_account()

    def ensure_connected(self) -> None:
        """Reconnect with exponential backoff if the desktop terminal drops."""

        last_error: Exception | None = None
        for attempt in range(self.reconnect_attempts):
            try:
                terminal = self.mt5.terminal_info()
                if terminal is not None and bool(getattr(terminal, "connected", False)):
                    self._assert_account()
                    return
                self.mt5.shutdown()
                self._initialize_terminal()
                return
            except MT5ExecutionError as exc:
                if self._terminal_is_connected():
                    raise
                last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt + 1 < self.reconnect_attempts:
                delay = self.reconnect_initial_delay_seconds * (2**attempt)
                LOGGER.warning(
                    "MT5 connection attempt %d/%d failed; retrying in %.1f seconds",
                    attempt + 1,
                    self.reconnect_attempts,
                    delay,
                )
                time.sleep(delay)
        raise MT5ExecutionError(
            f"MT5 terminal reconnect failed after {self.reconnect_attempts} attempts"
        ) from last_error

    def _terminal_is_connected(self) -> bool:
        try:
            terminal = self.mt5.terminal_info()
        except Exception:
            return False
        return terminal is not None and bool(getattr(terminal, "connected", False))

    def _mt5_call(
        self,
        method_name: str,
        *args: Any,
        retry_safe: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Retry read-only calls only; order submissions are never replayed."""

        for attempt in range(self.reconnect_attempts):
            self.ensure_connected()
            try:
                result = getattr(self.mt5, method_name)(*args, **kwargs)
            except Exception as exc:
                if not retry_safe or self._terminal_is_connected():
                    raise
                if attempt + 1 == self.reconnect_attempts:
                    raise MT5ExecutionError(
                        f"MT5 connection was lost during {method_name}"
                    ) from exc
            else:
                if not retry_safe or self._terminal_is_connected():
                    return result
                if attempt + 1 == self.reconnect_attempts:
                    raise MT5ExecutionError(
                        f"MT5 connection was lost during {method_name}"
                    )
            delay = self.reconnect_initial_delay_seconds * (2**attempt)
            LOGGER.warning(
                "MT5 disconnected during %s; retrying in %.1f seconds",
                method_name,
                delay,
            )
            time.sleep(delay)
        raise MT5ExecutionError(f"MT5 {method_name} failed after reconnect retries")

    def _assert_account(self) -> Any:
        account = self.mt5.account_info()
        if account is None:
            raise MT5ExecutionError("MT5 did not return account information")
        account_mode = getattr(account, "trade_mode", None)
        demo_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        if self.settings.mt5_account_mode == "demo":
            if demo_mode is None or account_mode != demo_mode:
                raise MT5ExecutionError(
                    "Refusing to trade: the connected account is not confirmed as DEMO"
                )
        else:
            known_modes = {
                getattr(self.mt5, name, None)
                for name in (
                    "ACCOUNT_TRADE_MODE_DEMO",
                    "ACCOUNT_TRADE_MODE_CONTEST",
                    "ACCOUNT_TRADE_MODE_REAL",
                )
            }
            known_modes.discard(None)
            if account_mode not in known_modes:
                raise MT5ExecutionError("MT5 account type could not be confirmed")
        if (
            self.settings.mt5_login is not None
            and int(getattr(account, "login", 0)) != self.settings.mt5_login
        ):
            raise MT5ExecutionError("Connected MT5 account does not match MT5_LOGIN")
        if self.settings.mt5_server is not None and str(
            getattr(account, "server", "")
        ).casefold() != self.settings.mt5_server.casefold():
            raise MT5ExecutionError("Connected MT5 server does not match MT5_SERVER")
        return account

    def symbol_info(self) -> Any:
        self.ensure_connected()
        symbol = self.settings.trading_symbol
        info = self._mt5_call("symbol_info", symbol)
        if info is None:
            raise MT5ExecutionError(f"MT5 does not recognize trading symbol {symbol}")
        if not bool(getattr(info, "visible", False)):
            if not self._mt5_call("symbol_select", symbol, True):
                raise MT5ExecutionError(f"Could not enable {symbol} in Market Watch")
            info = self._mt5_call("symbol_info", symbol)
        if info is None or not bool(getattr(info, "visible", False)):
            raise MT5ExecutionError(f"Trading symbol {symbol} is not visible in Market Watch")
        disabled = getattr(self.mt5, "SYMBOL_TRADE_MODE_DISABLED", None)
        if disabled is not None and getattr(info, "trade_mode", None) == disabled:
            raise MT5ExecutionError(f"Trading is disabled for {symbol}")
        for field in ("point", "trade_contract_size", "volume_min", "volume_step", "volume_max"):
            value = float(getattr(info, field, 0.0) or 0.0)
            if not math.isfinite(value) or value <= 0:
                raise MT5ExecutionError(f"MT5 returned invalid {field} for {symbol}")
        return info

    def account_snapshot(self) -> MT5Account:
        self.ensure_connected()
        raw = self._assert_account()
        terminal = self._mt5_call("terminal_info")
        trade_allowed = bool(getattr(raw, "trade_allowed", False)) and bool(
            getattr(terminal, "trade_allowed", False)
        )
        margin_level = getattr(raw, "margin_level", None)
        balance = float(raw.balance)
        equity = float(raw.equity)
        margin = float(raw.margin or 0.0)
        margin_free = float(raw.margin_free or 0.0)
        if (
            not all(math.isfinite(value) for value in (balance, equity, margin, margin_free))
            or balance <= 0
            or equity <= 0
            or margin < 0
            or margin_free < 0
        ):
            raise MT5ExecutionError("MT5 returned invalid account balance or margin data")
        if margin_level is not None and (
            not math.isfinite(float(margin_level)) or float(margin_level) < 0
        ):
            raise MT5ExecutionError("MT5 returned an invalid margin level")
        mode_value = getattr(raw, "trade_mode", None)
        mode_labels = {
            getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", object()): "DEMO",
            getattr(self.mt5, "ACCOUNT_TRADE_MODE_CONTEST", object()): "CONTEST",
            getattr(self.mt5, "ACCOUNT_TRADE_MODE_REAL", object()): "REAL",
        }
        return MT5Account(
            login=int(raw.login),
            server=str(raw.server),
            trade_mode=mode_labels.get(mode_value, "UNKNOWN"),
            balance=balance,
            equity=equity,
            margin=margin,
            margin_free=margin_free,
            margin_level=float(margin_level) if margin_level is not None else None,
            trade_allowed=trade_allowed,
        )

    def positions(self) -> tuple[Any, ...]:
        self.ensure_connected()
        positions = self._mt5_call("positions_get")
        if positions is None:
            error = self.mt5.last_error()
            error_code = error[0] if isinstance(error, tuple) and error else "unknown"
            raise MT5DemoError(f"MT5 position query failed (error code {error_code})")
        current_positions = tuple(positions)
        account = self._assert_account()
        self._reconcile_positions(account, current_positions)
        return current_positions

    def market_data(self) -> MT5MarketData:
        """Use broker OHLC and bid/ask quotes so signal and execution prices match."""

        self.ensure_connected()
        info = self.symbol_info()
        required_bars = max(self.settings.slow_ema_period + 10, 500)
        rates = self._mt5_call(
            "copy_rates_from_pos",
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

        tick = self._mt5_call("symbol_info_tick", self.settings.trading_symbol)
        if tick is None:
            raise DataUnavailable("MT5 did not return a current bid/ask quote")
        bid, ask = float(tick.bid), float(tick.ask)
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
            raise DataUnavailable("MT5 returned an invalid bid/ask quote")
        tick_time = float(getattr(tick, "time", 0) or 0)
        quote_age = datetime.now(UTC) - datetime.fromtimestamp(tick_time, tz=UTC)
        if quote_age > timedelta(minutes=2) or quote_age < timedelta(minutes=-60):
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

    def stop_risk_per_lot(self, side: str, entry_price: float, stop_loss: float) -> float:
        """Return broker-estimated stop loss in the account's deposit currency."""

        order_type = (
            self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
        )
        loss = self._mt5_call(
            "order_calc_profit",
            order_type,
            self.settings.trading_symbol,
            1.0,
            entry_price,
            stop_loss,
        )
        if loss is None or not math.isfinite(float(loss)) or float(loss) >= 0:
            raise DataUnavailable("MT5 could not calculate a valid stop-loss amount")
        return abs(float(loss))

    def record_event(self, event_type: str, detail: dict[str, Any]) -> None:
        with self._connection() as connection:
            self._record_event(connection, event_type, detail, datetime.now(UTC).isoformat())

    def send_mt5_order(self, trade: ApprovedTrade) -> MT5OrderOutcome:
        """Check account, symbol, margin, and stop risk before one market order."""

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
            tick = self._mt5_call("symbol_info_tick", self.settings.trading_symbol)
            if tick is None:
                return self._reject("MT5 did not return a current bid/ask quote")
            tick_time = float(getattr(tick, "time", 0) or 0)
            quote_age = datetime.now(UTC) - datetime.fromtimestamp(tick_time, tz=UTC)
            if quote_age > timedelta(minutes=2) or quote_age < timedelta(minutes=-1):
                return self._reject("MT5 execution quote is stale or has a future timestamp")
            order_type = (
                self.mt5.ORDER_TYPE_BUY if trade.side == "BUY" else self.mt5.ORDER_TYPE_SELL
            )
            price = float(tick.ask if trade.side == "BUY" else tick.bid)
            if not math.isfinite(price) or price <= 0:
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

            margin_required = self._mt5_call(
                "order_calc_margin",
                order_type,
                self.settings.trading_symbol,
                trade.lots,
                price,
            )
            if (
                margin_required is None
                or not math.isfinite(float(margin_required))
                or float(margin_required) < 0
            ):
                return self._reject("MT5 could not calculate required margin")
            available_margin = account.margin_free * self.settings.maximum_free_margin_usage_pct
            if float(margin_required) > available_margin:
                return self._reject("Required margin exceeds the configured free-margin budget")

            actual_stop_loss = self._mt5_call(
                "order_calc_profit",
                order_type,
                self.settings.trading_symbol,
                trade.lots,
                price,
                trade.stop_loss,
            )
            if (
                actual_stop_loss is None
                or not math.isfinite(float(actual_stop_loss))
                or float(actual_stop_loss) >= 0
            ):
                return self._reject("MT5 could not confirm the order's stop-loss risk")
            if abs(float(actual_stop_loss)) > (
                account.equity * self.settings.max_risk_per_trade_pct + 1e-8
            ):
                return self._reject("Current quote would exceed the configured per-trade risk")

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
                "comment": "gold-supervisor",
                "type_time": self.mt5.ORDER_TIME_GTC,
                "type_filling": self.mt5.ORDER_FILLING_RETURN,
            }
            check = self._mt5_call("order_check", request)
            if check is None:
                return self._reject("MT5 order_check returned no result")
            check_code = int(getattr(check, "retcode", -1))
            if check_code != 0:
                return self._reject(
                    f"MT5 pre-trade check rejected the order (retcode {check_code})"
                )

            result = self._mt5_call("order_send", request, retry_safe=False)
            if result is None:
                return self._reject(
                    "MT5 order_send returned no result; check terminal positions before retrying"
                )
            retcode = int(getattr(result, "retcode", -1))
            success = retcode == int(self.mt5.TRADE_RETCODE_DONE)
            outcome = MT5OrderOutcome(
                success=success,
                reason="MT5 order executed" if success else "MT5 rejected the order",
                retcode=retcode,
                order_ticket=int(result.order) if getattr(result, "order", 0) else None,
                deal_ticket=int(result.deal) if getattr(result, "deal", 0) else None,
                fill_price=float(result.price) if getattr(result, "price", 0) else None,
                volume=float(getattr(result, "volume", trade.lots) or trade.lots),
                executed_at=datetime.now(UTC).isoformat(),
            )
            self.record_event(
                "MT5_ORDER_EXECUTED" if success else "MT5_ORDER_REJECTED",
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
        except (MT5ExecutionError, DataUnavailable) as exc:
            return self._reject(str(exc))
        except Exception as exc:
            return self._reject(f"Unexpected MT5 execution error: {type(exc).__name__}")

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def _reject(self, reason: str) -> MT5OrderOutcome:
        self.record_event(
            "MT5_ORDER_REJECTED",
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
                CREATE TABLE IF NOT EXISTS mt5_open_positions (
                    login INTEGER NOT NULL,
                    server TEXT NOT NULL,
                    ticket INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    position_type INTEGER NOT NULL,
                    volume REAL NOT NULL CHECK (volume > 0),
                    price_open REAL NOT NULL CHECK (price_open > 0),
                    stop_loss REAL NOT NULL CHECK (stop_loss >= 0),
                    take_profit REAL NOT NULL CHECK (take_profit >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (login, server, ticket)
                );
                """
            )

    def _reconcile_positions(self, account: Any, positions: tuple[Any, ...]) -> None:
        login = int(account.login)
        server = str(account.server)
        timestamp = datetime.now(UTC).isoformat()
        current: dict[int, tuple[str, int, float, float, float, float]] = {}
        for position in positions:
            ticket = int(getattr(position, "ticket", 0) or 0)
            symbol = str(getattr(position, "symbol", ""))
            position_type = int(getattr(position, "type", -1))
            volume = float(getattr(position, "volume", 0.0) or 0.0)
            price_open = float(getattr(position, "price_open", 0.0) or 0.0)
            stop_loss = float(getattr(position, "sl", 0.0) or 0.0)
            take_profit = float(getattr(position, "tp", 0.0) or 0.0)
            if (
                ticket <= 0
                or not symbol
                or volume <= 0
                or price_open <= 0
                or not all(
                    math.isfinite(value)
                    for value in (volume, price_open, stop_loss, take_profit)
                )
            ):
                raise MT5ExecutionError("MT5 returned an invalid open-position record")
            current[ticket] = (
                symbol,
                position_type,
                volume,
                price_open,
                stop_loss,
                take_profit,
            )

        with self._connection() as connection:
            existing_tickets = {
                int(row["ticket"])
                for row in connection.execute(
                    """
                    SELECT ticket FROM mt5_open_positions
                    WHERE login = ? AND server = ?
                    """,
                    (login, server),
                ).fetchall()
            }
            current_tickets = set(current)
            added = sorted(current_tickets - existing_tickets)
            removed = sorted(existing_tickets - current_tickets)
            for ticket, values in current.items():
                connection.execute(
                    """
                    INSERT INTO mt5_open_positions (
                        login, server, ticket, symbol, position_type, volume,
                        price_open, stop_loss, take_profit, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(login, server, ticket) DO UPDATE SET
                        symbol = excluded.symbol,
                        position_type = excluded.position_type,
                        volume = excluded.volume,
                        price_open = excluded.price_open,
                        stop_loss = excluded.stop_loss,
                        take_profit = excluded.take_profit,
                        updated_at = excluded.updated_at
                    """,
                    (login, server, ticket, *values, timestamp),
                )
            if current_tickets:
                placeholders = ",".join("?" for _ in current_tickets)
                connection.execute(
                    f"""
                    DELETE FROM mt5_open_positions
                    WHERE login = ? AND server = ? AND ticket NOT IN ({placeholders})
                    """,
                    (login, server, *sorted(current_tickets)),
                )
            else:
                connection.execute(
                    "DELETE FROM mt5_open_positions WHERE login = ? AND server = ?",
                    (login, server),
                )
            if added or removed:
                detail = {
                    "added_tickets": added,
                    "removed_tickets": removed,
                    "open_position_count": len(current),
                }
                self._record_event(connection, "MT5_POSITION_RECONCILED", detail, timestamp)

        if added or removed:
            LOGGER.info(
                "MT5 position ledger reconciled: %d added, %d closed",
                len(added),
                len(removed),
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


MT5DemoExecution = MT5Execution
