import asyncio
import logging
from typing import Any

import aiohttp
import pandas as pd

from config import (
    BINANCE_BASE_URL,
    CANDLE_LIMIT,
    SYMBOL,
    TIMEFRAMES,
)


logger = logging.getLogger(__name__)


# =========================================================
# HTTP REQUEST
# =========================================================

async def binance_request(
    endpoint: str,
    params: dict[str, Any],
) -> Any:
    url = f"{BINANCE_BASE_URL}{endpoint}"

    timeout = aiohttp.ClientTimeout(
        total=25,
        connect=10,
        sock_read=20,
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "JanSOLSignalBot/3.0",
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(
            url,
            params=params,
        ) as response:
            response_text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Binance HTTP {response.status}: "
                    f"{response_text[:300]}"
                )

            try:
                return await response.json()

            except Exception as error:
                raise RuntimeError(
                    "Binance returned invalid JSON: "
                    f"{response_text[:300]}"
                ) from error


# =========================================================
# LIVE TICKER
# =========================================================

async def get_ticker_24h() -> dict[str, Any]:
    result = await binance_request(
        endpoint="/api/v3/ticker/24hr",
        params={
            "symbol": SYMBOL,
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "Binance returned an invalid ticker response."
        )

    required_fields = [
        "lastPrice",
        "priceChangePercent",
        "highPrice",
        "lowPrice",
        "quoteVolume",
    ]

    missing_fields = [
        field
        for field in required_fields
        if field not in result
    ]

    if missing_fields:
        raise RuntimeError(
            "Ticker response is missing: "
            + ", ".join(missing_fields)
        )

    return result


# =========================================================
# CANDLE DATA
# =========================================================

async def get_klines(
    interval: str,
    limit: int = CANDLE_LIMIT,
    remove_open_candle: bool = True,
) -> pd.DataFrame:
    result = await binance_request(
        endpoint="/api/v3/klines",
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit,
        },
    )

    if not isinstance(result, list):
        raise RuntimeError(
            f"Invalid Binance candle response "
            f"for {interval}."
        )

    if len(result) < 210:
        raise RuntimeError(
            f"Only {len(result)} candles returned "
            f"for {interval}."
        )

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trade_count",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore",
    ]

    frame = pd.DataFrame(
        result,
        columns=columns,
    )

    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trade_count",
        "taker_buy_base",
        "taker_buy_quote",
    ]

    for column in numeric_columns:
        frame[column] = pd.to_numeric(
            frame[column],
            errors="coerce",
        )

    frame["open_time"] = pd.to_datetime(
        frame["open_time"],
        unit="ms",
        utc=True,
    )

    frame["close_time"] = pd.to_datetime(
        frame["close_time"],
        unit="ms",
        utc=True,
    )

    frame = frame.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ).reset_index(drop=True)

    if remove_open_candle and len(frame) > 1:
        frame = frame.iloc[:-1].copy()

    if len(frame) < 200:
        raise RuntimeError(
            f"Only {len(frame)} usable candles "
            f"remain for {interval}."
        )

    return frame


# =========================================================
# MULTI-TIMEFRAME DOWNLOAD
# =========================================================

async def get_all_timeframes(
    remove_open_candle: bool = True,
) -> tuple[
    dict[str, pd.DataFrame],
    dict[str, str],
]:
    tasks = {
        interval: asyncio.create_task(
            get_klines(
                interval=interval,
                remove_open_candle=remove_open_candle,
            )
        )
        for interval in TIMEFRAMES
    }

    candle_data: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}

    for interval, task in tasks.items():
        try:
            candle_data[interval] = await task

        except Exception as error:
            logger.exception(
                "Candle request failed for %s",
                interval,
            )

            errors[interval] = (
                f"{type(error).__name__}: {error}"
            )

    return candle_data, errors


# =========================================================
# CURRENT MARKET SNAPSHOT
# =========================================================

async def get_market_snapshot() -> dict[str, Any]:
    ticker_task = asyncio.create_task(
        get_ticker_24h()
    )

    candle_task = asyncio.create_task(
        get_all_timeframes()
    )

    ticker = await ticker_task

    candle_data, errors = await candle_task

    return {
        "ticker": ticker,
        "candles": candle_data,
        "errors": errors,
    }
