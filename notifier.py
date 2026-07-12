import hashlib
import time
from dataclasses import dataclass
from typing import Any

from strategy import (
    MarketSignal,
    get_readiness_label,
    get_signal_grade,
)


# =========================================================
# ALERT SETTINGS
# =========================================================

WATCH_COOLDOWN_SECONDS = 20 * 60
CONFIRMED_COOLDOWN_SECONDS = 45 * 60
RAPID_CHANGE_COOLDOWN_SECONDS = 15 * 60
INVALIDATION_COOLDOWN_SECONDS = 10 * 60

RAPID_SCORE_CHANGE = 22.0


# =========================================================
# RUNTIME MEMORY
# =========================================================

last_alert_times: dict[str, float] = {}
last_signal_hashes: dict[str, str] = {}
previous_scores: dict[str, float] = {}
active_setups: dict[str, dict[str, Any]] = {}


# =========================================================
# RESULT MODEL
# =========================================================

@dataclass
class AlertDecision:
    should_send: bool
    alert_type: str
    symbol: str
    message: str
    reason: str


# =========================================================
# FORMAT HELPERS
# =========================================================

def valid_number(value: Any) -> bool:
    try:
        number = float(value)
        return number == number
    except (TypeError, ValueError):
        return False


def price_text(value: Any) -> str:
    if not valid_number(value):
        return "N/A"

    number = float(value)

    if number >= 1000:
        return f"${number:,.2f}"

    if number >= 1:
        return f"${number:,.4f}"

    if number >= 0.01:
        return f"${number:,.6f}"

    return f"${number:,.8f}"


def direction_emoji(direction: str) -> str:
    emojis = {
        "STRONG LONG": "🚀",
        "LONG": "🟢",
        "WAIT": "🟡",
        "SHORT": "🔴",
        "STRONG SHORT": "🔻",
    }

    return emojis.get(direction, "⚪")


def side_from_direction(direction: str) -> str:
    if "LONG" in direction:
        return "LONG"

    if "SHORT" in direction:
        return "SHORT"

    return "WAIT"


def unique_items(
    items: list[str],
    maximum: int,
) -> list[str]:
    result: list[str] = []

    for item in items:
        cleaned = str(item).strip()

        if cleaned and cleaned not in result:
            result.append(cleaned)

        if len(result) >= maximum:
            break

    return result


# =========================================================
# COOLDOWN HELPERS
# =========================================================

def make_alert_key(
    symbol: str,
    alert_type: str,
    side: str = "",
) -> str:
    return (
        f"{symbol.upper()}:"
        f"{alert_type.upper()}:"
        f"{side.upper()}"
    )


def alert_allowed(
    key: str,
    cooldown_seconds: int,
) -> bool:
    previous_time = last_alert_times.get(
        key,
        0.0,
    )

    return (
        time.time() - previous_time
        >= cooldown_seconds
    )


def mark_alert_sent(key: str) -> None:
    last_alert_times[key] = time.time()


# =========================================================
# SIGNAL HASH
# =========================================================

def signal_hash(signal: MarketSignal) -> str:
    trade_plan = signal.trade_plan

    parts = [
        signal.symbol,
        signal.direction,
        signal.stage,
        f"{signal.score:.1f}",
        str(signal.confidence),
    ]

    if trade_plan is not None:
        parts.extend(
            [
                f"{trade_plan.entry_low:.8f}",
                f"{trade_plan.entry_high:.8f}",
                f"{trade_plan.stop_loss:.8f}",
                f"{trade_plan.tp1:.8f}",
                f"{trade_plan.tp2:.8f}",
                f"{trade_plan.tp3:.8f}",
            ]
        )

    payload = "|".join(parts)

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


# =========================================================
# TIMEFRAME FORMATTER
# =========================================================

def format_timeframes(
    signal: MarketSignal,
) -> list[str]:
    lines: list[str] = []

    for interval in [
        "5m",
        "15m",
        "1h",
        "4h",
        "8h",
        "1d",
    ]:
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
            f"{interval}: {analysis.direction} "
            f"({analysis.score:+.0f})"
        )

    return lines


# =========================================================
# TRADE PLAN FORMATTER
# =========================================================

