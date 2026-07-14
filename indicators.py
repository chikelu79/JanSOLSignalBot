import math
from typing import Any

import numpy as np
import pandas as pd


# =========================================================
# BASIC HELPERS
# =========================================================

def valid_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def safe_float(
    value: Any,
    fallback: float = 0.0,
) -> float:
    if not valid_number(value):
        return fallback

    return float(value)


def clamp(
    value: float,
    minimum: float,
    maximum: float,
) -> float:
    return max(
        minimum,
        min(maximum, value),
    )


# =========================================================
# MOVING AVERAGES
# =========================================================

def calculate_ema(
    values: pd.Series,
    period: int,
) -> pd.Series:
    return values.ewm(
        span=period,
        adjust=False,
        min_periods=period,
    ).mean()


def calculate_sma(
    values: pd.Series,
    period: int,
) -> pd.Series:
    return values.rolling(
        period
    ).mean()


# =========================================================
# RSI
# =========================================================

def calculate_rsi(
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    change = close.diff()

    gains = change.clip(
        lower=0
    )

    losses = -change.clip(
        upper=0
    )

    average_gain = gains.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    average_loss = losses.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    relative_strength = (
        average_gain
        / average_loss.replace(
            0,
            np.nan,
        )
    )

    return 100 - (
        100
        / (1 + relative_strength)
    )


# =========================================================
# MACD
# =========================================================

def calculate_macd(
    close: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
]:
    fast_ema = calculate_ema(
        close,
        fast_period,
    )

    slow_ema = calculate_ema(
        close,
        slow_period,
    )

    macd_line = (
        fast_ema - slow_ema
    )

    signal_line = macd_line.ewm(
        span=signal_period,
        adjust=False,
    ).mean()

    histogram = (
        macd_line - signal_line
    )

    return (
        macd_line,
        signal_line,
        histogram,
    )


# =========================================================
# STOCHASTIC RSI
# =========================================================

def calculate_stoch_rsi(
    rsi: pd.Series,
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[
    pd.Series,
    pd.Series,
]:
    lowest_rsi = rsi.rolling(
        period
    ).min()

    highest_rsi = rsi.rolling(
        period
    ).max()

    denominator = (
        highest_rsi
        - lowest_rsi
    ).replace(
        0,
        np.nan,
    )

    raw_stoch_rsi = (
        100
        * (
            rsi
            - lowest_rsi
        )
        / denominator
    )

    stoch_k = raw_stoch_rsi.rolling(
        smooth_k
    ).mean()

    stoch_d = stoch_k.rolling(
        smooth_d
    ).mean()

    return (
        stoch_k,
        stoch_d,
    )


# =========================================================
# STOCHASTIC OSCILLATOR
# =========================================================

def calculate_stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[
    pd.Series,
    pd.Series,
]:
    highest_high = high.rolling(
        period
    ).max()

    lowest_low = low.rolling(
        period
    ).min()

    denominator = (
        highest_high
        - lowest_low
    ).replace(
        0,
        np.nan,
    )

    raw_k = (
        100
        * (
            close
            - lowest_low
        )
        / denominator
    )

    stochastic_k = raw_k.rolling(
        smooth_k
    ).mean()

    stochastic_d = (
        stochastic_k.rolling(
            smooth_d
        ).mean()
    )

    return (
        stochastic_k,
        stochastic_d,
    )


# =========================================================
# WILLIAMS %R
# =========================================================

def calculate_williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    highest_high = high.rolling(
        period
    ).max()

    lowest_low = low.rolling(
        period
    ).min()

    denominator = (
        highest_high
        - lowest_low
    ).replace(
        0,
        np.nan,
    )

    return (
        -100
        * (
            highest_high
            - close
        )
        / denominator
    )


# =========================================================
# MONEY FLOW INDEX
# =========================================================

def calculate_mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    typical_price = (
        high
        + low
        + close
    ) / 3

    raw_money_flow = (
        typical_price
        * volume
    )

    price_direction = (
        typical_price.diff()
    )

    positive_flow = (
        raw_money_flow.where(
            price_direction > 0,
            0.0,
        )
    )

    negative_flow = (
        raw_money_flow.where(
            price_direction < 0,
            0.0,
        ).abs()
    )

    positive_sum = positive_flow.rolling(
        period
    ).sum()

    negative_sum = negative_flow.rolling(
        period
    ).sum()

    money_ratio = (
        positive_sum
        / negative_sum.replace(
            0,
            np.nan,
        )
    )

    return 100 - (
        100
        / (
            1
            + money_ratio
        )
    )


# =========================================================
# TRUE RANGE AND ATR
# =========================================================

def calculate_true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> pd.Series:
    previous_close = close.shift(
        1
    )

    components = pd.concat(
        [
            high - low,
            (
                high
                - previous_close
            ).abs(),
            (
                low
                - previous_close
            ).abs(),
        ],
        axis=1,
    )

    return components.max(
        axis=1
    )


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    true_range = calculate_true_range(
        high,
        low,
        close,
    )

    return true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()


# =========================================================
# ADX AND DIRECTIONAL MOVEMENT
# =========================================================

def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
]:
    upward_move = high.diff()

    downward_move = (
        -low.diff()
    )

    plus_dm = pd.Series(
        np.where(
            (
                upward_move
                > downward_move
            )
            & (
                upward_move > 0
            ),
            upward_move,
            0.0,
        ),
        index=high.index,
    )

    minus_dm = pd.Series(
        np.where(
            (
                downward_move
                > upward_move
            )
            & (
                downward_move > 0
            ),
            downward_move,
            0.0,
        ),
        index=high.index,
    )

    atr = calculate_atr(
        high,
        low,
        close,
        period,
    )

    smoothed_plus_dm = (
        plus_dm.ewm(
            alpha=1 / period,
            adjust=False,
        ).mean()
    )

    smoothed_minus_dm = (
        minus_dm.ewm(
            alpha=1 / period,
            adjust=False,
        ).mean()
    )

    plus_di = (
        100
        * smoothed_plus_dm
        / atr.replace(
            0,
            np.nan,
        )
    )

    minus_di = (
        100
        * smoothed_minus_dm
        / atr.replace(
            0,
            np.nan,
        )
    )

    directional_sum = (
        plus_di
        + minus_di
    ).replace(
        0,
        np.nan,
    )

    directional_difference = (
        plus_di
        - minus_di
    ).abs()

    dx = (
        100
        * directional_difference
        / directional_sum
    )

    adx = dx.ewm(
        alpha=1 / period,
        adjust=False,
    ).mean()

    return (
        adx,
        plus_di,
        minus_di,
    )


# =========================================================
# BOLLINGER BANDS
# =========================================================

def calculate_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    standard_deviations: float = 2.0,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
]:
    middle_band = close.rolling(
        period
    ).mean()

    rolling_deviation = close.rolling(
        period
    ).std()

    upper_band = (
        middle_band
        + rolling_deviation
        * standard_deviations
    )

    lower_band = (
        middle_band
        - rolling_deviation
        * standard_deviations
    )

    bandwidth = (
        (
            upper_band
            - lower_band
        )
        / middle_band.replace(
            0,
            np.nan,
        )
        * 100
    )

    return (
        upper_band,
        middle_band,
        lower_band,
        bandwidth,
    )


# =========================================================
# VWAP
# =========================================================

def calculate_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 50,
) -> pd.Series:
    typical_price = (
        high
        + low
        + close
    ) / 3

    cumulative_value = (
        typical_price
        * volume
    ).rolling(
        period
    ).sum()

    cumulative_volume = volume.rolling(
        period
    ).sum()

    return (
        cumulative_value
        / cumulative_volume.replace(
            0,
            np.nan,
        )
    )


# =========================================================
# ON-BALANCE VOLUME
# =========================================================

def calculate_obv(
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    close_change = close.diff()

    direction = np.where(
        close_change > 0,
        1,
        np.where(
            close_change < 0,
            -1,
            0,
        ),
    )

    signed_volume = (
        volume
        * direction
    )

    return signed_volume.cumsum()


# =========================================================
# RATE OF CHANGE AND MOMENTUM
# =========================================================

def calculate_roc(
    close: pd.Series,
    period: int = 12,
) -> pd.Series:
    previous_price = close.shift(
        period
    )

    return (
        (
            close
            - previous_price
        )
        / previous_price.replace(
            0,
            np.nan,
        )
        * 100
    )


def calculate_momentum(
    close: pd.Series,
    period: int = 10,
) -> pd.Series:
    return (
        close
        - close.shift(
            period
        )
    )


# =========================================================
# SUPERTREND
# =========================================================

def calculate_supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[
    pd.Series,
    pd.Series,
]:
    atr = calculate_atr(
        high,
        low,
        close,
        period,
    )

    midpoint = (
        high + low
    ) / 2

    basic_upper_band = (
        midpoint
        + multiplier
        * atr
    )

    basic_lower_band = (
        midpoint
        - multiplier
        * atr
    )

    final_upper_band = (
        basic_upper_band.copy()
    )

    final_lower_band = (
        basic_lower_band.copy()
    )

    supertrend = pd.Series(
        np.nan,
        index=close.index,
        dtype=float,
    )

    trend_direction = pd.Series(
        0,
        index=close.index,
        dtype=int,
    )

    for index in range(
        1,
        len(close),
    ):
        previous_index = index - 1

        if (
            basic_upper_band.iloc[index]
            < final_upper_band.iloc[
                previous_index
            ]
            or close.iloc[
                previous_index
            ]
            > final_upper_band.iloc[
                previous_index
            ]
        ):
            final_upper_band.iloc[
                index
            ] = basic_upper_band.iloc[
                index
            ]
        else:
            final_upper_band.iloc[
                index
            ] = final_upper_band.iloc[
                previous_index
            ]

        if (
            basic_lower_band.iloc[index]
            > final_lower_band.iloc[
                previous_index
            ]
            or close.iloc[
                previous_index
            ]
            < final_lower_band.iloc[
                previous_index
            ]
        ):
            final_lower_band.iloc[
                index
            ] = basic_lower_band.iloc[
                index
            ]
        else:
            final_lower_band.iloc[
                index
            ] = final_lower_band.iloc[
                previous_index
            ]

        previous_supertrend = (
            supertrend.iloc[
                previous_index
            ]
        )

        if not valid_number(
            previous_supertrend
        ):
            if (
                close.iloc[index]
                <= final_upper_band.iloc[
                    index
                ]
            ):
                supertrend.iloc[index] = (
                    final_upper_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = -1
            else:
                supertrend.iloc[index] = (
                    final_lower_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = 1

        elif (
            previous_supertrend
            == final_upper_band.iloc[
                previous_index
            ]
        ):
            if (
                close.iloc[index]
                <= final_upper_band.iloc[
                    index
                ]
            ):
                supertrend.iloc[index] = (
                    final_upper_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = -1
            else:
                supertrend.iloc[index] = (
                    final_lower_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = 1

        else:
            if (
                close.iloc[index]
                >= final_lower_band.iloc[
                    index
                ]
            ):
                supertrend.iloc[index] = (
                    final_lower_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = 1
            else:
                supertrend.iloc[index] = (
                    final_upper_band.iloc[
                        index
                    ]
                )

                trend_direction.iloc[
                    index
                ] = -1

    return (
        supertrend,
        trend_direction,
    )


# =========================================================
# RELATIVE VOLUME
# =========================================================

def calculate_relative_volume(
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    average_volume = volume.rolling(
        period
    ).mean()

    return (
        volume
        / average_volume.replace(
            0,
            np.nan,
        )
    )


# =========================================================
# CANDLE BODY HELPERS
# =========================================================

def calculate_candle_components(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    result = frame.copy()

    result["body"] = (
        result["close"]
        - result["open"]
    ).abs()

    result["range"] = (
        result["high"]
        - result["low"]
    ).replace(
        0,
        np.nan,
    )

    result["upper_wick"] = (
        result["high"]
        - result[
            [
                "open",
                "close",
            ]
        ].max(
            axis=1
        )
    )

    result["lower_wick"] = (
        result[
            [
                "open",
                "close",
            ]
        ].min(
            axis=1
        )
        - result["low"]
    )

    result["body_percent"] = (
        result["body"]
        / result["range"]
        * 100
    )

    return result


# =========================================================
# CANDLE PATTERNS
# =========================================================

def detect_candle_patterns(
    frame: pd.DataFrame,
) -> list[str]:
    patterns: list[str] = []

    if len(frame) < 3:
        return patterns

    candle_data = (
        calculate_candle_components(
            frame
        )
    )

    latest = candle_data.iloc[
        -1
    ]

    previous = candle_data.iloc[
        -2
    ]

    third = candle_data.iloc[
        -3
    ]

    latest_bullish = (
        latest["close"]
        > latest["open"]
    )

    latest_bearish = (
        latest["close"]
        < latest["open"]
    )

    previous_bullish = (
        previous["close"]
        > previous["open"]
    )

    previous_bearish = (
        previous["close"]
        < previous["open"]
    )

    bullish_engulfing = (
        previous_bearish
        and latest_bullish
        and latest["open"]
        <= previous["close"]
        and latest["close"]
        >= previous["open"]
    )

    bearish_engulfing = (
        previous_bullish
        and latest_bearish
        and latest["open"]
        >= previous["close"]
        and latest["close"]
        <= previous["open"]
    )

    if bullish_engulfing:
        patterns.append(
            "Bullish engulfing"
        )

    if bearish_engulfing:
        patterns.append(
            "Bearish engulfing"
        )

    if (
        latest["lower_wick"]
        >= latest["body"] * 2
        and latest["upper_wick"]
        <= latest["body"]
        and latest_bullish
    ):
        patterns.append(
            "Bullish hammer"
        )

    if (
        latest["upper_wick"]
        >= latest["body"] * 2
        and latest["lower_wick"]
        <= latest["body"]
        and latest_bearish
    ):
        patterns.append(
            "Bearish shooting star"
        )

    if (
        latest["body_percent"]
        <= 10
    ):
        patterns.append(
            "Doji"
        )

    morning_star = (
        third["close"]
        < third["open"]
        and previous[
            "body_percent"
        ] <= 35
        and latest_bullish
        and latest["close"]
        > (
            third["open"]
            + third["close"]
        ) / 2
    )

    evening_star = (
        third["close"]
        > third["open"]
        and previous[
            "body_percent"
        ] <= 35
        and latest_bearish
        and latest["close"]
        < (
            third["open"]
            + third["close"]
        ) / 2
    )

    if morning_star:
        patterns.append(
            "Morning star"
        )

    if evening_star:
        patterns.append(
            "Evening star"
        )

    three_white_soldiers = all(
        candle_data.iloc[
            index
        ]["close"]
        > candle_data.iloc[
            index
        ]["open"]
        for index in [
            -3,
            -2,
            -1,
        ]
    ) and (
        candle_data.iloc[
            -1
        ]["close"]
        > candle_data.iloc[
            -2
        ]["close"]
        > candle_data.iloc[
            -3
        ]["close"]
    )

    three_black_crows = all(
        candle_data.iloc[
            index
        ]["close"]
        < candle_data.iloc[
            index
        ]["open"]
        for index in [
            -3,
            -2,
            -1,
        ]
    ) and (
        candle_data.iloc[
            -1
        ]["close"]
        < candle_data.iloc[
            -2
        ]["close"]
        < candle_data.iloc[
            -3
        ]["close"]
    )

    if three_white_soldiers:
        patterns.append(
            "Three white soldiers"
        )

    if three_black_crows:
        patterns.append(
            "Three black crows"
        )

    return patterns[:6]


# =========================================================
# BASIC CHART STRUCTURES
# =========================================================

def detect_chart_structures(
    frame: pd.DataFrame,
    lookback: int = 80,
) -> list[str]:
    structures: list[str] = []

    if len(frame) < lookback:
        return structures

    recent = frame.iloc[
        -lookback:
    ]

    highs = recent["high"]

    lows = recent["low"]

    closes = recent["close"]

    half = lookback // 2

    left_high = highs.iloc[
        :half
    ].max()

    right_high = highs.iloc[
        half:
    ].max()

    left_low = lows.iloc[
        :half
    ].min()

    right_low = lows.iloc[
        half:
    ].min()

    high_difference = (
        abs(
            left_high
            - right_high
        )
        / max(
            left_high,
            0.000001,
        )
    )

    low_difference = (
        abs(
            left_low
            - right_low
        )
        / max(
            left_low,
            0.000001,
        )
    )

    if high_difference <= 0.008:
        structures.append(
            "Possible double top"
        )

    if low_difference <= 0.008:
        structures.append(
            "Possible double bottom"
        )

    latest_close = closes.iloc[
        -1
    ]

    previous_resistance = highs.iloc[
        :-1
    ].tail(
        30
    ).max()

    previous_support = lows.iloc[
        :-1
    ].tail(
        30
    ).min()

    if (
        latest_close
        > previous_resistance
    ):
        structures.append(
            "Resistance breakout"
        )

    if (
        latest_close
        < previous_support
    ):
        structures.append(
            "Support breakdown"
        )

    recent_range = (
        highs.tail(
            30
        ).max()
        - lows.tail(
            30
        ).min()
    )

    prior_range = (
        highs.iloc[
            -60:-30
        ].max()
        - lows.iloc[
            -60:-30
        ].min()
    )

    if (
        valid_number(
            recent_range
        )
        and valid_number(
            prior_range
        )
        and prior_range > 0
        and recent_range
        < prior_range * 0.65
    ):
        structures.append(
            "Volatility compression"
        )

    high_slope = np.polyfit(
        np.arange(
            30
        ),
        highs.tail(
            30
        ).to_numpy(),
        1,
    )[0]

    low_slope = np.polyfit(
        np.arange(
            30
        ),
        lows.tail(
            30
        ).to_numpy(),
        1,
    )[0]

    if (
        high_slope < 0
        and low_slope > 0
    ):
        structures.append(
            "Symmetrical triangle candidate"
        )

    elif (
        abs(
            high_slope
        )
        < abs(
            low_slope
        ) * 0.25
        and low_slope > 0
    ):
        structures.append(
            "Ascending triangle candidate"
        )

    elif (
        abs(
            low_slope
        )
        < abs(
            high_slope
        ) * 0.25
        and high_slope < 0
    ):
        structures.append(
            "Descending triangle candidate"
        )

    return structures[:6]


# =========================================================
# DIVERGENCE HELPERS
# =========================================================

def detect_regular_divergence(
    price: pd.Series,
    oscillator: pd.Series,
    lookback: int = 30,
) -> list[str]:
    divergences: list[str] = []

    clean = pd.DataFrame(
        {
            "price": price,
            "oscillator": oscillator,
        }
    ).dropna()

    if len(clean) < lookback:
        return divergences

    recent = clean.iloc[
        -lookback:
    ]

    half = lookback // 2

    first_half = recent.iloc[
        :half
    ]

    second_half = recent.iloc[
        half:
    ]

    first_price_low = (
        first_half["price"].min()
    )

    second_price_low = (
        second_half["price"].min()
    )

    first_oscillator_low = (
        first_half[
            "oscillator"
        ].min()
    )

    second_oscillator_low = (
        second_half[
            "oscillator"
        ].min()
    )

    first_price_high = (
        first_half["price"].max()
    )

    second_price_high = (
        second_half["price"].max()
    )

    first_oscillator_high = (
        first_half[
            "oscillator"
        ].max()
    )

    second_oscillator_high = (
        second_half[
            "oscillator"
        ].max()
    )

    bullish_divergence = (
        second_price_low
        < first_price_low
        and second_oscillator_low
        > first_oscillator_low
    )

    bearish_divergence = (
        second_price_high
        > first_price_high
        and second_oscillator_high
        < first_oscillator_high
    )

    if bullish_divergence:
        divergences.append(
            "Bullish regular divergence"
        )

    if bearish_divergence:
        divergences.append(
            "Bearish regular divergence"
        )

    return divergences


# =========================================================
# COMPLETE INDICATOR FRAME
# =========================================================

def add_all_indicators(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    result = frame.copy()

    high = result["high"]

    low = result["low"]

    close = result["close"]

    volume = result["volume"]

    result["ema20"] = calculate_ema(
        close,
        20,
    )

    result["ema50"] = calculate_ema(
        close,
        50,
    )

    result["ema100"] = calculate_ema(
        close,
        100,
    )

    result["ema200"] = calculate_ema(
        close,
        200,
    )

    result["sma200"] = calculate_sma(
        close,
        200,
    )

    result["rsi"] = calculate_rsi(
        close,
        14,
    )
    result["rsi_6"] = calculate_rsi(close, 6)
    result["rsi_12"] = calculate_rsi(close, 12)
    result["rsi_24"] = calculate_rsi(close, 24)

    (
        result["macd"],
        result["macd_signal"],
        result["macd_histogram"],
    ) = calculate_macd(
        close
    )

    (
        result["stoch_rsi_k"],
        result["stoch_rsi_d"],
    ) = calculate_stoch_rsi(
        result["rsi"]
    )

    (
        result["stochastic_k"],
        result["stochastic_d"],
    ) = calculate_stochastic(
        high,
        low,
        close,
    )

    result["williams_r"] = (
        calculate_williams_r(
            high,
            low,
            close,
        )
    )

    result["mfi"] = calculate_mfi(
        high,
        low,
        close,
        volume,
    )

    result["atr"] = calculate_atr(
        high,
        low,
        close,
    )

    (
        result["adx"],
        result["plus_di"],
        result["minus_di"],
    ) = calculate_adx(
        high,
        low,
        close,
    )

    (
        result["bollinger_upper"],
        result["bollinger_middle"],
        result["bollinger_lower"],
        result["bollinger_width"],
    ) = calculate_bollinger_bands(
        close
    )

    result["vwap"] = calculate_vwap(
        high,
        low,
        close,
        volume,
    )

    result["obv"] = calculate_obv(
        close,
        volume,
    )

    result["roc"] = calculate_roc(
        close
    )

    result["momentum"] = (
        calculate_momentum(
            close
        )
    )

    (
        result["supertrend"],
        result["supertrend_direction"],
    ) = calculate_supertrend(
        high,
        low,
        close,
    )

    result["relative_volume"] = (
        calculate_relative_volume(
            volume
        )
    )

    result["volume_average_20"] = (
        volume.rolling(
            20
        ).mean()
    )

    result["highest_high_20"] = (
        high.rolling(
            20
        ).max()
    )

    result["lowest_low_20"] = (
        low.rolling(
            20
        ).min()
    )

    result["highest_high_50"] = (
        high.rolling(
            50
        ).max()
    )

    result["lowest_low_50"] = (
        low.rolling(
            50
        ).min()
    )

    return result
