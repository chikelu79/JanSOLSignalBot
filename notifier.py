import logging
import time
from dataclasses import dataclass
from typing import Any

from strategy import (
    MarketSignal,
    TradePlan,
    get_readiness_label,
    get_signal_grade,
)


logger = logging.getLogger(__name__)


# =========================================================
# ALERT SETTINGS
# =========================================================

DEFAULT_ALERT_COOLDOWN_SECONDS = 30 * 60

WATCH_ALERT_COOLDOWN_SECONDS = 20 * 60

CONFIRMED_ALERT_COOLDOWN_SECONDS = 45 * 60

INVALIDATION_ALERT_COOLDOWN_SECONDS = 10 * 60

SCORE_CHANGE_ALERT_COOLDOWN_SECONDS = 15 * 60

MINIMUM_MEANINGFUL_SCORE_CHANGE = 12.0

RAPID_SCORE_CHANGE = 22.0


# =========================================================
# RUNTIME ALERT STATE
# =========================================================

last_alert_times: dict[str, float] = {}

previous_signals: dict[str, dict[str, Any]] = {}

active_setups: dict[str, dict[str, Any]] = {}


# =========================================================
# ALERT DECISION MODEL
# =========================================================

@dataclass
class AlertDecision:
    should_send: bool

    alert_type: str
    symbol: str
    side: str

    reason: str
    message: str


# =========================================================
# BASIC HELPERS
# =========================================================

def valid_number(
    value: Any,
) -> bool:
    try:
        number = float(value)

        return number == number

    except (
        TypeError,
        ValueError,
    ):
        return False


def price_text(
    value: Any,
) -> str:
    if not valid_number(
        value
    ):
        return "N/A"

    number = float(
        value
    )

    if number >= 1000:
        return f"${number:,.2f}"

    if number >= 1:
        return f"${number:,.4f}"

    if number >= 0.01:
        return f"${number:,.6f}"

    return f"${number:,.8f}"


def number_text(
    value: Any,
    decimals: int = 2,
) -> str:
    if not valid_number(
        value
    ):
        return "N/A"

    return f"{float(value):,.{decimals}f}"


def percentage_text(
    value: Any,
) -> str:
    if not valid_number(
        value
    ):
        return "N/A"

    return f"{float(value):+.2f}%"


def direction_emoji(
    direction: str,
) -> str:
    values = {
        "STRONG LONG": "🚀",
        "LONG": "🟢",
        "WAIT": "🟡",
        "SHORT": "🔴",
        "STRONG SHORT": "🔻",
    }

    return values.get(
        direction,
        "⚪",
    )


def stage_emoji(
    stage: str,
) -> str:
    values = {
        "NEUTRAL": "⚪",
        "WATCH": "👀",
        "CONFIRMED": "🚨",
        "STRONG": "💎",
        "INVALIDATED": "❌",
        "RAPID CHANGE": "⚡",
    }

    return values.get(
        stage,
        "📡",
    )


def side_from_direction(
    direction: str,
) -> str:
    if "LONG" in direction:
        return "LONG"

    if "SHORT" in direction:
        return "SHORT"

    return "WAIT"


def unique_lines(
    values: list[str],
    maximum: int,
) -> list[str]:
    unique: list[str] = []

    for value in values:
        clean = str(
            value
        ).strip()

        if (
            clean
            and clean not in unique
        ):
            unique.append(
                clean
            )

        if len(unique) >= maximum:
            break

    return unique


# =========================================================
# COOLDOWN MANAGEMENT
# =========================================================

def alert_key(
    symbol: str,
    alert_type: str,
    side: str = "",
) -> str:
    return (
        f"{symbol.upper()}:"
        f"{alert_type.upper()}:"
        f"{side.upper()}"
    )


def cooldown_remaining(
    key: str,
    cooldown_seconds: int,
) -> int:
    previous_time = last_alert_times.get(
        key,
        0.0,
    )

    elapsed = (
        time.time()
        - previous_time
    )

    remaining = (
        cooldown_seconds
        - elapsed
    )

    return max(
        0,
        int(remaining),
    )


def alert_is_allowed(
    key: str,
    cooldown_seconds: int,
) -> bool:
    return cooldown_remaining(
        key,
        cooldown_seconds,
    ) <= 0


def mark_alert_sent(
    key: str,
) -> None:
    last_alert_times[key] = (
        time.time()
    )


# =========================================================
# TRADE PLAN FORMATTER
# =========================================================

def format_trade_plan(
    trade_plan: TradePlan | None,
) -> list[str]:
    if trade_plan is None:
        return [
            "No trade plan generated.",
        ]

    return [
        f"Side: {trade_plan.side}",
        (
            "Entry zone: "
            f"{price_text(trade_plan.entry_low)} "
            "to "
            f"{price_text(trade_plan.entry_high)}"
        ),
        (
            "Stop / invalidation: "
            f"{price_text(trade_plan.stop_loss)}"
        ),
        (
            f"TP1: {price_text(trade_plan.tp1)} "
            f"({trade_plan.reward_risk_tp1:.2f}R)"
        ),
        (
            f"TP2: {price_text(trade_plan.tp2)} "
            f"({trade_plan.reward_risk_tp2:.2f}R)"
        ),
        (
            f"TP3: {price_text(trade_plan.tp3)} "
            f"({trade_plan.reward_risk_tp3:.2f}R)"
        ),
    ]


# =========================================================
# TIMEFRAME FORMATTER
# =========================================================