def format_trade_plan(
    signal: MarketSignal,
) -> list[str]:
    plan = signal.trade_plan

    if plan is None:
        return [
            "No trade plan generated.",
        ]

    return [
        f"Side: {plan.side}",
        (
            f"Entry: {price_text(plan.entry_low)} "
            f"to {price_text(plan.entry_high)}"
        ),
        (
            f"Stop / invalidation: "
            f"{price_text(plan.stop_loss)}"
        ),
        (
            f"TP1: {price_text(plan.tp1)} "
            f"({plan.reward_risk_tp1:.2f}R)"
        ),
        (
            f"TP2: {price_text(plan.tp2)} "
            f"({plan.reward_risk_tp2:.2f}R)"
        ),
        (
            f"TP3: {price_text(plan.tp3)} "
            f"({plan.reward_risk_tp3:.2f}R)"
        ),
    ]


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

    return [
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
            f"{getattr(context, 'btc_correlation', 0):.2f}"
        ),
        (
            "BTC dominance: "
            f"{getattr(context, 'btc_dominance', 0):.2f}%"
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
            "Context adjustment: "
            f"{getattr(context, 'score_adjustment', 0):+.1f}"
        ),
    ]


# =========================================================
# FULL MANUAL SCAN MESSAGE
# =========================================================

