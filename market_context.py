from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

import market
from indicators import clamp
from strategy import MarketSignal, build_market_signal
from news_intelligence import fetch_news_intelligence


logger = logging.getLogger(__name__)


COINGECKO_API_KEY = os.getenv(
    "COINGECKO_API_KEY",
    "",
).strip()

GLOBAL_CRYPTO_CACHE: dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
}

GLOBAL_CRYPTO_CACHE_SECONDS = 300

FEAR_GREED_CACHE: dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
}
FEAR_GREED_CACHE_SECONDS = 300
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=2&format=json"

DERIVATIVES_CACHE: dict[str, dict[str, Any]] = {}
DERIVATIVES_CACHE_SECONDS = 60
COINBASE_PREMIUM_CACHE: dict[str, Any] = {"data": None, "timestamp": 0.0}
COINBASE_PREMIUM_CACHE_SECONDS = 60

COINGECKO_GLOBAL_URL = (
    "https://api.coingecko.com/api/v3/global"
)

FRED_VIX_CSV_URL = (
    "https://fred.stlouisfed.org/graph/"
    "fredgraph.csv?id=VIXCLS"
)

BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
BYBIT_BASE_URL = "https://api.bybit.com"
OKX_BASE_URL = "https://www.okx.com"


# =========================================================
# RESULT MODEL
# =========================================================

@dataclass
class MarketContext:
    available: bool
    original_score: float
    adjusted_score: float
    score_adjustment: float

    macro_score: float
    macro_bias: str
    macro_reasons: list[str]

    btc_score: float
    btc_direction: str

    eth_score: float
    eth_direction: str
    eth_btc_relative_strength: float
    eth_dominance: float

    btc_correlation: float
    correlation_strength: str

    btc_dominance: float
    btc_dominance_effect: str

    crypto_market_change_24h: float

    vix_value: float
    vix_change_percent: float
    vix_regime: str

    fear_greed_value: float
    fear_greed_label: str
    fear_greed_change: float
    fear_greed_live: bool

    btc_coinbase_premium: float
    eth_coinbase_premium: float
    coinbase_premium_live: bool

    funding_rate: float
    funding_label: str
    open_interest_value: float
    open_interest_change_5m: float
    open_interest_change_1h: float
    derivatives_live: bool
    derivatives_adjustment: float
    derivatives_provider: str
    long_liquidations_1h: float
    short_liquidations_1h: float
    liquidation_pressure: str
    orderbook_imbalance: float
    bid_wall_price: float
    bid_wall_strength: float
    ask_wall_price: float
    ask_wall_strength: float
    taker_buy_ratio: float
    taker_flow_imbalance: float
    large_trade_threshold: float
    large_trade_count: int
    large_flow_share: float
    large_flow_imbalance: float
    largest_trade_value: float
    largest_trade_side: str
    largest_trade_multiple: float
    news_score: float
    news_label: str
    news_live: bool

    reasons: list[str]
    warnings: list[str]


# =========================================================
# HTTP HELPERS
# =========================================================

async def fetch_json(
    url: str,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(
        total=25,
        connect=10,
        sock_read=20,
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "JanCryptoSignalBot/2.0",
    }

    if (
        COINGECKO_API_KEY
        and "coingecko.com" in url
    ):
        headers["x-cg-demo-api-key"] = (
            COINGECKO_API_KEY
        )

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                async with session.get(
                    url
                ) as response:
                    text = await response.text()

                    if response.status == 429:
                        retry_after = response.headers.get(
                            "Retry-After",
                            str(5 * (attempt + 1)),
                        )

                        try:
                            delay = float(
                                retry_after
                            )
                        except ValueError:
                            delay = float(
                                5 * (attempt + 1)
                            )

                        logger.warning(
                            "CoinGecko rate limit reached. "
                            "Retrying in %.1f seconds.",
                            delay,
                        )

                        await asyncio.sleep(
                            min(delay, 30)
                        )

                        continue

                    if response.status != 200:
                        raise RuntimeError(
                            f"HTTP {response.status} "
                            f"from {url}: {text[:300]}"
                        )

                    data = await response.json(
                        content_type=None
                    )

                    if not isinstance(
                        data,
                        dict,
                    ):
                        raise RuntimeError(
                            "Provider returned invalid JSON."
                        )

                    return data

        except Exception as error:
            last_error = error

            logger.warning(
                "JSON request attempt %s failed: %s",
                attempt + 1,
                error,
            )

            if attempt < 2:
                await asyncio.sleep(
                    3 * (attempt + 1)
                )

    raise RuntimeError(
        "JSON provider failed after three attempts: "
        f"{last_error}"
    )


def calculate_coinbase_premium(coinbase_mid: float, okx_usdt_mid: float, usdt_usd_mid: float) -> float:
    okx_usd_mid = okx_usdt_mid * usdt_usd_mid
    if coinbase_mid <= 0 or okx_usd_mid <= 0:
        raise ValueError("Premium inputs must be positive")
    return (coinbase_mid / okx_usd_mid - 1.0) * 100.0


async def fetch_coinbase_premiums() -> dict[str, Any]:
    cached = COINBASE_PREMIUM_CACHE.get("data")
    if isinstance(cached, dict) and time.time() - float(COINBASE_PREMIUM_CACHE.get("timestamp", 0.0)) < COINBASE_PREMIUM_CACHE_SECONDS:
        return dict(cached)

    usdt_ticker = await fetch_json("https://api.exchange.coinbase.com/products/USDT-USD/ticker")
    usdt_usd_mid = (float(usdt_ticker["bid"]) + float(usdt_ticker["ask"])) / 2.0

    async def premium(asset: str) -> float:
        coinbase, okx = await asyncio.gather(
            fetch_json(f"https://api.exchange.coinbase.com/products/{asset}-USD/ticker"),
            fetch_json(f"{OKX_BASE_URL}/api/v5/market/ticker?instId={asset}-USDT"),
        )
        okx_rows = okx.get("data", [])
        if not okx_rows:
            raise RuntimeError(f"OKX returned no {asset} spot ticker")
        coinbase_mid = (float(coinbase["bid"]) + float(coinbase["ask"])) / 2.0
        okx_mid = (float(okx_rows[0]["bidPx"]) + float(okx_rows[0]["askPx"])) / 2.0
        return calculate_coinbase_premium(coinbase_mid, okx_mid, usdt_usd_mid)

    btc, eth = await asyncio.gather(premium("BTC"), premium("ETH"))
    result = {
        "btc": round(btc, 4), "eth": round(eth, 4),
        "usdt_usd": round(usdt_usd_mid, 6), "live": True,
    }
    COINBASE_PREMIUM_CACHE.update({"data": dict(result), "timestamp": time.time()})
    return result

async def fetch_text(
    url: str,
) -> str:
    timeout = aiohttp.ClientTimeout(
        total=20,
        connect=8,
        sock_read=15,
    )

    headers = {
        "Accept": "text/csv",
        "User-Agent": "JanCryptoSignalBot/2.0",
    }

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(url) as response:
            text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"HTTP {response.status}: "
                    f"{text[:250]}"
                )

            return text


