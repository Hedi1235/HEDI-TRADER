"""SQLite-backed simulated execution. This module has no live-broker adapter."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator

from gold_trader.config import Settings
from gold_trader.data_feeds import PriceBar
from gold_trader.risk_manager import ApprovedTrade


@dataclass(frozen=True)
class AccountSummary:
    balance: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int


class PaperExecutionEngine:
    """Records PAPER orders and simulated exits in a local SQLite database."""

    def __init__(self, settings: Settings) -> None:
        self.db_path = settings.database_path
        self.contract_size_ounces = settings.contract_size_ounces
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(settings.initial_paper_equity)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, initial_equity: float) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_equity REAL NOT NULL CHECK (initial_equity > 0),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_equity (
                    utc_date TEXT PRIMARY KEY,
                    opening_equity REAL NOT NULL CHECK (opening_equity > 0),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_mode TEXT NOT NULL DEFAULT 'PAPER' CHECK (execution_mode = 'PAPER'),
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    entry_price REAL NOT NULL CHECK (entry_price > 0),
                    stop_loss REAL NOT NULL CHECK (stop_loss > 0),
                    take_profit REAL NOT NULL CHECK (take_profit > 0),
                    lots REAL NOT NULL CHECK (lots > 0),
                    risk_amount REAL NOT NULL CHECK (risk_amount > 0),
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED_SL', 'CLOSED_TP')),
                    opened_at TEXT NOT NULL,
                    mark_price REAL NOT NULL CHECK (mark_price > 0),
                    closed_at TEXT,
                    exit_price REAL,
                    pnl REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO account (id, initial_equity, created_at)
                VALUES (1, ?, ?)
                """,
                (initial_equity, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _direction_sign(side: str) -> float:
        return 1.0 if side == "BUY" else -1.0

    def account_summary(self) -> AccountSummary:
        with self._connection() as connection:
            account = connection.execute(
                "SELECT initial_equity FROM account WHERE id = 1"
            ).fetchone()
            realized = connection.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE status != 'OPEN'"
            ).fetchone()["pnl"]
            open_trades = connection.execute(
                """
                SELECT side, entry_price, mark_price, lots
                FROM trades WHERE status = 'OPEN'
                """
            ).fetchall()
        initial_equity = float(account["initial_equity"])
        realized_pnl = float(realized)
        unrealized_pnl = sum(
            self._direction_sign(row["side"])
            * (float(row["mark_price"]) - float(row["entry_price"]))
            * float(row["lots"])
            * self.contract_size_ounces
            for row in open_trades
        )
        balance = initial_equity + realized_pnl
        return AccountSummary(
            balance=balance,
            equity=balance + unrealized_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_positions=len(open_trades),
        )

    def daily_opening_equity(self, now: datetime | None = None) -> float:
        timestamp = now or datetime.now(UTC)
        utc_date = timestamp.astimezone(UTC).date().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT opening_equity FROM daily_equity WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            if row is not None:
                return float(row["opening_equity"])
            summary = self.account_summary()
            connection.execute(
                """
                INSERT OR IGNORE INTO daily_equity (utc_date, opening_equity, created_at)
                VALUES (?, ?, ?)
                """,
                (utc_date, summary.equity, timestamp.astimezone(UTC).isoformat()),
            )
            saved = connection.execute(
                "SELECT opening_equity FROM daily_equity WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
        return float(saved["opening_equity"])

    def open_paper_trade(self, trade: ApprovedTrade) -> int:
        """Persist a validated simulation; no broker or network order call exists."""

        opened_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trades (
                    execution_mode, side, entry_price, stop_loss, take_profit,
                    lots, risk_amount, confidence, rationale, status, opened_at, mark_price
                ) VALUES ('PAPER', ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    trade.side,
                    trade.entry_price,
                    trade.stop_loss,
                    trade.take_profit,
                    trade.lots,
                    trade.risk_amount,
                    trade.confidence,
                    trade.rationale,
                    opened_at,
                    trade.entry_price,
                ),
            )
            trade_id = int(cursor.lastrowid)
            self._insert_event(
                connection,
                "PAPER_ORDER_OPENED",
                {"trade_id": trade_id, "side": trade.side, "lots": trade.lots},
                opened_at,
            )
        return trade_id

    def update_open_positions(self, bar: PriceBar) -> list[dict[str, Any]]:
        """Mark open simulations to market and close touched stops/targets.

        If both stop and target fall within one candle, the stop is assumed to
        fill first. This is the conservative treatment when tick ordering is
        unavailable.
        """

        closed: list[dict[str, Any]] = []
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            trades = connection.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id"
            ).fetchall()
            for row in trades:
                side = row["side"]
                stop = float(row["stop_loss"])
                target = float(row["take_profit"])
                stop_hit = bar.low <= stop if side == "BUY" else bar.high >= stop
                target_hit = bar.high >= target if side == "BUY" else bar.low <= target
                status: str | None = None
                exit_price: float | None = None
                if stop_hit:
                    status, exit_price = "CLOSED_SL", stop
                elif target_hit:
                    status, exit_price = "CLOSED_TP", target

                if status is None:
                    connection.execute(
                        "UPDATE trades SET mark_price = ? WHERE id = ? AND status = 'OPEN'",
                        (bar.close, row["id"]),
                    )
                    continue

                pnl = (
                    self._direction_sign(side)
                    * (exit_price - float(row["entry_price"]))
                    * float(row["lots"])
                    * self.contract_size_ounces
                )
                connection.execute(
                    """
                    UPDATE trades
                    SET status = ?, mark_price = ?, closed_at = ?, exit_price = ?, pnl = ?
                    WHERE id = ? AND status = 'OPEN'
                    """,
                    (status, exit_price, now, exit_price, pnl, row["id"]),
                )
                detail = {
                    "trade_id": int(row["id"]),
                    "status": status,
                    "exit_price": exit_price,
                    "pnl": pnl,
                }
                self._insert_event(connection, status, detail, now)
                closed.append(detail)
        return closed

    def record_event(self, event_type: str, detail: dict[str, Any]) -> None:
        with self._connection() as connection:
            self._insert_event(connection, event_type, detail, datetime.now(UTC).isoformat())

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, created_at, detail_json
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event_type: str,
        detail: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO events (event_type, created_at, detail_json) VALUES (?, ?, ?)",
            (event_type, created_at, json.dumps(detail, default=str, sort_keys=True)),
        )
