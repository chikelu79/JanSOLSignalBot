import os
import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

import market
from indicators import clamp
from strategy import MarketSignal, build_market_signal


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
COINGECKO_GLOBAL_URL = (
    "https://api.coingecko.com/api/v3/global"
)

FRED_VIX_CSV_URL = (
    "https://fred.stlouisfed.org/graph/"
    "fredgraph.csv?id=VIXCLS"
)


# =========================================================
# RESULT MODEL
# =========================================================

@dataclass
class MarketContext:
    available: bool

    original_score: float
    adjusted_score: float
    score_adjustment: float

    btc_score: float
    btc_direction: str

    eth_score: float
    eth_direction: str

    btc_correlation: float
    correlation_strength: str

    btc_dominance: float
    btc_dominance_effect: str

    crypto_market_change_24h: float

    vix_value: float
    vix_change_percent: float
    vix_regime: str

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

    results = await asyncio.gather(
        btc_task,
        eth_task,
        global_task,
        vix_task,
        return_exceptions=True,
    )

    names = [
        "btc",
        "eth",
        "global_crypto",
        "vix",
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

    correlation = 0.0

    btc_dominance = 0.0
    dominance_effect = "UNKNOWN"

    market_change = 0.0

    vix_value = 0.0
    vix_change = 0.0
    vix_regime = "UNKNOWN"

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

    if btc_signal is not None:
        correlation = calculate_correlation(
        selected_signal,
        btc_signal,
    )

    if (
        selected_signal.symbol.upper()
        != "BTCUSDT"
    ):
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

        provider_errors = context_data.get(
            "provider_errors",
        {},
    )

        if provider_errors:
            for provider_name, provider_error in provider_errors.items():
                warnings.append(
                    f"{provider_name} failed: {provider_error}"
                )

    total_adjustment = clamp(
        total_adjustment,
        -30.0,
        30.0,
    )

    adjusted_score = clamp(
        selected_signal.score
        + total_adjustment,
        -100.0,
        100.0,
    )

    unique_reasons = list(
        dict.fromkeys(
            reasons
        )
    )

    unique_warnings = list(
        dict.fromkeys(
            warnings
        )
    )

    return MarketContext(
        available=True,
        original_score=selected_signal.score,
        adjusted_score=adjusted_score,
        score_adjustment=total_adjustment,
        btc_score=btc_score,
        btc_direction=btc_direction,
        eth_score=eth_score,
        eth_direction=eth_direction,
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
        reasons=unique_reasons[:10],
        warnings=unique_warnings[:10],
    )
