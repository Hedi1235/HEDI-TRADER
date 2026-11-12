"""Market and macroeconomic data ingestion with explicit stale-data checks."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gold_trader.config import Settings


class DataUnavailable(RuntimeError):
    """Raised when required external data is missing, invalid, or stale."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PriceBar(StrictModel):
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "PriceBar":
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values must sit inside the candle high/low range")
        if self.low > self.high:
            raise ValueError("Candle low cannot be above candle high")
        return self


class MarketSnapshot(StrictModel):
    ticker: str
    latest_bar: PriceBar
    rsi_14: float = Field(ge=0, le=100)
    ema_50: float = Field(gt=0)
    ema_200: float = Field(gt=0)
    ema_50_angle_deg: float
    ema_200_angle_deg: float
    support: float = Field(gt=0)
    resistance: float = Field(gt=0)
    candles_available: int = Field(ge=200)


class MacroSnapshot(StrictModel):
    observed_through: date
    nominal_10y_pct: float
    nominal_10y_change_pp: float
    real_10y_pct: float
    real_10y_change_pp: float
    cpi_yoy_pct: float
    cpi_yoy_change_pp: float
    broad_usd_index: float
    broad_usd_change_pct: float


@dataclass(frozen=True)
class FredObservation:
    observed_on: date
    value: float