def build_scan_message(
    signal: MarketSignal,
    context: Any | None = None,
) -> str:
    adjusted_score = getattr(
        context,
        "adjusted_score",
        signal.score,
    )

    adjusted_confidence = min(
        95,
        int(abs(adjusted_score)),
    )

    reasons = list(
        signal.supporting_reasons
    )

    warnings = list(
        signal.warnings
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

        warnings.extend(
            list(
                getattr(
                    context,
                    "warnings",
                    [],
                )
            )
        )

    lines = [
        f"📡 {signal.symbol} MARKET SCAN",
        "",
        f"Price: {price_text(signal.price)}",
        (
            f"{direction_emoji(signal.direction)} "
            f"Direction: {signal.direction}"
        ),
        f"Technical score: {signal.score:+.1f}",
        f"Adjusted score: {adjusted_score:+.1f}",
        f"Confidence: {adjusted_confidence}%",
        f"Grade: {get_signal_grade(signal)}",
        f"Readiness: {get_readiness_label(signal)}",
        f"Stage: {signal.stage}",
        "",
        "TIMEFRAMES",
        *format_timeframes(signal),
        "",
        "MARKET CONTEXT",
        *format_market_context(context),
    ]

    if signal.trade_plan is not None:
        lines.extend(
            [
                "",
                "TRADE MAP",
                *format_trade_plan(signal),
            ]
        )

    clean_reasons = unique_items(
        reasons,
        8,
    )

    if clean_reasons:
        lines.extend(
            [
                "",
                "WHY",
                *[
                    f"• {reason}"
                    for reason in clean_reasons
                ],
            ]
        )

    clean_warnings = unique_items(
        warnings,
        6,
    )

    if clean_warnings:
        lines.extend(
            [
                "",
                "RISKS",
                *[
                    f"• {warning}"
                    for warning in clean_warnings
                ],
            ]
        )

    lines.extend(
        [
            "",
            "Analysis only. Confirm the setup and "
            "use controlled risk.",
        ]
    )

    return "\n".join(lines)


# =========================================================
# ALERT MESSAGE
# =========================================================

def build_alert_message(
    signal: MarketSignal,
    alert_type: str,
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

    headings = {
        "WATCH": f"👀 {signal.symbol} {side} SETUP BUILDING",
        "CONFIRMED": f"🚨 {signal.symbol} {side} CONFIRMED",
        "STRONG": f"💎 {signal.symbol} STRONG {side} SETUP",
        "RAPID_CHANGE": f"⚡ {signal.symbol} RAPID MARKET CHANGE",
        "INVALIDATED": f"❌ {signal.symbol} SETUP INVALIDATED",
    }

    lines = [
        headings.get(
            alert_type,
            f"📡 {signal.symbol} MARKET ALERT",
        ),
        "",
        f"Price: {price_text(signal.price)}",
        f"Technical score: {signal.score:+.1f}",
        f"Adjusted score: {adjusted_score:+.1f}",
        f"Confidence: {signal.confidence}%",
        f"Grade: {get_signal_grade(signal)}",
        f"Stage: {signal.stage}",
    ]

    if (
        alert_type != "INVALIDATED"
        and signal.trade_plan is not None
    ):
        lines.extend(
            [
                "",
                "TRADE MAP",
                *format_trade_plan(signal),
            ]
        )

    reasons = unique_items(
        signal.supporting_reasons,
        5,
    )

    if context is not None:
        reasons = unique_items(
            reasons
            + list(
                getattr(
                    context,
                    "reasons",
                    [],
                )
            ),
            6,
        )

    if reasons:
        lines.extend(
            [
                "",
                "CONFLUENCE",
                *[
                    f"• {reason}"
                    for reason in reasons
                ],
            ]
        )

    warnings = unique_items(
        signal.warnings,
        4,
    )

    if context is not None:
        warnings = unique_items(
            warnings
            + list(
                getattr(
                    context,
                    "warnings",
                    [],
                )
            ),
            5,
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

    return "\n".join(lines)


# =========================================================
# SETUP INVALIDATION
# =========================================================

def setup_is_invalidated(
    signal: MarketSignal,
) -> bool:
    setup = active_setups.get(
        signal.symbol
    )

    if setup is None:
        return False

    side = setup.get("side")

    invalidation = float(
        setup.get(
            "invalidation",
            0,
        )
    )

    if side == "LONG":
        return (
            signal.price <= invalidation
            or signal.score < 20
        )

    if side == "SHORT":
        return (
            signal.price >= invalidation
            or signal.score > -20
        )

    return False


# =========================================================
# ALERT EVALUATION
# =========================================================

def evaluate_signal_alert(
    signal: MarketSignal,
    context: Any | None = None,
) -> AlertDecision:
    symbol = signal.symbol.upper()

    current_score = float(
        getattr(
            context,
            "adjusted_score",
            signal.score,
        )
    )

    previous_score = previous_scores.get(
        symbol
    )

    previous_scores[symbol] = current_score

    current_hash = signal_hash(
        signal
    )

    previous_hash = last_signal_hashes.get(
        symbol
    )

    last_signal_hashes[symbol] = (
        current_hash
    )

    side = side_from_direction(
        signal.direction
    )

    if setup_is_invalidated(signal):
        key = make_alert_key(
            symbol,
            "INVALIDATED",
            active_setups[
                symbol
            ].get(
                "side",
                "",
            ),
        )

        if alert_allowed(
            key,
            INVALIDATION_COOLDOWN_SECONDS,
        ):
            mark_alert_sent(key)

            active_setups.pop(
                symbol,
                None,
            )

            return AlertDecision(
                should_send=True,
                alert_type="INVALIDATED",
                symbol=symbol,
                message=build_alert_message(
                    signal,
                    "INVALIDATED",
                    context,
                ),
                reason="Active setup invalidated",
            )

    if (
        previous_score is not None
        and abs(
            current_score
            - previous_score
        ) >= RAPID_SCORE_CHANGE
    ):
        key = make_alert_key(
            symbol,
            "RAPID_CHANGE",
            side,
        )

        if alert_allowed(
            key,
            RAPID_CHANGE_COOLDOWN_SECONDS,
        ):
            mark_alert_sent(key)

            return AlertDecision(
                should_send=True,
                alert_type="RAPID_CHANGE",
                symbol=symbol,
                message=build_alert_message(
                    signal,
                    "RAPID_CHANGE",
                    context,
                ),
                reason="Rapid score change",
            )

    if (
        signal.stage in {
            "CONFIRMED",
            "STRONG",
        }
        and signal.trade_plan is not None
    ):
        alert_type = (
            "STRONG"
            if signal.stage == "STRONG"
            else "CONFIRMED"
        )

        key = make_alert_key(
            symbol,
            alert_type,
            side,
        )

        duplicate = (
            previous_hash
            == current_hash
        )

        if (
            not duplicate
            and alert_allowed(
                key,
                CONFIRMED_COOLDOWN_SECONDS,
            )
        ):
            mark_alert_sent(key)

            active_setups[symbol] = {
                "side": side,
                "invalidation": (
                    signal.trade_plan.invalidation
                ),
                "created_at": time.time(),
            }

            return AlertDecision(
                should_send=True,
                alert_type=alert_type,
                symbol=symbol,
                message=build_alert_message(
                    signal,
                    alert_type,
                    context,
                ),
                reason="Confirmed setup",
            )

    if (
        signal.stage == "WATCH"
        and signal.direction != "WAIT"
    ):
        key = make_alert_key(
            symbol,
            "WATCH",
            side,
        )

        duplicate = (
            previous_hash
            == current_hash
        )

        if (
            not duplicate
            and alert_allowed(
                key,
                WATCH_COOLDOWN_SECONDS,
            )
        ):
            mark_alert_sent(key)

            return AlertDecision(
                should_send=True,
                alert_type="WATCH",
                symbol=symbol,
                message=build_alert_message(
                    signal,
                    "WATCH",
                    context,
                ),
                reason="Developing setup",
            )

    return AlertDecision(
        should_send=False,
        alert_type="NONE",
        symbol=symbol,
        message="",
        reason="No new alert condition",
    )