def format_timeframes(
    signal: MarketSignal,
) -> list[str]:
    lines: list[str] = []

    order = [
        "5m",
        "15m",
        "1h",
        "4h",
        "8h",
        "1d",
    ]

    for interval in order:
        analysis = signal.analyses.get(
            interval
        )

        if analysis is None:
            lines.append(
                f"⚠️ {interval}: unavailable"
            )

            continue

        lines.append(
            f"{direction_emoji(analysis.direction)} "
            f"{interval}: "
            f"{analysis.direction} "
            f"({analysis.score:+.0f})"
        )

    return lines


# =========================================================
# MACRO CONTEXT FORMATTER
# =========================================================

def format_market_context(
    context: Any | None,
) -> list[str]:
    if context is None:
        return [
            "Macro context: unavailable",
        ]

    lines = [
        (
            "BTC: "
            f"{getattr(context, 'btc_direction', 'UNKNOWN')} "
            f"({getattr(context, 'btc_score', 0):+.1f})"
        ),
        (
            "ETH: "
            f"{getattr(context, 'eth_direction', 'UNKNOWN')} "
            f"({getattr(context, 'eth_score', 0):+.1f})"
        ),
        (
            "BTC correlation: "
            f"{getattr(context, 'btc_correlation', 0):.2f} "
            f"({getattr(context, 'correlation_strength', 'UNKNOWN')})"
        ),
        (
            "BTC dominance: "
            f"{getattr(context, 'btc_dominance', 0):.2f}% "
            f"({getattr(context, 'btc_dominance_effect', 'UNKNOWN')})"
        ),
        (
            "Crypto market 24h: "
            f"{getattr(context, 'crypto_market_change_24h', 0):+.2f}%"
        ),
        (
            "VIX: "
            f"{getattr(context, 'vix_value', 0):.2f} "
            f"({getattr(context, 'vix_regime', 'UNKNOWN')})"
        ),
        (
            "Macro adjustment: "
            f"{getattr(context, 'score_adjustment', 0):+.1f}"
        ),
    ]

    return lines


# =========================================================
# MANUAL SCAN MESSAGE
# =========================================================

def build_scan_message(
    signal: MarketSignal,
    context: Any | None = None,
) -> str:
    adjusted_score = (
        getattr(
            context,
            "adjusted_score",
            signal.score,
        )
        if context is not None
        else signal.score
    )

    adjusted_confidence_value = min(
        95,
        int(
            abs(
                adjusted_score
            )
        ),
    )

    grade = get_signal_grade(
        signal
    )

    readiness = get_readiness_label(
        signal
    )

    lines = [
        f"📡 {signal.symbol} MARKET SCAN",
        "",
        f"Price: {price_text(signal.price)}",
        (
            f"{direction_emoji(signal.direction)} "
            f"Technical direction: "
            f"{signal.direction}"
        ),
        f"Technical score: {signal.score:+.1f}",
        f"Context-adjusted score: {adjusted_score:+.1f}",
        f"Confidence: {adjusted_confidence_value}%",
        f"Grade: {grade}",
        f"Readiness: {readiness}",
        f"Stage: {signal.stage}",
        "",
        "TIMEFRAMES",
        *format_timeframes(
            signal
        ),
        "",
        "MARKET CONTEXT",
        *format_market_context(
            context
        ),
    ]

    if signal.trade_plan is not None:
        lines.extend(
            [
                "",
                "TRADE MAP",
                *format_trade_plan(
                    signal.trade_plan
                ),
            ]
        )

    reasons = list(
        signal.supporting_reasons
    )

    if context is not None:
        reasons.extend(
            list(
                getattr(
                    context,
                    "reasons",
                    [],
                )
            )
        )

    reasons = unique_lines(
        reasons,
        8,
    )

    if reasons:
        lines.extend(
            [
                "",
                "WHY THE MODEL LEANS THIS WAY",
                *[
                    f"• {reason}"
                    for reason in reasons
                ],
            ]
        )

    warnings = list(
        signal.warnings
    )

    if context is not None:
        warnings.extend(
            list(
                getattr(
                    context,
                    "warnings",
                    [],
                )
            )
        )

    warnings = unique_lines(
        warnings,
        6,
    )

    if warnings:
        lines.extend(
            [
                "",
                "RISKS",
                *[
                    f"• {warning}"
                    for warning in warnings
                ],
            ]
        )

    lines.extend(
        [
            "",
            "Analysis only. Use controlled risk and "
            "confirm the setup before entering.",
        ]
    )

    return "\n".join(
        lines
    )


# =========================================================
# WATCH ALERT
# =========================================================

def build_watch_alert(
    signal: MarketSignal,
    context: Any | None = None,
) -> str:
    side = side_from_direction(
        signal.direction
    )

    adjusted_score = getattr(
        context,
        "adjusted_score",
        signal.score,
    )

    lines = [
        f"👀 {signal.symbol} {side} SETUP BUILDING",
        "",
        f"Price: {price_text(signal.price)}",
        f"Technical score: {signal.score:+.1f}",
        f"Adjusted score: {adjusted_score:+.1f}",
        f"Confidence: {signal.confidence}%",
        f"Grade: {get_signal_grade(signal)}",
        "",
        "The setup is developing but is not fully confirmed.",
    ]

    if signal.trade_plan is not None:
        lines.extend(
            [
                "",
                *format_trade_plan(
                    signal.trade_plan
                ),
            ]
        )

    reasons = unique_lines(
        signal.supporting_reasons,
        5,
    )

    if context is not None:
