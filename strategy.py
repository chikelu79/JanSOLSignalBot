from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from config import (
    ATR_STOP_MULTIPLIER,
    CONFIRMED_THRESHOLD,
    MINIMUM_AVAILABLE_TIMEFRAMES,
    MINIMUM_REWARD_RISK,
    STRONG_THRESHOLD,
    TIMEFRAMES,
    TP1_REWARD_MULTIPLIER,
    TP2_REWARD_MULTIPLIER,
    TP3_REWARD_MULTIPLIER,
    WATCH_THRESHOLD,
)

from indicators import (
    add_all_indicators,
    clamp,
    detect_candle_patterns,
    detect_chart_structures,
    detect_regular_divergence,
    safe_float,
)
from bot_state import get_risk_style, get_trading_horizon
from trading_profile import get_profile, mfi_reversal_min_change


logger = logging.getLogger(__name__)


def active_profile():
    return get_profile(get_trading_horizon(), get_risk_style())


# =========================================================
# RESULT MODELS
# =========================================================

@dataclass
class TimeframeSignal:
    interval: str
    label: str

    price: float
    score: float
    direction: str
    confidence: int

    ema20: float
    ema50: float
    ema100: float
    ema200: float
    sma200: float

    rsi: float
    previous_rsi: float
    rsi_6: float
    rsi_12: float
    rsi_24: float
    previous_rsi_6: float
    previous_rsi_12: float
    previous_rsi_24: float

    macd: float
    macd_signal: float
    macd_histogram: float
    previous_macd: float
    previous_macd_signal: float
    previous_macd_histogram: float

    stoch_rsi_k: float
    stoch_rsi_d: float
    previous_stoch_rsi_k: float
    previous_stoch_rsi_d: float

    stochastic_k: float
    stochastic_d: float

    williams_r: float
    mfi: float
    previous_mfi: float
    two_back_mfi: float

    atr: float
    atr_percent: float

    adx: float
    plus_di: float
    minus_di: float

    bollinger_upper: float
    bollinger_middle: float
    bollinger_lower: float
    bollinger_width: float

    vwap: float
    obv: float
    roc: float
    momentum: float

    supertrend: float
    supertrend_direction: int

    relative_volume: float

    support: float
    resistance: float

    breakout_up: bool
    breakout_down: bool

    candle_patterns: list[str]
    chart_structures: list[str]
    divergences: list[str]
    reasons: list[str]


@dataclass
class TradePlan:
    side: str

    entry_low: float
    entry_high: float

    stop_loss: float
    invalidation: float

    tp1: float
    tp2: float
    tp3: float

    risk_per_unit: float

    reward_risk_tp1: float
    reward_risk_tp2: float
    reward_risk_tp3: float


@dataclass
class MarketSignal:
    symbol: str

    direction: str
    stage: str

    score: float
    confidence: int

    price: float

    bullish_timeframes: int
    bearish_timeframes: int
    neutral_timeframes: int

    analyses: dict[str, TimeframeSignal]
    errors: dict[str, str]

    trade_plan: TradePlan | None

    supporting_reasons: list[str]
    warnings: list[str]


# =========================================================
# DIRECTION HELPERS
# =========================================================

def score_to_direction(
    score: float,
) -> str:
    profile = active_profile()
    if score >= profile.strong_threshold:
        return "STRONG LONG"

    if score >= profile.watch_threshold:
        return "LONG"

    if score <= -profile.strong_threshold:
        return "STRONG SHORT"

    if score <= -profile.watch_threshold:
        return "SHORT"

    return "WAIT"


def score_to_confidence(
    score: float,
) -> int:
    return int(
        clamp(
            abs(score),
            0,
            95,
        )
    )


def score_to_stage(
    score: float,
    confirmed: bool,
) -> str:
    absolute_score = abs(score)
    profile = active_profile()

    if confirmed:
        if absolute_score >= profile.strong_threshold:
            return "STRONG"

        return "CONFIRMED"

    if absolute_score >= profile.watch_threshold:
        return "WATCH"

    return "NEUTRAL"


def is_bullish_direction(
    direction: str,
) -> bool:
    return direction in {
        "LONG",
        "STRONG LONG",
    }


def is_bearish_direction(
    direction: str,
) -> bool:
    return direction in {
        "SHORT",
        "STRONG SHORT",
    }


# =========================================================
# PATTERN SCORING
# =========================================================

