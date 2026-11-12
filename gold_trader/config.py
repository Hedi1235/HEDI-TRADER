"""Environment-backed settings shared by the paper and local MT5 demo runners."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _optional_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return _int_env(name, 0)


@dataclass(frozen=True)
class Settings:
    """Validated settings. MT5 credentials are used only by the Windows demo runner."""

    fred_api_key: str | None = None
    yahoo_ticker: str = "GC=F"
    market_period: str = "60d"
    market_interval: str = "15m"
    market_max_age_minutes: int = 45
    interval_minutes: int = 15
    fast_ema_period: int = 50
    slow_ema_period: int = 200
    support_resistance_lookback: int = 48
    max_risk_per_trade_pct: float = 0.01
    max_daily_drawdown_pct: float = 0.03
    default_stop_loss_usd: float = 8.0
    default_take_profit_usd: float = 16.0
    contract_size_ounces: float = 100.0
    minimum_lot: float = 0.01
    lot_step: float = 0.01
    maximum_lots: float = 2.0
    initial_paper_equity: float = 10_000.0
    minimum_decision_confidence: float = 0.55
    minimum_signal_strength: float = 0.30
    max_open_positions: int = 1
    database_path: Path = Path("trading_bot.db")
    headlines_path: Path = Path("news/headlines.json")
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_terminal_path: Path | None = None
    trading_symbol: str = "XAUUSD"
    mt5_deviation_points: int = 20
    minimum_margin_level_pct: float = 200.0
    maximum_free_margin_usage_pct: float = 0.80
    mt5_database_path: Path = Path("mt5_demo_trades.db")

    def __post_init__(self) -> None:
        if not 0 < self.max_risk_per_trade_pct <= 0.01:
            raise ValueError("MAX_RISK_PER_TRADE_PCT must be greater than 0 and no more than 0.01")
        if not 0 < self.max_daily_drawdown_pct <= 0.03:
            raise ValueError("MAX_DAILY_DRAWDOWN_PCT must be greater than 0 and no more than 0.03")
        if self.default_stop_loss_usd <= 0 or self.default_take_profit_usd <= 0:
            raise ValueError("Stop-loss and take-profit distances must be positive")
        if self.contract_size_ounces <= 0 or self.initial_paper_equity <= 0:
            raise ValueError("Contract size and initial paper equity must be positive")
        if self.minimum_lot <= 0 or self.lot_step <= 0 or self.maximum_lots < self.minimum_lot:
            raise ValueError("Lot limits must be positive and maximum lots must cover the minimum lot")
        if self.market_max_age_minutes <= 0 or self.interval_minutes <= 0:
            raise ValueError("Market age and polling interval must be positive")
        if self.support_resistance_lookback <= 0:
            raise ValueError("Support/resistance lookback must be positive")
        if self.fast_ema_period <= 0 or self.slow_ema_period <= self.fast_ema_period:
            raise ValueError("EMA periods must be positive and the slow period must exceed the fast period")
        if not 0 <= self.minimum_decision_confidence <= 1:
            raise ValueError("MINIMUM_DECISION_CONFIDENCE must be between 0 and 1")
        if not 0 <= self.minimum_signal_strength <= 1:
            raise ValueError("MINIMUM_SIGNAL_STRENGTH must be between 0 and 1")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least 1")
        if self.mt5_deviation_points <= 0:
            raise ValueError("MT5_DEVIATION_POINTS must be positive")
        if self.minimum_margin_level_pct < 0:
            raise ValueError("MINIMUM_MARGIN_LEVEL_PCT cannot be negative")
        if not 0 < self.maximum_free_margin_usage_pct <= 1:
            raise ValueError("MAXIMUM_FREE_MARGIN_USAGE_PCT must be in the interval (0, 1]")
        supplied_mt5_credentials = (
            self.mt5_login is not None,
            bool(self.mt5_password),
            bool(self.mt5_server),
        )
        if any(supplied_mt5_credentials) and not all(supplied_mt5_credentials):
            raise ValueError("MT5_LOGIN, MT5_PASSWORD, and MT5_SERVER must be supplied together")
        if self.mt5_login is not None and self.mt5_login <= 0:
            raise ValueError("MT5_LOGIN must be a positive account number")
        if not self.trading_symbol.strip():
            raise ValueError("TRADING_SYMBOL cannot be empty")

    @property
    def mt5_credentials_configured(self) -> bool:
        return self.mt5_login is not None and bool(self.mt5_password) and bool(self.mt5_server)

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from Replit environment variables or a local .env file."""

        return cls(
            fred_api_key=os.getenv("FRED_API_KEY") or None,
            yahoo_ticker=os.getenv("YAHOO_TICKER", "GC=F"),
            market_period=os.getenv("MARKET_PERIOD", "60d"),
            market_interval=os.getenv("MARKET_INTERVAL", "15m"),
            market_max_age_minutes=_int_env("MARKET_MAX_AGE_MINUTES", 45),
            interval_minutes=_int_env("INTERVAL_MINUTES", 15),
            fast_ema_period=_int_env("FAST_EMA_PERIOD", 50),
            slow_ema_period=_int_env("SLOW_EMA_PERIOD", 200),
            support_resistance_lookback=_int_env("SUPPORT_RESISTANCE_LOOKBACK", 48),
            max_risk_per_trade_pct=_float_env("MAX_RISK_PER_TRADE_PCT", 0.01),
            max_daily_drawdown_pct=_float_env("MAX_DAILY_DRAWDOWN_PCT", 0.03),
            default_stop_loss_usd=_float_env("DEFAULT_STOP_LOSS_USD", 8.0),
            default_take_profit_usd=_float_env("DEFAULT_TAKE_PROFIT_USD", 16.0),
            contract_size_ounces=_float_env("CONTRACT_SIZE_OUNCES", 100.0),
            minimum_lot=_float_env("MINIMUM_LOT", 0.01),
            lot_step=_float_env("LOT_STEP", 0.01),
            maximum_lots=_float_env("MAXIMUM_LOTS", 2.0),
            initial_paper_equity=_float_env("INITIAL_PAPER_EQUITY", 10_000.0),
            minimum_decision_confidence=_float_env("MINIMUM_DECISION_CONFIDENCE", 0.55),
            minimum_signal_strength=_float_env("MINIMUM_SIGNAL_STRENGTH", 0.30),
            max_open_positions=_int_env("MAX_OPEN_POSITIONS", 1),
            database_path=Path(os.getenv("DATABASE_PATH", "trading_bot.db")),
            headlines_path=Path(os.getenv("HEADLINES_PATH", "news/headlines.json")),
            mt5_login=_optional_int_env("MT5_LOGIN"),
            mt5_password=os.getenv("MT5_PASSWORD") or None,
            mt5_server=os.getenv("MT5_SERVER") or None,
            mt5_terminal_path=(
                Path(os.environ["MT5_TERMINAL_PATH"])
                if os.getenv("MT5_TERMINAL_PATH")
                else None
            ),
            trading_symbol=os.getenv("TRADING_SYMBOL", "XAUUSD"),
            mt5_deviation_points=_int_env("MT5_DEVIATION_POINTS", 20),
            minimum_margin_level_pct=_float_env("MINIMUM_MARGIN_LEVEL_PCT", 200.0),
            maximum_free_margin_usage_pct=_float_env("MAXIMUM_FREE_MARGIN_USAGE_PCT", 0.80),
            mt5_database_path=Path(os.getenv("MT5_DATABASE_PATH", "mt5_demo_trades.db")),
        )