# =========================================================
# MARKET COMPATIBILITY
# =========================================================

async def fetch_snapshot(
    symbol: str,
) -> dict[str, Any]:
    snapshot_function = getattr(
        market,
        "get_symbol_snapshot",
        None,
    )

    if snapshot_function is not None:
        return await snapshot_function(
            symbol
        )

    timeframe_function = getattr(
        market,
        "get_all_timeframes",
        None,
    )

    if timeframe_function is None:
        raise RuntimeError(
            "market.py has no usable timeframe function."
        )

    try:
        candles, errors = await timeframe_function(
            symbol
        )

    except TypeError:
        candles, errors = await timeframe_function()

    return {
        "symbol": symbol,
        "candles": candles,
        "errors": errors,
    }


# =========================================================
# GLOBAL CRYPTO DATA
# =========================================================

async def fetch_global_crypto() -> dict[str, float]:
    cached_data = GLOBAL_CRYPTO_CACHE.get("data")
    cached_timestamp = float(
        GLOBAL_CRYPTO_CACHE.get("timestamp", 0.0)
    )

    if (
        isinstance(cached_data, dict)
        and time.time() - cached_timestamp
        < GLOBAL_CRYPTO_CACHE_SECONDS
    ):
        return cached_data

    timeout = aiohttp.ClientTimeout(
        total=25,
        connect=10,
        sock_read=20,
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "JanCryptoSignalBot/2.0",
    }

    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    async def request_global(
        request_headers: dict[str, str],
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=request_headers,
        ) as session:
            async with session.get(
                COINGECKO_GLOBAL_URL
            ) as response:
                text = await response.text()

                if response.status != 200:
                    raise RuntimeError(
                        f"CoinGecko HTTP "
                        f"{response.status}: {text[:300]}"
                    )

                payload = await response.json(
                    content_type=None
                )

                if not isinstance(payload, dict):
                    raise RuntimeError(
                        "CoinGecko returned invalid JSON."
                    )

                return payload

    try:
        response = await request_global(headers)

    except Exception as keyed_error:
        if not COINGECKO_API_KEY:
            raise

        logger.warning(
            "CoinGecko keyed request failed: %s. "
            "Trying keyless access.",
            keyed_error,
        )

        keyless_headers = {
            "Accept": "application/json",
            "User-Agent": "JanCryptoSignalBot/2.0",
        }

        response = await request_global(
            keyless_headers
        )

    data = response.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "CoinGecko response has no data object."
        )

    dominance = data.get(
        "market_cap_percentage"
    )

    if not isinstance(dominance, dict):
        raise RuntimeError(
            "CoinGecko response has no "
            "market-cap percentages."
        )

    btc_dominance = dominance.get("btc")
    eth_dominance = dominance.get("eth")
    usdt_dominance = dominance.get("usdt")

    market_change = data.get(
        "market_cap_change_percentage_24h_usd"
    )

    if btc_dominance is None:
        raise RuntimeError(
            "CoinGecko did not return BTC dominance."
        )

    result = {
        "btc_dominance": float(
            btc_dominance
        ),
        "eth_dominance": float(
            eth_dominance or 0.0
        ),
        "usdt_dominance": float(
            usdt_dominance or 0.0
        ),
        "market_change_24h": float(
            market_change or 0.0
        ),
    }

    GLOBAL_CRYPTO_CACHE["data"] = result
    GLOBAL_CRYPTO_CACHE["timestamp"] = time.time()

    return result
# =========================================================
# CRYPTO FEAR & GREED
# =========================================================

async def fetch_fear_greed() -> dict[str, Any]:
    cached_data = FEAR_GREED_CACHE.get("data")
    cached_timestamp = float(FEAR_GREED_CACHE.get("timestamp", 0.0))

    if (
        isinstance(cached_data, dict)
        and time.time() - cached_timestamp < FEAR_GREED_CACHE_SECONDS
    ):
        return cached_data

    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = {
        "Accept": "application/json",
        "User-Agent": "JanCryptoSignalBot/2.1",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(FEAR_GREED_URL) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"Fear & Greed HTTP {response.status}: {text[:250]}"
                )
            payload = await response.json(content_type=None)

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Fear & Greed provider returned no usable data.")

    current = rows[0]
    previous = rows[1] if len(rows) > 1 else current
    value = float(current.get("value", 50))
    previous_value = float(previous.get("value", value))
    label = str(current.get("value_classification", "Neutral")).upper()

    result = {
        "value": value,
        "label": label,
        "change": value - previous_value,
        "live": True,
    }
    FEAR_GREED_CACHE["data"] = result
    FEAR_GREED_CACHE["timestamp"] = time.time()
    return result


# =========================================================
# VIX DATA
# =========================================================

async def fetch_vix() -> dict[str, float]:
    csv_text = await fetch_text(
        FRED_VIX_CSV_URL
    )

    reader = csv.DictReader(
        io.StringIO(csv_text)
    )

    values: list[float] = []

    for row in reader:
        raw_value = str(
            row.get(
                "VIXCLS",
                "",
            )
        ).strip()

        if not raw_value or raw_value == ".":
            continue

        try:
            values.append(
                float(raw_value)
            )

        except ValueError:
            continue

    if not values:
        raise RuntimeError(
            "FRED returned no usable VIX values."
        )

    current = values[-1]

    previous = (
        values[-2]
        if len(values) >= 2
        else current
    )

    change_percent = (
        (
            current - previous
        )
        / previous
        * 100
        if previous
        else 0.0
    )

    return {
        "value": current,
        "previous_close": previous,
        "change_percent": change_percent,
    }


# =========================================================
# FUTURES POSITIONING
# =========================================================

async def _fetch_binance_derivatives_context(symbol: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = {"Accept": "application/json", "User-Agent": "JanCryptoSignalBot/2.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async def get_payload(path: str, params: dict[str, Any]) -> Any:
            async with session.get(f"{BINANCE_FUTURES_BASE_URL}{path}", params=params) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"Binance Futures HTTP {response.status}: {text[:250]}")
                return await response.json(content_type=None)

        funding_result, oi_result, oi_history_result = await asyncio.gather(
            get_payload("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 2}),
            get_payload("/fapi/v1/openInterest", {"symbol": symbol}),
            get_payload(
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": "5m", "limit": 13},
            ),
        )

    if not isinstance(funding_result, list) or not funding_result:
        raise RuntimeError("Binance Futures returned no funding history.")
    if not isinstance(oi_result, dict):
        raise RuntimeError("Binance Futures returned invalid open interest.")
    if not isinstance(oi_history_result, list) or len(oi_history_result) < 2:
        raise RuntimeError("Binance Futures returned insufficient open-interest history.")

    funding_rate = float(funding_result[-1]["fundingRate"])
    current_oi = float(oi_result["openInterest"])
    history_values = [float(item["sumOpenInterestValue"]) for item in oi_history_result]
    previous_5m = history_values[-2]
    previous_1h = history_values[0]
    current_value = history_values[-1]
    change_5m = (current_value / previous_5m - 1.0) * 100.0 if previous_5m else 0.0
    change_1h = (current_value / previous_1h - 1.0) * 100.0 if previous_1h else 0.0

    absolute_funding = abs(funding_rate)
    if absolute_funding >= 0.001:
        funding_label = "EXTREME LONGS" if funding_rate > 0 else "EXTREME SHORTS"
    elif absolute_funding >= 0.0005:
        funding_label = "CROWDED LONGS" if funding_rate > 0 else "CROWDED SHORTS"
    elif absolute_funding >= 0.0001:
        funding_label = "LONGS PAY" if funding_rate > 0 else "SHORTS PAY"
    else:
        funding_label = "BALANCED"

    return {
        "funding_rate": funding_rate,
        "funding_label": funding_label,
        "open_interest": current_oi,
        "open_interest_value": current_value,
        "open_interest_change_5m": change_5m,
        "open_interest_change_1h": change_1h,
        "live": True,
        "provider": "Binance Futures",
    }


async def _fetch_bybit_derivatives_context(symbol: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = {"Accept": "application/json", "User-Agent": "JanCryptoSignalBot/2.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async def get_payload(path: str, params: dict[str, Any]) -> dict[str, Any]:
            async with session.get(f"{BYBIT_BASE_URL}{path}", params=params) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"Bybit HTTP {response.status}: {text[:250]}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                    raise RuntimeError(f"Bybit returned an error: {str(payload)[:250]}")
                return payload

        ticker_payload, history_payload = await asyncio.gather(
            get_payload("/v5/market/tickers", {"category": "linear", "symbol": symbol}),
            get_payload(
                "/v5/market/open-interest",
                {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 13},
            ),
        )

    tickers = ticker_payload.get("result", {}).get("list", [])
    history = history_payload.get("result", {}).get("list", [])
    if not tickers or len(history) < 2:
        raise RuntimeError("Bybit returned insufficient derivatives data.")
    ticker = tickers[0]
    ordered_history = sorted(history, key=lambda item: int(item["timestamp"]))
    history_values = [float(item["openInterest"]) for item in ordered_history]
    previous_5m = history_values[-2]
    previous_1h = history_values[0]
    current_oi = float(ticker["openInterest"])
    current_value = float(ticker.get("openInterestValue", 0.0))
    change_5m = (current_oi / previous_5m - 1.0) * 100.0 if previous_5m else 0.0
    change_1h = (current_oi / previous_1h - 1.0) * 100.0 if previous_1h else 0.0
    funding_rate = float(ticker["fundingRate"])

    absolute_funding = abs(funding_rate)
    if absolute_funding >= 0.001:
        funding_label = "EXTREME LONGS" if funding_rate > 0 else "EXTREME SHORTS"
    elif absolute_funding >= 0.0005:
        funding_label = "CROWDED LONGS" if funding_rate > 0 else "CROWDED SHORTS"
    elif absolute_funding >= 0.0001:
        funding_label = "LONGS PAY" if funding_rate > 0 else "SHORTS PAY"
    else:
        funding_label = "BALANCED"

    return {
        "funding_rate": funding_rate,
        "funding_label": funding_label,
        "open_interest": current_oi,
        "open_interest_value": current_value,
        "open_interest_change_5m": change_5m,
        "open_interest_change_1h": change_1h,
        "live": True,
        "provider": "Bybit Futures fallback",
    }


async def _fetch_okx_derivatives_context(symbol: str) -> dict[str, Any]:
    if not symbol.endswith("USDT"):
        raise RuntimeError("OKX fallback currently supports USDT pairs only.")
    base_asset = symbol[:-4]
    instrument = f"{base_asset}-USDT-SWAP"
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = {"Accept": "application/json", "User-Agent": "JanCryptoSignalBot/2.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async def get_payload(path: str, params: dict[str, Any]) -> dict[str, Any]:
            async with session.get(f"{OKX_BASE_URL}{path}", params=params) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"OKX HTTP {response.status}: {text[:250]}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict) or str(payload.get("code")) != "0":
                    raise RuntimeError(f"OKX returned an error: {str(payload)[:250]}")
                return payload

        funding_payload, oi_payload, history_payload, liquidation_payload, instrument_payload, book_payload, trades_payload = await asyncio.gather(
            get_payload("/api/v5/public/funding-rate", {"instId": instrument}),
            get_payload(
                "/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": instrument},
            ),
            get_payload(
                "/api/v5/rubik/stat/contracts/open-interest-history",
                {"instId": instrument, "period": "5m"},
            ),
            get_payload(
                "/api/v5/public/liquidation-orders",
                {"instType": "SWAP", "uly": f"{base_asset}-USDT", "state": "filled", "limit": "100"},
            ),
            get_payload(
                "/api/v5/public/instruments",
                {"instType": "SWAP", "instId": instrument},
            ),
            get_payload(
                "/api/v5/market/books",
                {"instId": instrument, "sz": "100"},
            ),
            get_payload(
                "/api/v5/market/trades",
                {"instId": instrument, "limit": "500"},
            ),
        )

    funding_data = funding_payload.get("data", [])
    oi_data = oi_payload.get("data", [])
    history = history_payload.get("data", [])
    instrument_data = instrument_payload.get("data", [])
    book_data = book_payload.get("data", [])
    trades_data = trades_payload.get("data", [])
    if not funding_data or not oi_data or len(history) < 2 or not instrument_data or not book_data:
        raise RuntimeError("OKX returned insufficient derivatives data.")

    funding_rate = float(funding_data[0]["fundingRate"])
    current_oi = float(oi_data[0]["oi"])
    current_value = float(oi_data[0]["oiUsd"])
    previous_5m = float(history[1][3])
    previous_1h = float(history[min(12, len(history) - 1)][3])
    latest_history_value = float(history[0][3])
    change_5m = (
        (latest_history_value / previous_5m - 1.0) * 100.0
        if previous_5m
        else 0.0
    )
    change_1h = (
        (latest_history_value / previous_1h - 1.0) * 100.0
        if previous_1h
        else 0.0
    )

    absolute_funding = abs(funding_rate)
    if absolute_funding >= 0.001:
        funding_label = "EXTREME LONGS" if funding_rate > 0 else "EXTREME SHORTS"
    elif absolute_funding >= 0.0005:
        funding_label = "CROWDED LONGS" if funding_rate > 0 else "CROWDED SHORTS"
    elif absolute_funding >= 0.0001:
        funding_label = "LONGS PAY" if funding_rate > 0 else "SHORTS PAY"
    else:
        funding_label = "BALANCED"

    cutoff_1h = int(time.time() * 1000) - 60 * 60 * 1000
    long_liquidations_1h = 0.0
    short_liquidations_1h = 0.0
    contract_value = float(instrument_data[0].get("ctVal", 1.0))
    for group in liquidation_payload.get("data", []):
        if group.get("instId") != instrument:
            continue
        for detail in group.get("details", []):
            if int(detail.get("ts", detail.get("time", 0))) < cutoff_1h:
                continue
            # OKX liquidation size is expressed in contracts. Linear USDT
            # swaps use ctVal units of the base asset per contract (BTC is
            # commonly 0.01, ETH 0.1, SOL 1), so apply the instrument value.
            notional = (
                float(detail.get("sz", 0.0))
                * contract_value
                * float(detail.get("bkPx", 0.0))
            )
            if detail.get("posSide") == "long":
                long_liquidations_1h += notional
            elif detail.get("posSide") == "short":
                short_liquidations_1h += notional
    total_liquidations = long_liquidations_1h + short_liquidations_1h
    liquidation_intensity = total_liquidations / current_value * 100.0 if current_value else 0.0
    if liquidation_intensity < 0.02:
        liquidation_pressure = "LOW"
    elif long_liquidations_1h >= short_liquidations_1h * 1.5:
        liquidation_pressure = "LONG FLUSH"
    elif short_liquidations_1h >= long_liquidations_1h * 1.5:
        liquidation_pressure = "SHORT SQUEEZE"
    else:
        liquidation_pressure = "TWO-WAY"

    bids = book_data[0].get("bids", [])
    asks = book_data[0].get("asks", [])
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else best_bid or best_ask

    def book_levels(levels: list[list[Any]], lower: float, upper: float) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for level in levels:
            price = float(level[0])
            if lower <= price <= upper:
                notional = price * float(level[1]) * contract_value
                result.append((price, notional))
        return result

    near_bids = book_levels(bids, midpoint * 0.99, midpoint) if midpoint else []
    near_asks = book_levels(asks, midpoint, midpoint * 1.01) if midpoint else []
    bid_depth = sum(value for _, value in near_bids)
    ask_depth = sum(value for _, value in near_asks)
    total_depth = bid_depth + ask_depth
    orderbook_imbalance = (bid_depth - ask_depth) / total_depth * 100.0 if total_depth else 0.0

    def strongest_wall(levels: list[tuple[float, float]]) -> tuple[float, float]:
        if not levels:
            return 0.0, 0.0
        price, value = max(levels, key=lambda item: item[1])
        ordered = sorted(item[1] for item in levels)
        median = ordered[len(ordered) // 2] if ordered else 0.0
        return price, value / median if median else 0.0

    bid_wall_price, bid_wall_strength = strongest_wall(near_bids)
    ask_wall_price, ask_wall_strength = strongest_wall(near_asks)

    taker_buy_value = 0.0
    taker_sell_value = 0.0
    trade_records: list[tuple[float, str]] = []
    for trade in trades_data:
        notional = float(trade.get("px", 0.0)) * float(trade.get("sz", 0.0)) * contract_value
        trade_records.append((notional, str(trade.get("side", "unknown"))))
        if trade.get("side") == "buy":
            taker_buy_value += notional
        elif trade.get("side") == "sell":
            taker_sell_value += notional
    taker_total = taker_buy_value + taker_sell_value
    taker_buy_ratio = taker_buy_value / taker_total * 100.0 if taker_total else 50.0
    taker_flow_imbalance = (taker_buy_value - taker_sell_value) / taker_total * 100.0 if taker_total else 0.0
    ordered_trade_values = sorted(value for value, _ in trade_records)
    average_trade = taker_total / len(trade_records) if trade_records else 0.0
    percentile_99 = ordered_trade_values[int((len(ordered_trade_values) - 1) * 0.99)] if ordered_trade_values else 0.0
    large_trade_threshold = max(percentile_99, average_trade * 5.0)
    large_trades = [item for item in trade_records if item[0] >= large_trade_threshold]
    large_buy_value = sum(value for value, side in large_trades if side == "buy")
    large_sell_value = sum(value for value, side in large_trades if side == "sell")
    large_total = large_buy_value + large_sell_value
    large_flow_share = large_total / taker_total * 100.0 if taker_total else 0.0
    large_flow_imbalance = (large_buy_value - large_sell_value) / large_total * 100.0 if large_total else 0.0
    largest_trade_value, largest_trade_side = max(trade_records, default=(0.0, "unknown"), key=lambda item: item[0])
    largest_trade_multiple = largest_trade_value / average_trade if average_trade else 0.0

    return {
        "funding_rate": funding_rate,
        "funding_label": funding_label,
        "open_interest": current_oi,
        "open_interest_value": current_value,
        "open_interest_change_5m": change_5m,
        "open_interest_change_1h": change_1h,
        "live": True,
        "provider": "OKX Futures fallback",
        "long_liquidations_1h": long_liquidations_1h,
        "short_liquidations_1h": short_liquidations_1h,
        "liquidation_pressure": liquidation_pressure,
        "liquidation_intensity": liquidation_intensity,
        "contract_value": contract_value,
        "orderbook_imbalance": orderbook_imbalance,
        "bid_wall_price": bid_wall_price,
        "bid_wall_strength": bid_wall_strength,
        "ask_wall_price": ask_wall_price,
        "ask_wall_strength": ask_wall_strength,
        "taker_buy_ratio": taker_buy_ratio,
        "taker_flow_imbalance": taker_flow_imbalance,
        "large_trade_threshold": large_trade_threshold,
        "large_trade_count": len(large_trades),
        "large_flow_share": large_flow_share,
        "large_flow_imbalance": large_flow_imbalance,
        "largest_trade_value": largest_trade_value,
        "largest_trade_side": largest_trade_side.upper(),
        "largest_trade_multiple": largest_trade_multiple,
    }


async def _fetch_derivatives_context_uncached(symbol: str) -> dict[str, Any]:
    try:
        return await _fetch_okx_derivatives_context(symbol)
    except Exception as okx_error:
        logger.warning("OKX Futures data unavailable for %s: %s", symbol, okx_error)
        try:
            result = await _fetch_binance_derivatives_context(symbol)
            result["fallback_reason"] = type(okx_error).__name__
            return result
        except Exception as binance_error:
            logger.warning("Binance Futures data unavailable for %s: %s", symbol, binance_error)
            result = await _fetch_bybit_derivatives_context(symbol)
            result["fallback_reason"] = (
                f"{type(okx_error).__name__}, {type(binance_error).__name__}"
            )
            return result


async def fetch_derivatives_context(symbol: str) -> dict[str, Any]:
    normalized = symbol.upper()
    cached = DERIVATIVES_CACHE.get(normalized, {})
    if (
        cached.get("data") is not None
        and time.time() - float(cached.get("timestamp", 0.0)) < DERIVATIVES_CACHE_SECONDS
    ):
        return dict(cached["data"])

    result = await _fetch_derivatives_context_uncached(normalized)
    DERIVATIVES_CACHE[normalized] = {
        "data": dict(result),
        "timestamp": time.time(),
    }
    return result


# =========================================================
# COMPLETE RAW CONTEXT
# =========================================================

async def get_market_context_data(
    selected_symbol: str,
) -> dict[str, Any]:
    btc_task = asyncio.create_task(
        fetch_snapshot(
            "BTCUSDT"
        )
    )

    eth_task = asyncio.create_task(
        fetch_snapshot(
            "ETHUSDT"
        )
    )

    global_task = asyncio.create_task(
        fetch_global_crypto()
    )

    vix_task = asyncio.create_task(
        fetch_vix()
    )

    fear_greed_task = asyncio.create_task(
        fetch_fear_greed()
    )

    derivatives_task = asyncio.create_task(
        fetch_derivatives_context(selected_symbol)
    )
    coinbase_premium_task = asyncio.create_task(fetch_coinbase_premiums())
    news_task = asyncio.create_task(fetch_news_intelligence())

    results = await asyncio.gather(
        btc_task,
        eth_task,
        global_task,
        vix_task,
        fear_greed_task,
        derivatives_task,
        coinbase_premium_task,
        news_task,
        return_exceptions=True,
    )

    names = [
        "btc",
        "eth",
        "global_crypto",
        "vix",
        "fear_greed",
        "derivatives",
        "coinbase_premium",
        "news",
    ]

    context: dict[str, Any] = {
        "selected_symbol": selected_symbol,
        "provider_errors": {},
    }

    for name, result in zip(
        names,
        results,
    ):
        if isinstance(
            result,
            Exception,
        ):
            context[
                "provider_errors"
            ][name] = (
                f"{type(result).__name__}: "
                f"{result}"
            )

        else:
            context[name] = result

    return context


# =========================================================
# SIGNAL HELPERS
# =========================================================

def bullish(
    signal: MarketSignal,
) -> bool:
    return "LONG" in signal.direction


def bearish(
    signal: MarketSignal,
) -> bool:
    return "SHORT" in signal.direction


def correlation_label(
    value: float,
) -> str:
    absolute_value = abs(
        value
    )

    if absolute_value >= 0.70:
        return "HIGH"

    if absolute_value >= 0.40:
        return "MEDIUM"

    return "LOW"


def calculate_correlation(
    selected_signal: MarketSignal,
    btc_signal: MarketSignal,
) -> float:
    if (
        selected_signal.symbol.upper()
        == btc_signal.symbol.upper()
    ):
        return 1.0

    correlations: list[float] = []

    weights = {
        "5m": 0.10,
        "15m": 0.20,
        "1h": 0.30,
        "4h": 0.25,
        "8h": 0.10,
        "1d": 0.05,
    }

    weighted_total = 0.0
    total_weight = 0.0

    for interval, weight in weights.items():
        selected = selected_signal.analyses.get(
            interval
        )

        bitcoin = btc_signal.analyses.get(
            interval
        )

        if (
            selected is None
            or bitcoin is None
        ):
            continue

        selected_frame_score = (
            selected.score / 100
        )

        btc_frame_score = (
            bitcoin.score / 100
        )

        correlation_proxy = (
            1.0
            - abs(
                selected_frame_score
                - btc_frame_score
            )
            / 2.0
        )

        if (
            selected_frame_score
            * btc_frame_score
            < 0
        ):
            correlation_proxy *= -1

        correlations.append(
            correlation_proxy
        )

        weighted_total += (
            correlation_proxy
            * weight
        )

        total_weight += weight

    if total_weight <= 0:
        return 0.0

    return float(
        clamp(
            weighted_total / total_weight,
            -1.0,
            1.0,
        )
    )


# =========================================================
# BTC INFLUENCE
# =========================================================

def evaluate_btc(
    selected_signal: MarketSignal,
    btc_signal: MarketSignal,
    correlation: float,
) -> tuple[
    float,
    list[str],
    list[str],
]:
    adjustment = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    positive_correlation = (
        correlation >= 0.35
    )

    strong_correlation = (
        correlation >= 0.65
    )

    if bullish(
        selected_signal
    ):
        if (
            positive_correlation
            and bullish(btc_signal)
        ):
            adjustment += (
                8.0
                if strong_correlation
                else 5.0
            )

            reasons.append(
                "BTC direction supports the long setup."
            )

        elif (
            positive_correlation
            and bearish(btc_signal)
        ):
            adjustment -= (
                13.0
                if strong_correlation
                else 8.0
            )

            warnings.append(
                "BTC weakness conflicts with the long setup."
            )

    elif bearish(
        selected_signal
    ):
        if (
            positive_correlation
            and bearish(btc_signal)
        ):
            adjustment -= (
                8.0
                if strong_correlation
                else 5.0
            )

            reasons.append(
                "BTC weakness supports the short setup."
            )

        elif (
            positive_correlation
            and bullish(btc_signal)
        ):
            adjustment += (
                13.0
                if strong_correlation
                else 8.0
            )

            warnings.append(
                "BTC strength conflicts with the short setup."
            )

    if abs(correlation) < 0.25:
        warnings.append(
            "Recent BTC alignment is weak."
        )

    return (
        adjustment,
        reasons,
        warnings,
    )


# =========================================================
# ETH INFLUENCE
# =========================================================

def evaluate_eth(
    selected_signal: MarketSignal,
    eth_signal: MarketSignal,
) -> tuple[
    float,
    list[str],
    list[str],
]:
    symbol = selected_signal.symbol.upper()

    if symbol in {
        "BTCUSDT",
        "ETHUSDT",
    }:
        return 0.0, [], []

    adjustment = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    if (
        bullish(selected_signal)
        and bullish(eth_signal)
    ):
        adjustment += 4.0

        reasons.append(
            "ETH strength supports the altcoin market."
        )

    elif (
        bullish(selected_signal)
        and bearish(eth_signal)
    ):
        adjustment -= 5.0

        warnings.append(
            "ETH weakness conflicts with the altcoin long."
        )

    if (
        bearish(selected_signal)
        and bearish(eth_signal)
    ):
        adjustment -= 4.0

        reasons.append(
            "ETH weakness supports the altcoin short."
        )

    elif (
        bearish(selected_signal)
        and bullish(eth_signal)
    ):
        adjustment += 5.0

        warnings.append(
            "ETH strength conflicts with the altcoin short."
        )

    return adjustment, reasons, warnings


# =========================================================
# BTC DOMINANCE
# =========================================================

def evaluate_dominance(
    selected_signal: MarketSignal,
    btc_dominance: float,
) -> tuple[
    float,
    str,
    list[str],
    list[str],
]:
    symbol = selected_signal.symbol.upper()

    if symbol == "BTCUSDT":
        return (
            0.0,
            "BTC MARKET",
            [],
            [],
        )

    adjustment = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    if btc_dominance >= 58:
        effect = "BTC HEAVY"

        if bullish(
            selected_signal
        ):
            adjustment -= 6.0

            warnings.append(
                "High BTC dominance can suppress altcoin longs."
            )

        elif bearish(
            selected_signal
        ):
            adjustment -= 3.0

            reasons.append(
                "High BTC dominance adds pressure to weaker altcoins."
            )

    elif btc_dominance <= 45:
        effect = "ALTCOIN FRIENDLY"

        if bullish(
            selected_signal
        ):
            adjustment += 5.0

            reasons.append(
                "Lower BTC dominance is friendlier to altcoin longs."
            )

    else:
        effect = "BALANCED"

    return (
        adjustment,
        effect,
        reasons,
        warnings,
    )


# =========================================================
# VIX REGIME
# =========================================================

def evaluate_vix(
    selected_signal: MarketSignal,
    value: float,
    change_percent: float,
) -> tuple[
    float,
    str,
    list[str],
    list[str],
]:
    adjustment = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    if value < 16:
        regime = "LOW FEAR"

        if bullish(
            selected_signal
        ):
            adjustment += 5.0

            reasons.append(
                "Low VIX supports a risk-on environment."
            )

    elif value < 22:
        regime = "NORMAL"

    elif value < 30:
        regime = "ELEVATED FEAR"

        if bullish(
            selected_signal
        ):
            adjustment -= 6.0

            warnings.append(
                "Elevated VIX increases reversal risk."
            )

        elif bearish(
            selected_signal
        ):
            adjustment -= 3.0

    elif value < 40:
        regime = "HIGH FEAR"

        if bullish(
            selected_signal
        ):
            adjustment -= 11.0

            warnings.append(
                "High VIX strongly conflicts with aggressive longs."
            )

        elif bearish(
            selected_signal
        ):
            adjustment -= 6.0

            reasons.append(
                "High VIX supports a defensive market bias."
            )

    else:
        regime = "EXTREME FEAR"

        if bullish(
            selected_signal
        ):
            adjustment -= 16.0

            warnings.append(
                "Extreme VIX makes new long entries unusually risky."
            )

        elif bearish(
            selected_signal
        ):
            adjustment -= 8.0

    if change_percent >= 10:
        if bullish(
            selected_signal
        ):
            adjustment -= 4.0

            warnings.append(
                "VIX increased sharply from its previous close."
            )

    return (
        adjustment,
        regime,
        reasons,
        warnings,
    )


# =========================================================
# TOTAL CRYPTO MARKET
# =========================================================

def evaluate_crypto_market(
    selected_signal: MarketSignal,
    market_change: float,
) -> tuple[
    float,
    list[str],
    list[str],
]:
    adjustment = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    if market_change >= 3:
        if bullish(
            selected_signal
        ):
            adjustment += 5.0

            reasons.append(
                "Total crypto market cap is rising strongly."
            )

        elif bearish(
            selected_signal
        ):
            adjustment += 4.0

            warnings.append(
                "Broad crypto strength conflicts with the short."
            )

    elif market_change >= 1:
        if bullish(
            selected_signal
        ):
            adjustment += 2.0

    elif market_change <= -3:
        if bullish(
            selected_signal
        ):
            adjustment -= 7.0

            warnings.append(
                "Total crypto market cap is falling sharply."
            )

        elif bearish(
            selected_signal
        ):
            adjustment -= 5.0

            reasons.append(
                "Broad crypto weakness supports the short."
            )

    elif market_change <= -1:
        if bullish(
            selected_signal
        ):
            adjustment -= 3.0

        elif bearish(
            selected_signal
        ):
            adjustment -= 2.0

    return adjustment, reasons, warnings


# =========================================================
# COMPLETE CONTEXT ENGINE
# =========================================================
def calculate_macro_bias(
    btc_score: float,
    eth_score: float,
    btc_correlation: float,
    btc_dominance: float,
    eth_dominance: float,
    market_change_24h: float,
    vix_value: float,
    fear_greed_value: float,
) -> tuple[float, str, list[str]]:
    macro_score = 0.0
    reasons: list[str] = []

    correlation_weight = max(
        0.25,
        min(abs(btc_correlation), 1.0),
    )

    btc_effect = (
        max(-10.0, min(10.0, btc_score / 6.0))
        * correlation_weight
    )
    macro_score += btc_effect

    if btc_effect >= 2.0:
        reasons.append(
            "BTC trend supports risk appetite."
        )
    elif btc_effect <= -2.0:
        reasons.append(
            "BTC trend creates bearish market pressure."
        )

    eth_effect = max(
        -5.0,
        min(5.0, eth_score / 10.0),
    )
    macro_score += eth_effect

    if eth_effect >= 1.5:
        reasons.append(
            "ETH strength supports the altcoin market."
        )
    elif eth_effect >= 0.5:
        reasons.append(
            "ETH is mildly supportive of altcoins."
        )
    elif eth_effect <= -1.5:
        reasons.append(
            "ETH weakness weighs on altcoins."
        )
    elif eth_effect <= -0.5:
        reasons.append(
            "ETH shows mild weakness for altcoins."
        )

    market_effect = max(
        -8.0,
        min(8.0, market_change_24h * 2.5),
    )
    macro_score += market_effect

    if market_change_24h >= 1.0:
        reasons.append(
            "The total crypto market is expanding."
        )
    elif market_change_24h <= -1.0:
        reasons.append(
            "The total crypto market is contracting."
        )

    if btc_dominance >= 60.0:
        macro_score -= 3.0
        reasons.append(
            "High BTC dominance may restrict altcoin strength."
        )
    elif btc_dominance <= 52.0:
        macro_score += 2.0
        reasons.append(
            "Lower BTC dominance may favor altcoins."
        )

    if eth_dominance >= 18.0:
        macro_score += 2.0
        reasons.append("Strong ETH dominance supports broad altcoin participation.")
    elif 0.0 < eth_dominance <= 14.0:
        macro_score -= 2.0
        reasons.append("Weak ETH dominance signals limited broad altcoin participation.")

    if vix_value >= 30.0:
        macro_score -= 6.0
        reasons.append(
            "High VIX signals strong risk aversion."
        )
    elif vix_value >= 22.0:
        macro_score -= 3.0
        reasons.append(
            "Elevated VIX adds risk-off pressure."
        )
    elif 0.0 < vix_value <= 17.0:
        macro_score += 2.0
        reasons.append(
            "Low VIX supports risk appetite."
        )

    if fear_greed_value <= 24.0:
        macro_score -= 4.0
        reasons.append("Crypto sentiment is in extreme fear.")
    elif fear_greed_value <= 44.0:
        macro_score -= 2.0
        reasons.append("Crypto sentiment remains fearful.")
    elif fear_greed_value >= 76.0:
        macro_score += 2.0
        reasons.append(
            "Crypto sentiment is extremely greedy; momentum is strong but reversal risk is elevated."
        )
    elif fear_greed_value >= 56.0:
        macro_score += 2.0
        reasons.append("Crypto sentiment supports risk appetite.")

    macro_score = max(
        -25.0,
        min(25.0, macro_score),
    )

    if macro_score >= 8.0:
        macro_bias = "BULLISH"
    elif macro_score <= -8.0:
        macro_bias = "BEARISH"
    else:
        macro_bias = "NEUTRAL"

    return (
        round(macro_score, 1),
        macro_bias,
        reasons,
    )
def build_market_context(
    selected_signal: MarketSignal,
    context_data: dict[str, Any],
) -> MarketContext:
    reasons: list[str] = []
    warnings: list[str] = []

    btc_score = 0.0
    btc_direction = "UNKNOWN"

    eth_score = 0.0
    eth_direction = "UNKNOWN"
    eth_btc_relative_strength = 0.0
    eth_dominance = 0.0

    correlation = 0.0

    btc_dominance = 0.0
    dominance_effect = "UNKNOWN"

    market_change = 0.0

    vix_value = 0.0
    vix_change = 0.0
    vix_regime = "UNKNOWN"

    fear_greed_value = 50.0
    fear_greed_label = "NEUTRAL"
    fear_greed_change = 0.0
    fear_greed_live = False
    btc_coinbase_premium = 0.0
    eth_coinbase_premium = 0.0
    coinbase_premium_live = False

    funding_rate = 0.0
    funding_label = "UNAVAILABLE"
    open_interest_value = 0.0
    open_interest_change_5m = 0.0
    open_interest_change_1h = 0.0
    derivatives_live = False
    derivatives_adjustment = 0.0
    derivatives_provider = "UNAVAILABLE"
    long_liquidations_1h = 0.0
    short_liquidations_1h = 0.0
    liquidation_pressure = "UNAVAILABLE"
    orderbook_imbalance = 0.0
    bid_wall_price = 0.0
    bid_wall_strength = 0.0
    ask_wall_price = 0.0
    ask_wall_strength = 0.0
    taker_buy_ratio = 50.0
    taker_flow_imbalance = 0.0
    large_trade_threshold = 0.0
    large_trade_count = 0
    large_flow_share = 0.0
    large_flow_imbalance = 0.0
    largest_trade_value = 0.0
    largest_trade_side = "UNKNOWN"
    largest_trade_multiple = 0.0
    news_score = 0.0
    news_label = "NEUTRAL"
    news_live = False

    total_adjustment = 0.0

    btc_snapshot = context_data.get(
        "btc"
    )

    eth_snapshot = context_data.get(
        "eth"
    )

    btc_signal: MarketSignal | None = None
    eth_signal: MarketSignal | None = None

    if btc_snapshot:
        try:
            btc_signal = build_market_signal(
                symbol="BTCUSDT",
                candle_data=btc_snapshot.get(
                    "candles",
                    {},
                ),
                errors=btc_snapshot.get(
                    "errors",
                    {},
                ),
            )

            btc_score = btc_signal.score
            btc_direction = btc_signal.direction

        except Exception as error:
            logger.exception(
                "BTC context calculation failed."
            )

            warnings.append(
                f"BTC context failed: "
                f"{type(error).__name__}"
            )

    if eth_snapshot:
        try:
            eth_signal = build_market_signal(
                symbol="ETHUSDT",
                candle_data=eth_snapshot.get(
                    "candles",
                    {},
                ),
                errors=eth_snapshot.get(
                    "errors",
                    {},
                ),
            )

            eth_score = eth_signal.score
            eth_direction = eth_signal.direction

        except Exception as error:
            logger.exception(
                "ETH context calculation failed."
            )

            warnings.append(
                f"ETH context failed: "
                f"{type(error).__name__}"
            )

    if btc_signal is not None and eth_signal is not None:
        btc_1h = btc_signal.analyses.get("1h")
        eth_1h = eth_signal.analyses.get("1h")
        if btc_1h is not None and eth_1h is not None:
            eth_btc_relative_strength = float(eth_1h.roc - btc_1h.roc)
            if selected_signal.symbol.upper() != "BTCUSDT":
                if eth_btc_relative_strength >= 0.50:
                    total_adjustment += 1.5
                    reasons.append("ETH is outperforming BTC over the 12-hour momentum window.")
                elif eth_btc_relative_strength <= -0.50:
                    total_adjustment -= 1.5
                    warnings.append("ETH is underperforming BTC over the 12-hour momentum window.")

    if btc_signal is not None:
        correlation = calculate_correlation(
            selected_signal,
            btc_signal,
        )

        if selected_signal.symbol.upper() != "BTCUSDT":
            (
                adjustment,
                new_reasons,
                new_warnings,
            ) = evaluate_btc(
                selected_signal,
                btc_signal,
                correlation,
            )

            total_adjustment += adjustment
            reasons.extend(new_reasons)
            warnings.extend(new_warnings)

    if eth_signal is not None:
        (
            adjustment,
            new_reasons,
            new_warnings,
        ) = evaluate_eth(
            selected_signal,
            eth_signal,
        )

        total_adjustment += adjustment
        reasons.extend(new_reasons)
        warnings.extend(new_warnings)

    global_crypto = context_data.get(
        "global_crypto",
        {},
    )

    if global_crypto:
        btc_dominance = float(
            global_crypto.get(
                "btc_dominance",
                0.0,
            )
        )
        eth_dominance = float(global_crypto.get("eth_dominance", 0.0))

        market_change = float(
            global_crypto.get(
                "market_change_24h",
                0.0,
            )
        )

        (
            adjustment,
            dominance_effect,
            new_reasons,
            new_warnings,
        ) = evaluate_dominance(
            selected_signal,
            btc_dominance,
        )

        total_adjustment += adjustment
        reasons.extend(new_reasons)
        warnings.extend(new_warnings)

        (
            adjustment,
            new_reasons,
            new_warnings,
        ) = evaluate_crypto_market(
            selected_signal,
            market_change,
        )

        total_adjustment += adjustment
        reasons.extend(new_reasons)
        warnings.extend(new_warnings)

    vix = context_data.get(
        "vix",
        {},
    )

    if vix:
        vix_value = float(
            vix.get(
                "value",
                0.0,
            )
        )

        vix_change = float(
            vix.get(
                "change_percent",
                0.0,
            )
        )

        (
            adjustment,
            vix_regime,
            new_reasons,
            new_warnings,
        ) = evaluate_vix(
            selected_signal,
            vix_value,
            vix_change,
        )

        total_adjustment += adjustment
        reasons.extend(new_reasons)
        warnings.extend(new_warnings)

    fear_greed = context_data.get(
        "fear_greed",
        {},
    )

    if fear_greed:
        fear_greed_value = float(fear_greed.get("value", 50.0))
        fear_greed_label = str(fear_greed.get("label", "NEUTRAL"))
        fear_greed_change = float(fear_greed.get("change", 0.0))
        fear_greed_live = bool(fear_greed.get("live", True))

    coinbase_premium = context_data.get("coinbase_premium", {})
    if coinbase_premium:
        btc_coinbase_premium = float(coinbase_premium.get("btc", 0.0))
        eth_coinbase_premium = float(coinbase_premium.get("eth", 0.0))
        coinbase_premium_live = bool(coinbase_premium.get("live", True))
        if btc_coinbase_premium >= 0.10:
            total_adjustment += 1.5
            reasons.append("Positive Coinbase BTC premium signals stronger US spot demand.")
        elif btc_coinbase_premium <= -0.10:
            total_adjustment -= 1.5
            warnings.append("Negative Coinbase BTC premium signals weaker US spot demand.")
        if eth_coinbase_premium >= 0.10:
            total_adjustment += 0.5
        elif eth_coinbase_premium <= -0.10:
            total_adjustment -= 0.5

    derivatives = context_data.get("derivatives", {})
    if derivatives:
        funding_rate = float(derivatives.get("funding_rate", 0.0))
        funding_label = str(derivatives.get("funding_label", "BALANCED"))
        open_interest_value = float(derivatives.get("open_interest_value", 0.0))
        open_interest_change_5m = float(derivatives.get("open_interest_change_5m", 0.0))
        open_interest_change_1h = float(derivatives.get("open_interest_change_1h", 0.0))
        derivatives_live = bool(derivatives.get("live", True))
        derivatives_provider = str(derivatives.get("provider", "UNKNOWN"))
        long_liquidations_1h = float(derivatives.get("long_liquidations_1h", 0.0))
        short_liquidations_1h = float(derivatives.get("short_liquidations_1h", 0.0))
        liquidation_pressure = str(derivatives.get("liquidation_pressure", "UNAVAILABLE"))
        orderbook_imbalance = float(derivatives.get("orderbook_imbalance", 0.0))
        bid_wall_price = float(derivatives.get("bid_wall_price", 0.0))
        bid_wall_strength = float(derivatives.get("bid_wall_strength", 0.0))
        ask_wall_price = float(derivatives.get("ask_wall_price", 0.0))
        ask_wall_strength = float(derivatives.get("ask_wall_strength", 0.0))
        taker_buy_ratio = float(derivatives.get("taker_buy_ratio", 50.0))
        taker_flow_imbalance = float(derivatives.get("taker_flow_imbalance", 0.0))
        large_trade_threshold = float(derivatives.get("large_trade_threshold", 0.0))
        large_trade_count = int(derivatives.get("large_trade_count", 0))
        large_flow_share = float(derivatives.get("large_flow_share", 0.0))
        large_flow_imbalance = float(derivatives.get("large_flow_imbalance", 0.0))
        largest_trade_value = float(derivatives.get("largest_trade_value", 0.0))
        largest_trade_side = str(derivatives.get("largest_trade_side", "UNKNOWN"))
        largest_trade_multiple = float(derivatives.get("largest_trade_multiple", 0.0))

        if funding_rate >= 0.0005:
            derivatives_adjustment -= 3.0
            warnings.append("Positive funding shows crowded leveraged longs.")
        elif funding_rate <= -0.0005:
            derivatives_adjustment += 3.0
            warnings.append("Negative funding shows crowded leveraged shorts.")

        if open_interest_change_1h >= 5.0:
            direction_effect = 3.0 if selected_signal.score > 0 else -3.0
            derivatives_adjustment += direction_effect
            reasons.append("Open interest is expanding and reinforces the technical direction.")
        elif open_interest_change_1h <= -5.0:
            direction_effect = -2.0 if selected_signal.score > 0 else 2.0
            derivatives_adjustment += direction_effect
            warnings.append("Open interest is contracting; deleveraging weakens trend conviction.")

        if orderbook_imbalance >= 15.0 and taker_flow_imbalance >= 15.0:
            derivatives_adjustment += 2.0
            reasons.append("Bid depth and recent aggressive buying confirm bullish order flow.")
        elif orderbook_imbalance <= -15.0 and taker_flow_imbalance <= -15.0:
            derivatives_adjustment -= 2.0
            reasons.append("Ask depth and recent aggressive selling confirm bearish order flow.")

        derivatives_adjustment = clamp(derivatives_adjustment, -6.0, 6.0)
        total_adjustment += derivatives_adjustment

    provider_errors = context_data.get(
        "provider_errors",
        {},
    )

    news = context_data.get("news", {})
    if news:
        news_score = float(news.get("score", 0.0))
        news_label = str(news.get("label", "NEUTRAL"))
        news_live = bool(news.get("live", False))
        if news_score:
            news_adjustment = clamp(news_score * 0.5, -3.0, 3.0)
            total_adjustment += news_adjustment
            reasons.append(f"News-intelligence bias is {news_label.lower()} ({news_score:+.0f}/6).")

    if provider_errors:
        for provider_name, provider_error in provider_errors.items():
            warnings.append(
                f"{provider_name} failed: {provider_error}"
            )

    (
        macro_score,
        macro_bias,
        macro_reasons,
    ) = calculate_macro_bias(
        btc_score=btc_score,
        eth_score=eth_score,
        btc_correlation=correlation,
        btc_dominance=btc_dominance,
        eth_dominance=eth_dominance,
        market_change_24h=market_change,
        vix_value=vix_value,
        fear_greed_value=fear_greed_value,
    )

    macro_adjustment = clamp(
        macro_score * 0.25,
        -8.0,
        8.0,
    )

    total_adjustment = clamp(
        total_adjustment + macro_adjustment,
        -30.0,
        30.0,
    )

    adjusted_score = clamp(
        selected_signal.score + total_adjustment,
        -100.0,
        100.0,
    )

    unique_reasons = list(
        dict.fromkeys(reasons)
    )
    unique_warnings = list(
        dict.fromkeys(warnings)
    )

    return MarketContext(
        available=True,
        original_score=selected_signal.score,
        adjusted_score=adjusted_score,
        score_adjustment=total_adjustment,
        macro_score=macro_score,
        macro_bias=macro_bias,
        macro_reasons=macro_reasons,
        btc_score=btc_score,
        btc_direction=btc_direction,
        eth_score=eth_score,
        eth_direction=eth_direction,
        eth_btc_relative_strength=eth_btc_relative_strength,
        eth_dominance=eth_dominance,
        btc_correlation=correlation,
        correlation_strength=correlation_label(
            correlation
        ),
        btc_dominance=btc_dominance,
        btc_dominance_effect=dominance_effect,
        crypto_market_change_24h=market_change,
        vix_value=vix_value,
        vix_change_percent=vix_change,
        vix_regime=vix_regime,
        fear_greed_value=fear_greed_value,
        fear_greed_label=fear_greed_label,
        fear_greed_change=fear_greed_change,
        fear_greed_live=fear_greed_live,
        btc_coinbase_premium=btc_coinbase_premium,
        eth_coinbase_premium=eth_coinbase_premium,
        coinbase_premium_live=coinbase_premium_live,
        funding_rate=funding_rate,
        funding_label=funding_label,
        open_interest_value=open_interest_value,
        open_interest_change_5m=open_interest_change_5m,
        open_interest_change_1h=open_interest_change_1h,
        derivatives_live=derivatives_live,
        derivatives_adjustment=derivatives_adjustment,
        derivatives_provider=derivatives_provider,
        long_liquidations_1h=long_liquidations_1h,
        short_liquidations_1h=short_liquidations_1h,
        liquidation_pressure=liquidation_pressure,
        orderbook_imbalance=orderbook_imbalance,
        bid_wall_price=bid_wall_price,
        bid_wall_strength=bid_wall_strength,
        ask_wall_price=ask_wall_price,
        ask_wall_strength=ask_wall_strength,
        taker_buy_ratio=taker_buy_ratio,
        taker_flow_imbalance=taker_flow_imbalance,
        large_trade_threshold=large_trade_threshold,
        large_trade_count=large_trade_count,
        large_flow_share=large_flow_share,
        large_flow_imbalance=large_flow_imbalance,
        largest_trade_value=largest_trade_value,
        largest_trade_side=largest_trade_side,
        largest_trade_multiple=largest_trade_multiple,
        news_score=news_score,
        news_label=news_label,
        news_live=news_live,
        reasons=unique_reasons[:10],
        warnings=unique_warnings[:10],
    )