class FredClient:
    """Small FRED observations client. Never includes its API key in errors."""

    endpoint = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str | None, session: requests.Session | None = None) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()

    def observations(self, series_id: str, limit: int) -> list[FredObservation]:
        if not self.api_key:
            raise DataUnavailable("FRED_API_KEY is not configured")
        try:
            response = self.session.get(
                self.endpoint,
                params={
                    "series_id": series_id,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": limit,
                },
                timeout=(5, 20),
            )
        except requests.RequestException as exc:
            raise DataUnavailable("FRED request failed because of a network error") from exc
        if response.status_code != 200:
            raise DataUnavailable(f"FRED returned HTTP {response.status_code} for {series_id}")
        try:
            payload: dict[str, Any] = response.json()
            raw_observations = payload["observations"]
            values = [
                FredObservation(
                    observed_on=date.fromisoformat(item["date"]),
                    value=float(item["value"]),
                )
                for item in raw_observations
                if item.get("value") not in (None, "", ".")
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailable(f"FRED returned invalid observations for {series_id}") from exc
        if not values:
            raise DataUnavailable(f"FRED returned no observations for {series_id}")
        if any(not math.isfinite(observation.value) for observation in values):
            raise DataUnavailable(f"FRED returned a non-finite value for {series_id}")
        values.sort(key=lambda observation: observation.observed_on)
        return values

    def macro_snapshot(self) -> MacroSnapshot:
        nominal = self.observations("DGS10", 6)
        real = self.observations("DFII10", 6)
        cpi = self.observations("CPIAUCSL", 26)
        broad_usd = self.observations("DTWEXBGS", 6)
        today = datetime.now(UTC).date()
        for series_id, latest, max_age_days in (
            ("DGS10", nominal[-1], 14),
            ("DFII10", real[-1], 14),
            ("CPIAUCSL", cpi[-1], 75),
            ("DTWEXBGS", broad_usd[-1], 14),
        ):
            if (today - latest.observed_on).days > max_age_days:
                raise DataUnavailable(f"FRED {series_id} observation is stale")
        if len(cpi) < 14:
            raise DataUnavailable("FRED CPI series does not contain enough monthly history")
        if cpi[-13].value <= 0 or cpi[-14].value <= 0:
            raise DataUnavailable("FRED CPI series contains an invalid base observation")

        cpi_yoy = (cpi[-1].value / cpi[-13].value - 1.0) * 100.0
        previous_cpi_yoy = (cpi[-2].value / cpi[-14].value - 1.0) * 100.0

        def change(observations: list[FredObservation]) -> float:
            if len(observations) < 2:
                raise DataUnavailable("FRED series contains fewer than two valid observations")
            return observations[-1].value - observations[max(0, len(observations) - 6)].value

        if broad_usd[-1].value <= 0 or broad_usd[0].value <= 0:
            raise DataUnavailable("FRED broad dollar series contains an invalid value")

        return MacroSnapshot(
            observed_through=min(
                nominal[-1].observed_on,
                real[-1].observed_on,
                cpi[-1].observed_on,
                broad_usd[-1].observed_on,
            ),
            nominal_10y_pct=nominal[-1].value,
            nominal_10y_change_pp=change(nominal),
            real_10y_pct=real[-1].value,
            real_10y_change_pp=change(real),
            cpi_yoy_pct=cpi_yoy,
            cpi_yoy_change_pp=cpi_yoy - previous_cpi_yoy,
            broad_usd_index=broad_usd[-1].value,
            broad_usd_change_pct=(broad_usd[-1].value / broad_usd[0].value - 1.0) * 100.0,
        )


def _ema_angle(series: pd.Series, lookback: int = 5) -> float:
    if len(series) <= lookback:
        return 0.0
    previous = float(series.iloc[-lookback - 1])
    current = float(series.iloc[-1])
    if previous <= 0:
        return 0.0
    normalized_percent_per_bar = ((current / previous) - 1.0) * 100.0 / lookback
    return math.degrees(math.atan(normalized_percent_per_bar))


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().iloc[-1]
    average_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().iloc[-1]
    if pd.isna(average_gain) or pd.isna(average_loss):
        raise DataUnavailable("Not enough price history to calculate RSI")
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    return float(100.0 - (100.0 / (1.0 + average_gain / average_loss)))


def market_snapshot_from_frame(
    settings: Settings,
    ticker: str,
    frame: pd.DataFrame,
) -> MarketSnapshot:
    """Build the shared indicators from Yahoo or MT5 OHLC bars."""

    if frame.empty or not {"Open", "High", "Low", "Close"}.issubset(frame.columns):
        raise DataUnavailable(f"{ticker} returned no usable OHLC bars")

    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    required = max(settings.slow_ema_period + 2, settings.support_resistance_lookback)
    if len(frame) < required:
        raise DataUnavailable(f"Only {len(frame)} candles are available; at least {required} are required")

    timestamp = pd.Timestamp(frame.index[-1])
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    observed_at = timestamp.to_pydatetime().astimezone(UTC)
    age = datetime.now(UTC) - observed_at
    if age > timedelta(minutes=settings.market_max_age_minutes):
        raise DataUnavailable(f"Latest market candle is stale ({int(age.total_seconds() // 60)} minutes old)")
    if age < timedelta(minutes=-5):
        raise DataUnavailable("Latest market candle timestamp is unexpectedly in the future")

    close = frame["Close"].astype(float)
    ema_fast = close.ewm(span=settings.fast_ema_period, adjust=False).mean()
    ema_slow = close.ewm(span=settings.slow_ema_period, adjust=False).mean()
    lookback = settings.support_resistance_lookback
    latest = frame.iloc[-1]
    bar = PriceBar(
        timestamp=observed_at,
        open=float(latest["Open"]),
        high=float(latest["High"]),
        low=float(latest["Low"]),
        close=float(latest["Close"]),
    )
    if bar.low > bar.high:
        raise DataUnavailable("Market candle has an invalid high/low range")

    support = float(frame["Low"].tail(lookback).astype(float).min())
    resistance = float(frame["High"].tail(lookback).astype(float).max())
    if not all(math.isfinite(value) for value in (support, resistance, bar.close)):
        raise DataUnavailable("Market feed contains non-finite prices")

    return MarketSnapshot(
        ticker=ticker,
        latest_bar=bar,
        rsi_14=_rsi(close),
        ema_50=float(ema_fast.iloc[-1]),
        ema_200=float(ema_slow.iloc[-1]),
        ema_50_angle_deg=_ema_angle(ema_fast),
        ema_200_angle_deg=_ema_angle(ema_slow),
        support=support,
        resistance=resistance,
        candles_available=len(frame),
    )


def fetch_market_snapshot(settings: Settings) -> MarketSnapshot:
    """Fetch Yahoo Finance bars for the paper runner and calculate indicators."""

    try:
        frame = yf.Ticker(settings.yahoo_ticker).history(
            period=settings.market_period,
            interval=settings.market_interval,
            auto_adjust=False,
            actions=False,
            timeout=20,
        )
    except Exception as exc:  # yfinance wraps multiple network/library errors
        raise DataUnavailable(f"Yahoo Finance request failed ({type(exc).__name__})") from exc
    if frame.empty:
        raise DataUnavailable("Yahoo Finance returned no usable OHLC bars")
    return market_snapshot_from_frame(settings, settings.yahoo_ticker, frame)


def load_headlines(path: Path) -> list[str]:
    """Load explicitly supplied headlines; no news scraping is implied."""

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataUnavailable("Configured headlines file could not be read") from exc
    if isinstance(payload, dict):
        payload = payload.get("headlines")
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise DataUnavailable("Headlines input must be a JSON list of strings or an object with a headlines list")
    if len(payload) > 100:
        raise DataUnavailable("At most 100 headlines can be analyzed per cycle")
    return [headline.strip()[:1000] for headline in payload if headline.strip()]