def score_candle_patterns(
    patterns: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    bullish_patterns = {
        "bullish engulfing",
        "bullish hammer",
        "morning star",
        "three white soldiers",
    }

    bearish_patterns = {
        "bearish engulfing",
        "bearish shooting star",
        "evening star",
        "three black crows",
    }

    for pattern in patterns:
        normalized = pattern.lower()

        if normalized in bullish_patterns:
            score += 3
            reasons.append(pattern)

        elif normalized in bearish_patterns:
            score -= 3
            reasons.append(pattern)

        elif normalized == "doji":
            reasons.append(
                "Doji shows market indecision"
            )

    return score, reasons


def score_chart_structures(
    structures: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    for structure in structures:
        normalized = structure.lower()

        if any(
            phrase in normalized
            for phrase in [
                "double bottom",
                "resistance breakout",
                "ascending triangle",
            ]
        ):
            score += 5
            reasons.append(structure)

        elif any(
            phrase in normalized
            for phrase in [
                "double top",
                "support breakdown",
                "descending triangle",
            ]
        ):
            score -= 5
            reasons.append(structure)

        elif any(
            phrase in normalized
            for phrase in [
                "compression",
                "symmetrical triangle",
            ]
        ):
            reasons.append(structure)

    return score, reasons


def score_divergences(
    divergences: list[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    for divergence in divergences:
        normalized = divergence.lower()

        if "bullish" in normalized:
            score += 7
            reasons.append(divergence)

        elif "bearish" in normalized:
            score -= 7
            reasons.append(divergence)

    return score, reasons
  # =========================================================
# TIMEFRAME ANALYSIS
# =========================================================

def analyze_timeframe(
    interval: str,
    frame: pd.DataFrame,
) -> TimeframeSignal:
    if interval not in TIMEFRAMES:
        raise ValueError(
            f"Unsupported timeframe: {interval}"
        )

    if len(frame) < 210:
        raise ValueError(
            f"Not enough candles for {interval}. "
            f"Received {len(frame)}."
        )

    indicator_frame = add_all_indicators(
        frame
    )

        # Some optional indicators may temporarily contain NaN.
    # Only the core indicators are required to build a signal.

    required_columns = [
        "close",
        "high",
        "low",
        "ema20",
        "ema50",
        "ema100",
        "ema200",
        "sma200",
        "rsi",
        "atr",
    ]

    missing_required = [
        column
        for column in required_columns
        if column not in indicator_frame.columns
    ]

    if missing_required:
        raise ValueError(
            "Missing required indicator columns: "
            + ", ".join(missing_required)
        )

    optional_defaults = {
        "macd": 0.0,
        "macd_signal": 0.0,
        "macd_histogram": 0.0,
        "rsi_6": 50.0,
        "rsi_12": 50.0,
        "rsi_24": 50.0,
        "stoch_rsi_k": 50.0,
        "stoch_rsi_d": 50.0,
        "stochastic_k": 50.0,
        "stochastic_d": 50.0,
        "williams_r": -50.0,
        "mfi": 50.0,
        "adx": 0.0,
        "plus_di": 0.0,
        "minus_di": 0.0,
        "bollinger_upper": 0.0,
        "bollinger_middle": 0.0,
        "bollinger_lower": 0.0,
        "bollinger_width": 0.0,
        "vwap": 0.0,
        "obv": 0.0,
        "roc": 0.0,
        "momentum": 0.0,
        "supertrend": 0.0,
        "supertrend_direction": 0,
        "relative_volume": 1.0,
    }

    for column, default_value in optional_defaults.items():
        if column not in indicator_frame.columns:
            indicator_frame[column] = default_value

    clean_frame = indicator_frame.dropna(
        subset=required_columns
    ).copy()

    optional_columns = list(
        optional_defaults.keys()
    )

    clean_frame[optional_columns] = (
        clean_frame[optional_columns]
        .ffill()
        .fillna(
            optional_defaults
        )
    )

    if len(clean_frame) < 3:
        raise ValueError(
            f"Indicator calculations are incomplete "
            f"for {interval}."
        )

    latest = clean_frame.iloc[-1]
    previous = clean_frame.iloc[-2]

    price = safe_float(
        latest["close"]
    )

    ema20 = safe_float(
        latest["ema20"]
    )

    ema50 = safe_float(
        latest["ema50"]
    )

    ema100 = safe_float(
        latest["ema100"]
    )

    ema200 = safe_float(
        latest["ema200"]
    )

    sma200 = safe_float(
        latest["sma200"]
    )

    rsi = safe_float(
        latest["rsi"],
        50.0,
    )
    previous_rsi = safe_float(previous["rsi"], 50.0)
    rsi_6 = safe_float(latest["rsi_6"], 50.0)
    rsi_12 = safe_float(latest["rsi_12"], 50.0)
    rsi_24 = safe_float(latest["rsi_24"], 50.0)
    previous_rsi_6 = safe_float(previous["rsi_6"], 50.0)
    previous_rsi_12 = safe_float(previous["rsi_12"], 50.0)
    previous_rsi_24 = safe_float(previous["rsi_24"], 50.0)

    macd = safe_float(
        latest["macd"]
    )

    macd_signal = safe_float(
        latest["macd_signal"]
    )

    macd_histogram = safe_float(
        latest["macd_histogram"]
    )

    previous_macd = safe_float(previous["macd"])
    previous_macd_signal = safe_float(previous["macd_signal"])

    previous_macd_histogram = safe_float(
        previous["macd_histogram"]
    )

    stoch_rsi_k = safe_float(
        latest["stoch_rsi_k"],
        50.0,
    )

    stoch_rsi_d = safe_float(
        latest["stoch_rsi_d"],
        50.0,
    )

    previous_stoch_rsi_k = safe_float(
        previous["stoch_rsi_k"],
        50.0,
    )

    previous_stoch_rsi_d = safe_float(
        previous["stoch_rsi_d"],
        50.0,
    )

    stochastic_k = safe_float(
        latest["stochastic_k"],
        50.0,
    )

    stochastic_d = safe_float(
        latest["stochastic_d"],
        50.0,
    )

    williams_r = safe_float(
        latest["williams_r"],
        -50.0,
    )

    mfi = safe_float(
        latest["mfi"],
        50.0,
    )
    previous_mfi = safe_float(previous["mfi"], 50.0)
    two_back_mfi = safe_float(clean_frame.iloc[-3]["mfi"], 50.0)

    atr = safe_float(
        latest["atr"]
    )

    atr_percent = (
        atr / price * 100
        if price > 0
        else 0.0
    )

    adx = safe_float(
        latest["adx"]
    )

    plus_di = safe_float(
        latest["plus_di"]
    )

    minus_di = safe_float(
        latest["minus_di"]
    )

    bollinger_upper = safe_float(
        latest["bollinger_upper"]
    )

    bollinger_middle = safe_float(
        latest["bollinger_middle"]
    )

    bollinger_lower = safe_float(
        latest["bollinger_lower"]
    )

    bollinger_width = safe_float(
        latest["bollinger_width"]
    )

    vwap = safe_float(
        latest["vwap"]
    )

    obv = safe_float(
        latest["obv"]
    )

    previous_obv = safe_float(
        previous["obv"]
    )

    roc = safe_float(
        latest["roc"]
    )

    momentum = safe_float(
        latest["momentum"]
    )

    supertrend = safe_float(
        latest["supertrend"]
    )

    supertrend_direction = int(
        safe_float(
            latest["supertrend_direction"]
        )
    )

    relative_volume = safe_float(
        latest["relative_volume"],
        1.0,
    )

    completed_frame = clean_frame.iloc[:-1]

    resistance = safe_float(
        completed_frame[
            "high"
        ].iloc[-50:].max()
    )

    support = safe_float(
        completed_frame[
            "low"
        ].iloc[-50:].min()
    )

    breakout_up = (
        price > resistance
        and relative_volume >= 1.15
    )

    breakout_down = (
        price < support
        and relative_volume >= 1.15
    )

    candle_patterns = detect_candle_patterns(
        clean_frame
    )

    chart_structures = detect_chart_structures(
        clean_frame
    )

    divergences = detect_regular_divergence(
        clean_frame["close"],
        clean_frame["rsi"],
    )

    score = 0.0
    reasons: list[str] = []

    # =====================================================
    # TREND STRUCTURE
    # =====================================================

    if price > ema20:
        score += 5
        reasons.append(
            "Price is above EMA 20"
        )
    else:
        score -= 5

    if ema20 > ema50:
        score += 7
        reasons.append(
            "EMA 20 is above EMA 50"
        )
    else:
        score -= 7

    if ema50 > ema100:
        score += 7
        reasons.append(
            "EMA 50 is above EMA 100"
        )
    else:
        score -= 7

    if ema100 > ema200:
        score += 9
        reasons.append(
            "EMA 100 is above EMA 200"
        )
    else:
        score -= 9

    if price > sma200:
        score += 5
        reasons.append(
            "Price is above SMA 200"
        )
    else:
        score -= 5

    if price > vwap:
        score += 5
        reasons.append(
            "Price is above VWAP"
        )
    else:
        score -= 5

    if supertrend_direction > 0:
        score += 8
        reasons.append(
            "Supertrend is bullish"
        )
    elif supertrend_direction < 0:
        score -= 8

    # =====================================================
    # RSI
    # =====================================================

    if 53 <= rsi <= 68:
        score += 8
        reasons.append(
            f"RSI is bullish at {rsi:.1f}"
        )

    elif 32 <= rsi <= 47:
        score -= 8

    elif rsi < 28:
        score += 3
        reasons.append(
            f"RSI is deeply oversold at {rsi:.1f}"
        )

    elif rsi > 72:
        score -= 3
        reasons.append(
            f"RSI is overbought at {rsi:.1f}"
        )

    if previous_rsi < 30 <= rsi:
        score += 8
        reasons.append(f"RSI exited oversold territory ({previous_rsi:.1f} → {rsi:.1f})")
    elif previous_rsi > 70 >= rsi:
        score -= 8
        reasons.append(f"RSI exited overbought territory ({previous_rsi:.1f} → {rsi:.1f})")

    if previous_rsi_6 <= previous_rsi_12 and rsi_6 - rsi_12 >= 0.5:
        score += 5
        reasons.append(f"RSI 6 crossed above RSI 12 ({rsi_6:.1f} > {rsi_12:.1f})")
    elif previous_rsi_6 >= previous_rsi_12 and rsi_12 - rsi_6 >= 0.5:
        score -= 5
        reasons.append(f"RSI 6 crossed below RSI 12 ({rsi_6:.1f} < {rsi_12:.1f})")
    if previous_rsi_12 <= previous_rsi_24 and rsi_12 - rsi_24 >= 0.5:
        score += 6
        reasons.append(f"RSI 12 crossed above RSI 24 ({rsi_12:.1f} > {rsi_24:.1f})")
    elif previous_rsi_12 >= previous_rsi_24 and rsi_24 - rsi_12 >= 0.5:
        score -= 6
        reasons.append(f"RSI 12 crossed below RSI 24 ({rsi_12:.1f} < {rsi_24:.1f})")

    # =====================================================
    # MACD
    # =====================================================

    if macd > macd_signal:
        score += 8
        reasons.append(
            "MACD is above its signal line"
        )
    else:
        score -= 8

    if previous_macd <= previous_macd_signal and macd > macd_signal:
        score += 7
        reasons.append("Fresh bullish MACD line crossover")
    elif previous_macd >= previous_macd_signal and macd < macd_signal:
        score -= 7
        reasons.append("Fresh bearish MACD line crossover")

    if (
        macd_histogram
        > previous_macd_histogram
    ):
        score += 5
        reasons.append(
            "MACD momentum is improving"
        )
    else:
        score -= 5

    if (
        previous_macd_histogram <= 0
        and macd_histogram > 0
    ):
        score += 5
        reasons.append(
            "Fresh bullish MACD histogram flip"
        )

    elif (
        previous_macd_histogram >= 0
        and macd_histogram < 0
    ):
        score -= 5
        reasons.append(
            "Fresh bearish MACD histogram flip"
        )

    # =====================================================
    # STOCHASTIC RSI
    # =====================================================

    bullish_stoch_rsi_cross = (
        stoch_rsi_k - stoch_rsi_d >= 2.0
        and previous_stoch_rsi_k
        <= previous_stoch_rsi_d
    )

    bearish_stoch_rsi_cross = (
        stoch_rsi_d - stoch_rsi_k >= 2.0
        and previous_stoch_rsi_k
        >= previous_stoch_rsi_d
    )

    if bullish_stoch_rsi_cross:
        extreme_cross = min(previous_stoch_rsi_k, previous_stoch_rsi_d) <= 20
        score += 10 if extreme_cross else 7
        reasons.append(
            "Bullish Stochastic RSI crossover from oversold" if extreme_cross else "Bullish Stochastic RSI crossover"
        )

    elif bearish_stoch_rsi_cross:
        extreme_cross = max(previous_stoch_rsi_k, previous_stoch_rsi_d) >= 80
        score -= 10 if extreme_cross else 7
        reasons.append(
            "Bearish Stochastic RSI crossover from overbought" if extreme_cross else "Bearish Stochastic RSI crossover"
        )

    elif stoch_rsi_k > stoch_rsi_d:
        score += 3

    else:
        score -= 3

    # =====================================================
    # STANDARD STOCHASTIC
    # =====================================================

    if (
        stochastic_k > stochastic_d
        and stochastic_k < 80
    ):
        score += 3

    elif (
        stochastic_k < stochastic_d
        and stochastic_k > 20
    ):
        score -= 3

    # =====================================================
    # WILLIAMS %R
    # =====================================================

    if -75 <= williams_r <= -35:
        score += 4

    elif williams_r < -85:
        score += 2
        reasons.append(
            "Williams %R is deeply oversold"
        )

    elif williams_r > -15:
        score -= 2
        reasons.append(
            "Williams %R is overbought"
        )

    # =====================================================
    # MONEY FLOW INDEX
    # =====================================================

    if 52 <= mfi <= 75:
        score += 6
        reasons.append(
            f"MFI confirms buying pressure at {mfi:.1f}"
        )

    elif 25 <= mfi <= 45:
        score -= 6

    elif mfi < 18:
        score += 2
        reasons.append(
            "MFI is deeply oversold"
        )

    elif mfi > 82:
        score -= 2
        reasons.append(
            "MFI is overbought"
        )

    mfi_min_change = mfi_reversal_min_change(active_profile())
    if mfi - previous_mfi >= mfi_min_change and previous_mfi <= two_back_mfi and previous_mfi <= 55.0:
        score += 5
        reasons.append(f"MFI money flow turned upward ({previous_mfi:.1f} → {mfi:.1f})")
    elif previous_mfi - mfi >= mfi_min_change and previous_mfi >= two_back_mfi and previous_mfi >= 45.0:
        score -= 5
        reasons.append(f"MFI money flow turned downward ({previous_mfi:.1f} → {mfi:.1f})")

    # =====================================================
    # ADX AND DIRECTIONAL MOVEMENT
    # =====================================================

    if adx >= 22:
        if plus_di > minus_di:
            score += 8
            reasons.append(
                f"ADX confirms bullish trend strength "
                f"at {adx:.1f}"
            )
        else:
            score -= 8
            reasons.append(
                f"ADX confirms bearish trend strength "
                f"at {adx:.1f}"
            )

    elif adx < 16:
        reasons.append(
            "ADX indicates a weak or sideways market"
        )

    # =====================================================
    # BOLLINGER BANDS
    # =====================================================

    if price > bollinger_middle:
        score += 3
    else:
        score -= 3

    if price < bollinger_lower:
        score += 2
        reasons.append(
            "Price is below the lower Bollinger Band"
        )

    elif price > bollinger_upper:
        score -= 2
        reasons.append(
            "Price is above the upper Bollinger Band"
        )

    if bollinger_width < 3:
        reasons.append(
            "Bollinger Bands show volatility compression"
        )

    # =====================================================
    # VOLUME, OBV, ROC AND MOMENTUM
    # =====================================================

    if relative_volume >= 1.5:
        if score >= 0:
            score += 7
        else:
            score -= 7

        reasons.append(
            f"Relative volume is {relative_volume:.2f}x"
        )

    elif relative_volume < 0.65:
        reasons.append(
            "Volume is below normal"
        )

    if obv > previous_obv:
        score += 3
        reasons.append(
            "OBV is rising"
        )
    else:
        score -= 3

    if roc > 0:
        score += 3
    elif roc < 0:
        score -= 3

    if momentum > 0:
        score += 2
    elif momentum < 0:
        score -= 2

    # =====================================================
    # BREAKOUTS
    # =====================================================

    if breakout_up:
        score += 14
        reasons.append(
            "Resistance breakout with volume confirmation"
        )

    if breakout_down:
        score -= 14
        reasons.append(
            "Support breakdown with volume confirmation"
        )

    # =====================================================
    # CANDLE PATTERNS
    # =====================================================

    candle_score, candle_reasons = (
        score_candle_patterns(
            candle_patterns
        )
    )

    score += candle_score

    reasons.extend(
        candle_reasons
    )

    # =====================================================
    # CHART STRUCTURES
    # =====================================================

    structure_score, structure_reasons = (
        score_chart_structures(
            chart_structures
        )
    )

    score += structure_score

    reasons.extend(
        structure_reasons
    )

    # =====================================================
    # DIVERGENCES
    # =====================================================

    divergence_score, divergence_reasons = (
        score_divergences(
            divergences
        )
    )

    score += divergence_score

    reasons.extend(
        divergence_reasons
    )

    score = clamp(
        score,
        -100,
        100,
    )

    direction = score_to_direction(
        score
    )

    confidence = score_to_confidence(
        score
    )

    unique_reasons: list[str] = []

    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(
                reason
            )

    return TimeframeSignal(
        interval=interval,
        label=TIMEFRAMES[
            interval
        ]["label"],
        price=price,
        score=score,
        direction=direction,
        confidence=confidence,
        ema20=ema20,
        ema50=ema50,
        ema100=ema100,
        ema200=ema200,
        sma200=sma200,
        rsi=rsi,
        previous_rsi=previous_rsi,
        rsi_6=rsi_6,
        rsi_12=rsi_12,
        rsi_24=rsi_24,
        previous_rsi_6=previous_rsi_6,
        previous_rsi_12=previous_rsi_12,
        previous_rsi_24=previous_rsi_24,
        macd=macd,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        previous_macd=previous_macd,
        previous_macd_signal=previous_macd_signal,
        previous_macd_histogram=previous_macd_histogram,
        stoch_rsi_k=stoch_rsi_k,
        stoch_rsi_d=stoch_rsi_d,
        previous_stoch_rsi_k=previous_stoch_rsi_k,
        previous_stoch_rsi_d=previous_stoch_rsi_d,
        stochastic_k=stochastic_k,
        stochastic_d=stochastic_d,
        williams_r=williams_r,
        mfi=mfi,
        previous_mfi=previous_mfi,
        two_back_mfi=two_back_mfi,
        atr=atr,
        atr_percent=atr_percent,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
        bollinger_upper=bollinger_upper,
        bollinger_middle=bollinger_middle,
        bollinger_lower=bollinger_lower,
        bollinger_width=bollinger_width,
        vwap=vwap,
        obv=obv,
        roc=roc,
        momentum=momentum,
        supertrend=supertrend,
        supertrend_direction=supertrend_direction,
        relative_volume=relative_volume,
        support=support,
        resistance=resistance,
        breakout_up=breakout_up,
        breakout_down=breakout_down,
        candle_patterns=candle_patterns,
        chart_structures=chart_structures,
        divergences=divergences,
        reasons=unique_reasons[:12],
    )
  # =========================================================
# MULTI-TIMEFRAME CONFIRMATION
# =========================================================

def determine_confirmation(
    overall_score: float,
    analyses: dict[str, TimeframeSignal],
) -> tuple[bool, list[str]]:
    profile = active_profile()
    primary_text = " and ".join(profile.primary_timeframes)
    warnings: list[str] = []

    bullish_bias = overall_score >= 0

    if bullish_bias:
        short_term_aligned = all(
            interval in analyses
            and analyses[interval].score >= profile.alignment_score
            for interval in profile.primary_timeframes
        )

        higher_timeframe_count = sum(
            1
            for interval in profile.confirmation_timeframes
            if (
                interval in analyses
                and analyses[interval].score >= max(15.0, profile.alignment_score - 15.0)
            )
        )

        volume_confirmation = any(
            analysis.relative_volume >= profile.volume_confirmation
            for analysis in analyses.values()
        )

        breakout_confirmation = any(
            analysis.breakout_up
            for analysis in analyses.values()
        )

        confirmed = (
            overall_score >= profile.confirmed_threshold
            and short_term_aligned
            and higher_timeframe_count >= profile.higher_timeframe_count
            and (
                volume_confirmation
                or breakout_confirmation
            )
        )

        if not short_term_aligned:
            warnings.append(
                f"The {primary_text} profile timeframes are not fully aligned bullish."
            )

        if higher_timeframe_count < profile.higher_timeframe_count:
            warnings.append(
                f"Fewer than {profile.higher_timeframe_count} profile confirmation timeframes support the long setup."
            )

        if not (
            volume_confirmation
            or breakout_confirmation
        ):
            warnings.append(
                "The long setup lacks volume or breakout confirmation."
            )

        return confirmed, warnings

    short_term_aligned = all(
        interval in analyses
        and analyses[interval].score <= -profile.alignment_score
        for interval in profile.primary_timeframes
    )

    higher_timeframe_count = sum(
        1
        for interval in profile.confirmation_timeframes
        if (
            interval in analyses
            and analyses[interval].score <= -max(15.0, profile.alignment_score - 15.0)
        )
    )

    volume_confirmation = any(
        analysis.relative_volume >= profile.volume_confirmation
        for analysis in analyses.values()
    )

    breakdown_confirmation = any(
        analysis.breakout_down
        for analysis in analyses.values()
    )

    confirmed = (
        overall_score <= -profile.confirmed_threshold
        and short_term_aligned
        and higher_timeframe_count >= profile.higher_timeframe_count
        and (
            volume_confirmation
            or breakdown_confirmation
        )
    )

    if not short_term_aligned:
        warnings.append(
            f"The {primary_text} profile timeframes are not fully aligned bearish."
        )

    if higher_timeframe_count < profile.higher_timeframe_count:
        warnings.append(
            f"Fewer than {profile.higher_timeframe_count} profile confirmation timeframes support the short setup."
        )

    if not (
        volume_confirmation
        or breakdown_confirmation
    ):
        warnings.append(
            "The short setup lacks volume or breakdown confirmation."
        )

    return confirmed, warnings


# =========================================================
# SUPPORTING REASONS
# =========================================================

def collect_supporting_reasons(
    analyses: dict[str, TimeframeSignal],
    overall_score: float,
) -> list[str]:
    collected: list[str] = []

    profile = active_profile()
    profile_order = list(profile.primary_timeframes + profile.confirmation_timeframes)
    preferred_intervals = profile_order + [
        interval for interval in ("5m", "15m", "1h", "4h", "8h", "1d")
        if interval not in profile_order
    ]

    bullish_bias = overall_score >= 0
    mixed_wait = abs(overall_score) < profile.watch_threshold

    for interval in preferred_intervals:
        analysis = analyses.get(
            interval
        )

        if analysis is None:
            continue

        supports_direction = mixed_wait or (
            analysis.score > 0
            if bullish_bias
            else analysis.score < 0
        )

        if not supports_direction:
            continue

        interval_added = 0
        interval_limit = 2 if mixed_wait else 8
        for reason in analysis.reasons:
            formatted_reason = (
                f"{interval}: {reason}"
            )

            if formatted_reason not in collected:
                collected.append(
                    formatted_reason
                )
                interval_added += 1

            if len(collected) >= 8:
                return collected
            if interval_added >= interval_limit:
                break

    return collected


# =========================================================
# TIMEFRAME COUNTS
# =========================================================

def count_timeframe_directions(
    analyses: dict[str, TimeframeSignal],
) -> tuple[int, int, int]:
    bullish_timeframes = sum(
        1
        for analysis in analyses.values()
        if is_bullish_direction(
            analysis.direction
        )
    )

    bearish_timeframes = sum(
        1
        for analysis in analyses.values()
        if is_bearish_direction(
            analysis.direction
        )
    )

    neutral_timeframes = (
        len(analyses)
        - bullish_timeframes
        - bearish_timeframes
    )

    return (
        bullish_timeframes,
        bearish_timeframes,
        neutral_timeframes,
    )


# =========================================================
# WEIGHTED SCORE
# =========================================================

def calculate_weighted_score(
    analyses: dict[str, TimeframeSignal],
) -> float:
    weighted_score = 0.0
    total_weight = 0.0

    profile = active_profile()
    for interval, analysis in analyses.items():
        if interval not in TIMEFRAMES:
            continue

        weight = float(profile.weights.get(interval, TIMEFRAMES[interval]["weight"]))

        weighted_score += (
            analysis.score
            * weight
        )

        total_weight += weight

    if total_weight <= 0:
        return 0.0

    return clamp(
        weighted_score
        / total_weight,
        -100,
        100,
    )


# =========================================================
# REFERENCE ANALYSIS
# =========================================================

def get_reference_analysis(
    analyses: dict[str, TimeframeSignal],
) -> TimeframeSignal:
    reference = (
        analyses.get("15m")
        or analyses.get("1h")
        or analyses.get("5m")
    )

    if reference is not None:
        return reference

    try:
        return next(
            iter(
                analyses.values()
            )
        )

    except StopIteration as error:
        raise ValueError(
            "No timeframe analysis is available."
        ) from error
      # =========================================================
# TRADE PLAN ENGINE
# =========================================================

def create_trade_plan(
    direction: str,
    price: float,
    analyses: dict[str, TimeframeSignal],
) -> TradePlan | None:
    if direction == "WAIT":
        return None

    reference = get_reference_analysis(
        analyses
    )

    atr = reference.atr

    if atr <= 0:
        atr = price * 0.008

    # =====================================================
    # LONG PLAN
    # =====================================================

    if is_bullish_direction(
        direction
    ):
        support_candidates: list[float] = []
        for interval in ("5m", "15m", "1h", "4h"):
            analysis = analyses.get(interval)
            if analysis is None:
                continue
            support_candidates.extend(
                level
                for level in (
                    analysis.support,
                    analysis.ema20,
                    analysis.ema50,
                    analysis.vwap,
                    analysis.bollinger_middle,
                )
                if (
                    level > 0
                    and level < price
                    and price - level <= atr * 4.0
                )
            )

        nearest_support = (
            max(
                support_candidates
            )
            if support_candidates
            else price - atr * 0.75
        )

        entry_low = max(0.00000001, nearest_support - atr * 0.15)
        entry_high = min(price, nearest_support + atr * 0.15)

        atr_stop = (
            entry_low
            - atr * active_profile().atr_stop_multiplier
        )

        structural_stop = (
            nearest_support
            - atr * 0.15
        )

        stop_loss = min(
            atr_stop,
            structural_stop,
        )

        risk_per_unit = max(
            entry_high
            - stop_loss,
            atr * 0.50,
        )

        tp1 = (
            entry_high
            + risk_per_unit
            * TP1_REWARD_MULTIPLIER
        )

        tp2 = (
            entry_high
            + risk_per_unit
            * TP2_REWARD_MULTIPLIER
        )

        tp3 = (
            entry_high
            + risk_per_unit
            * TP3_REWARD_MULTIPLIER
        )

        reward_risk_tp1 = (
            tp1 - entry_high
        ) / risk_per_unit

        reward_risk_tp2 = (
            tp2 - entry_high
        ) / risk_per_unit

        reward_risk_tp3 = (
            tp3 - entry_high
        ) / risk_per_unit

        return TradePlan(
            side="LONG",
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            invalidation=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            risk_per_unit=risk_per_unit,
            reward_risk_tp1=reward_risk_tp1,
            reward_risk_tp2=reward_risk_tp2,
            reward_risk_tp3=reward_risk_tp3,
        )

    # =====================================================
    # SHORT PLAN
    # =====================================================

    resistance_candidates: list[float] = []
    for interval in ("5m", "15m", "1h", "4h"):
        analysis = analyses.get(interval)
        if analysis is None:
            continue
        resistance_candidates.extend(
            level
            for level in (
                analysis.resistance,
                analysis.ema20,
                analysis.ema50,
                analysis.vwap,
                analysis.bollinger_middle,
            )
            if (
                level > price
                and level - price <= atr * 4.0
            )
        )

    nearest_resistance = (
        min(
            resistance_candidates
        )
        if resistance_candidates
        else price + atr * 0.75
    )

    entry_low = max(price, nearest_resistance - atr * 0.15)
    entry_high = nearest_resistance + atr * 0.15

    atr_stop = (
        entry_high
        + atr * active_profile().atr_stop_multiplier
    )

    structural_stop = (
        nearest_resistance
        + atr * 0.15
    )

    stop_loss = max(
        atr_stop,
        structural_stop,
    )

    risk_per_unit = max(
        stop_loss
        - entry_low,
        atr * 0.50,
    )

    tp1 = (
        entry_low
        - risk_per_unit
        * TP1_REWARD_MULTIPLIER
    )

    tp2 = (
        entry_low
        - risk_per_unit
        * TP2_REWARD_MULTIPLIER
    )

    tp3 = (
        entry_low
        - risk_per_unit
        * TP3_REWARD_MULTIPLIER
    )

    reward_risk_tp1 = (
        entry_low - tp1
    ) / risk_per_unit

    reward_risk_tp2 = (
        entry_low - tp2
    ) / risk_per_unit

    reward_risk_tp3 = (
        entry_low - tp3
    ) / risk_per_unit

    return TradePlan(
        side="SHORT",
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        invalidation=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_per_unit=risk_per_unit,
        reward_risk_tp1=reward_risk_tp1,
        reward_risk_tp2=reward_risk_tp2,
        reward_risk_tp3=reward_risk_tp3,
    )


# =========================================================
# TRADE PLAN VALIDATION
# =========================================================

def validate_trade_plan(
    trade_plan: TradePlan | None,
) -> list[str]:
    warnings: list[str] = []

    if trade_plan is None:
        return warnings

    if (
        trade_plan.reward_risk_tp2
        < active_profile().minimum_reward_risk
    ):
        warnings.append(
            "The projected TP2 reward-to-risk ratio "
            "is below the configured minimum."
        )

    if (
        trade_plan.entry_low
        <= 0
        or trade_plan.entry_high
        <= 0
        or trade_plan.stop_loss
        <= 0
    ):
        warnings.append(
            "One or more generated trade levels "
            "are invalid."
        )

    if (
        trade_plan.side == "LONG"
        and trade_plan.stop_loss
        >= trade_plan.entry_low
    ):
        warnings.append(
            "The generated long stop is not below "
            "the entry zone."
        )

    if (
        trade_plan.side == "SHORT"
        and trade_plan.stop_loss
        <= trade_plan.entry_high
    ):
        warnings.append(
            "The generated short stop is not above "
            "the entry zone."
        )

    return warnings
  # =========================================================
# COMPLETE MARKET SIGNAL
# =========================================================

def build_market_signal(
    symbol: str,
    candle_data: dict[str, pd.DataFrame],
    errors: dict[str, str] | None = None,
) -> MarketSignal:
    if errors is None:
        errors = {}

    analyses: dict[str, TimeframeSignal] = {}

    combined_errors = dict(
        errors
    )

    # Analyze every available timeframe.

    for interval, frame in candle_data.items():
        try:
            analyses[
                interval
            ] = analyze_timeframe(
                interval=interval,
                frame=frame,
            )

        except Exception as error:
            logger.exception(
                "Strategy analysis failed for %s %s",
                symbol,
                interval,
            )

            combined_errors[
                interval
            ] = (
                f"{type(error).__name__}: "
                f"{error}"
            )

    if (
        len(analyses)
        < MINIMUM_AVAILABLE_TIMEFRAMES
    ):
        available_intervals = ", ".join(
            sorted(
                analyses.keys()
            )
        ) or "none"

        error_details = "; ".join(
            f"{interval}: {message}"
            for interval, message
            in combined_errors.items()
        )

        raise RuntimeError(
            "Too few timeframes are available "
            "to create a reliable signal. "
            f"Available: {available_intervals}. "
            f"Errors: {error_details[:500]}"
        )

    # Calculate the combined multi-timeframe score.

    overall_score = (
        calculate_weighted_score(
            analyses
        )
    )

    direction = score_to_direction(
        overall_score
    )

    confirmed, confirmation_warnings = (
        determine_confirmation(
            overall_score=overall_score,
            analyses=analyses,
        )
    )

    stage = score_to_stage(
        score=overall_score,
        confirmed=confirmed,
    )

    confidence = score_to_confidence(
        overall_score
    )

    (
        bullish_timeframes,
        bearish_timeframes,
        neutral_timeframes,
    ) = count_timeframe_directions(
        analyses
    )

    # Prefer the shortest timeframe for current price.

    price_reference = (
        analyses.get("5m")
        or analyses.get("15m")
        or analyses.get("1h")
        or get_reference_analysis(
            analyses
        )
    )

    price = price_reference.price

    trade_plan = create_trade_plan(
        direction=direction,
        price=price,
        analyses=analyses,
    )

    trade_plan_warnings = (
        validate_trade_plan(
            trade_plan
        )
    )

    supporting_reasons = (
        collect_supporting_reasons(
            analyses=analyses,
            overall_score=overall_score,
        )
    )

    warnings: list[str] = []

    for warning in (
        confirmation_warnings
        + trade_plan_warnings
    ):
        if warning not in warnings:
            warnings.append(
                warning
            )

    # Add data-quality warnings.

    if combined_errors:
        unavailable = ", ".join(
            sorted(
                combined_errors.keys()
            )
        )

        warnings.append(
            "Some timeframes were unavailable: "
            f"{unavailable}."
        )

    # Do not create trade levels while the model says WAIT.

    if direction == "WAIT":
        trade_plan = None

    # Reduce confidence when timeframes disagree heavily.

    directional_total = (
        bullish_timeframes
        + bearish_timeframes
    )

    if (
        directional_total > 0
        and bullish_timeframes > 0
        and bearish_timeframes > 0
    ):
        disagreement_ratio = (
            min(
                bullish_timeframes,
                bearish_timeframes,
            )
            / directional_total
        )

        confidence_reduction = int(
            disagreement_ratio * 20
        )

        confidence = max(
            0,
            confidence
            - confidence_reduction,
        )

        warnings.append(
            "Timeframes are directionally mixed, "
            "so confidence was reduced."
        )

    # A confirmed signal should have meaningful reasons.

    if (
        confirmed
        and not supporting_reasons
    ):
        warnings.append(
            "The setup passed numeric confirmation "
            "but has limited descriptive confluence."
        )

    unique_warnings: list[str] = []

    for warning in warnings:
        if warning not in unique_warnings:
            unique_warnings.append(
                warning
            )

    return MarketSignal(
        symbol=symbol,
        direction=direction,
        stage=stage,
        score=overall_score,
        confidence=confidence,
        price=price,
        bullish_timeframes=(
            bullish_timeframes
        ),
        bearish_timeframes=(
            bearish_timeframes
        ),
        neutral_timeframes=(
            neutral_timeframes
        ),
        analyses=analyses,
        errors=combined_errors,
        trade_plan=trade_plan,
        supporting_reasons=(
            supporting_reasons
        ),
        warnings=unique_warnings[:8],
    )


# =========================================================
# SIGNAL SUMMARY HELPERS
# =========================================================

def get_signal_grade(
    signal: MarketSignal,
) -> str:
    confidence = signal.confidence

    if confidence >= 95:
        return "A+"

    if confidence >= 90:
        return "A"

    if confidence >= 80:
        return "B"

    if confidence >= 70:
        return "C"

    return "D"


def get_readiness_label(
    signal: MarketSignal,
) -> str:
    confidence = signal.confidence
    profile = active_profile()

    if confidence >= 95:
        return "EXCEPTIONAL"

    if confidence >= profile.strong_threshold:
        return "HIGH QUALITY"

    if confidence >= profile.confirmed_threshold:
        return "NEAR TRIGGER"

    if confidence >= profile.watch_threshold * 0.80:
        return "BUILDING"

    return "STAND ASIDE"


def should_send_watch_alert(
    signal: MarketSignal,
) -> bool:
    return (
        signal.stage == "WATCH"
        and signal.direction != "WAIT"
        and signal.confidence >= 50
    )


def should_send_confirmed_alert(
    signal: MarketSignal,
) -> bool:
    return (
        signal.stage
        in {
            "CONFIRMED",
            "STRONG",
        }
        and signal.direction != "WAIT"
        and signal.trade_plan is not None
    )
