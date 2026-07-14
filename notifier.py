from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any

from bot_state import get_active_setups, remove_active_setup, set_active_setup
from session_context import get_session_context
from strategy import MarketSignal, TradePlan, get_readiness_label, get_signal_grade

WATCH_COOLDOWN_SECONDS = 20 * 60
PREPARE_COOLDOWN_SECONDS = 10 * 60
ENTRY_COOLDOWN_SECONDS = 45 * 60
MANAGEMENT_COOLDOWN_SECONDS = 10 * 60
DO_NOT_CHASE_COOLDOWN_SECONDS = 20 * 60
RAPID_CHANGE_COOLDOWN_SECONDS = 15 * 60
DERIVATIVES_ALERT_COOLDOWN_SECONDS = 30 * 60
DERIVATIVES_EXIT_COOLDOWN_SECONDS = 15 * 60
RAPID_SCORE_CHANGE = 22.0

last_alert_times: dict[str, float] = {}
last_signal_hashes: dict[str, str] = {}
previous_scores: dict[str, float] = {}


def _load_setup_states() -> dict[str, dict[str, Any]]:
    restored: dict[str, dict[str, Any]] = {}
    for symbol, state in get_active_setups().items():
        try:
            restored[symbol] = {
                **state,
                "plan": TradePlan(**state["plan"]),
            }
        except (KeyError, TypeError, ValueError):
            remove_active_setup(symbol)
    return restored


setup_states: dict[str, dict[str, Any]] = _load_setup_states()


def _persist_setup(symbol: str, state: dict[str, Any]) -> None:
    serializable = dict(state)
    serializable["plan"] = asdict(state["plan"])
    set_active_setup(symbol, serializable)


def _clear_setup(symbol: str) -> None:
    setup_states.pop(symbol, None)
    remove_active_setup(symbol)


@dataclass
class AlertDecision:
    should_send: bool
    alert_type: str
    symbol: str
    message: str
    reason: str


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
    return {
        "STRONG LONG": "🚀",
        "LONG": "🟢",
        "WAIT": "🟡",
        "SHORT": "🔴",
        "STRONG SHORT": "🔻",
    }.get(direction, "⚪")


def side_from_direction(direction: str) -> str:
    if "LONG" in direction:
        return "LONG"
    if "SHORT" in direction:
        return "SHORT"
    return "WAIT"


def unique_items(items: list[str], maximum: int) -> list[str]:
    result: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= maximum:
            break
    return result


def make_alert_key(symbol: str, alert_type: str, side: str = "") -> str:
    return f"{symbol.upper()}:{alert_type.upper()}:{side.upper()}"


def alert_allowed(key: str, cooldown_seconds: int) -> bool:
    return time.time() - last_alert_times.get(key, 0.0) >= cooldown_seconds


def mark_alert_sent(key: str) -> None:
    last_alert_times[key] = time.time()


def signal_hash(signal: MarketSignal) -> str:
    plan = signal.trade_plan
    values = [
        signal.symbol,
        signal.direction,
        signal.stage,
        f"{signal.score:.1f}",
        str(signal.confidence),
    ]
    if plan:
        values.extend(
            f"{value:.8f}"
            for value in (
                plan.entry_low,
                plan.entry_high,
                plan.stop_loss,
                plan.tp1,
                plan.tp2,
                plan.tp3,
            )
        )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def format_timeframes(signal: MarketSignal) -> list[str]:
    lines: list[str] = []
    for interval in ("5m", "15m", "1h", "4h", "8h", "1d"):
        analysis = signal.analyses.get(interval)
        if analysis is None:
            lines.append(f"⚠️ {interval}: unavailable")
        else:
            lines.append(
                f"{direction_emoji(analysis.direction)} {interval}: "
                f"{analysis.direction} ({analysis.score:+.0f})"
            )
    return lines


def format_trade_plan(plan: TradePlan | None) -> list[str]:
    if plan is None:
        return ["No trade plan generated."]
    return [
        f"Side: {plan.side}",
        f"Entry zone: {price_text(plan.entry_low)} to {price_text(plan.entry_high)}",
        f"Stop / invalidation: {price_text(plan.stop_loss)}",
        f"TP1: {price_text(plan.tp1)} ({plan.reward_risk_tp1:.2f}R)",
        f"TP2: {price_text(plan.tp2)} ({plan.reward_risk_tp2:.2f}R)",
        f"TP3: {price_text(plan.tp3)} ({plan.reward_risk_tp3:.2f}R)",
    ]


def format_market_context(context: Any | None) -> list[str]:
    if context is None:
        return ["Macro context: unavailable"]
    fear_live = bool(getattr(context, "fear_greed_live", False))
    fear_suffix = "LIVE" if fear_live else "FALLBACK"
    return [
        f"BTC: {getattr(context, 'btc_direction', 'UNKNOWN')} "
        f"({getattr(context, 'btc_score', 0):+.1f})",
        f"ETH: {getattr(context, 'eth_direction', 'UNKNOWN')} "
        f"({getattr(context, 'eth_score', 0):+.1f})",
        f"BTC correlation: {getattr(context, 'btc_correlation', 0):.2f}",
        f"BTC dominance: {getattr(context, 'btc_dominance', 0):.2f}%",
        f"Crypto market 24h: {getattr(context, 'crypto_market_change_24h', 0):+.2f}%",
        f"VIX: {getattr(context, 'vix_value', 0):.2f} "
        f"({getattr(context, 'vix_regime', 'UNKNOWN')})",
        f"Fear & Greed: {getattr(context, 'fear_greed_value', 50):.0f} "
        f"({getattr(context, 'fear_greed_label', 'NEUTRAL')}, {fear_suffix})",
        f"Funding: {getattr(context, 'funding_rate', 0.0) * 100:+.4f}% "
        f"({getattr(context, 'funding_label', 'UNAVAILABLE')}, "
        f"{getattr(context, 'derivatives_provider', 'UNKNOWN')})",
        f"Open interest: ${getattr(context, 'open_interest_value', 0.0):,.0f}",
        f"OI change: {getattr(context, 'open_interest_change_5m', 0.0):+.2f}% (5m), "
        f"{getattr(context, 'open_interest_change_1h', 0.0):+.2f}% (1h)",
        f"Macro bias: {getattr(context, 'macro_bias', 'NEUTRAL')} "
        f"({getattr(context, 'macro_score', 0):+.1f})",
        f"Context adjustment: {getattr(context, 'score_adjustment', 0):+.1f}",
    ]


def _weighted_average(values: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in values) / total_weight


def _bias_label(score: float) -> str:
    if score >= 18:
        return "BULLISH"
    if score <= -18:
        return "BEARISH"
    return "NEUTRAL"


def build_confidence_breakdown(
    signal: MarketSignal,
    context: Any | None = None,
) -> list[str]:
    """Explain signal quality without changing the trading decision."""
    timeframe_weights = {
        "5m": 0.08,
        "15m": 0.14,
        "1h": 0.20,
        "4h": 0.23,
        "8h": 0.17,
        "1d": 0.18,
    }
    trend_values: list[tuple[float, float]] = []
    momentum_values: list[tuple[float, float]] = []
    liquidity_values: list[tuple[float, float]] = []
    volume_values: list[tuple[float, float]] = []

    for interval, analysis in signal.analyses.items():
        weight = timeframe_weights.get(interval, 0.10)
        trend_checks = (
            analysis.price > analysis.ema20,
            analysis.ema20 > analysis.ema50,
            analysis.ema50 > analysis.ema100,
            analysis.ema100 > analysis.ema200,
            analysis.price > analysis.vwap,
            analysis.supertrend_direction > 0,
        )
        trend_score = (sum(trend_checks) / len(trend_checks) * 200.0) - 100.0
        trend_values.append((trend_score, weight))

        momentum_parts = [
            max(-100.0, min(100.0, (analysis.rsi - 50.0) * 4.0)),
            45.0 if analysis.macd > analysis.macd_signal else -45.0,
            max(-100.0, min(100.0, analysis.roc * 12.0)),
            35.0 if analysis.stoch_rsi_k > analysis.stoch_rsi_d else -35.0,
        ]
        momentum_values.append((sum(momentum_parts) / len(momentum_parts), weight))

        range_width = analysis.resistance - analysis.support
        if analysis.breakout_up:
            liquidity_score = 100.0
        elif analysis.breakout_down:
            liquidity_score = -100.0
        elif range_width > 0:
            range_position = (analysis.price - analysis.support) / range_width
            liquidity_score = max(-100.0, min(100.0, (range_position - 0.5) * 200.0))
        else:
            liquidity_score = 0.0
        liquidity_values.append((liquidity_score, weight))

        volume_activity = max(0.0, min(100.0, analysis.relative_volume / 1.5 * 100.0))
        volume_values.append((volume_activity, weight))

    trend = _weighted_average(trend_values)
    momentum = _weighted_average(momentum_values)
    liquidity = _weighted_average(liquidity_values)
    volume = _weighted_average(volume_values)
    macro = float(getattr(context, "macro_score", 0.0))
    macro = max(-100.0, min(100.0, macro / 30.0 * 100.0))

    signed_scores = [
        (analysis.score, timeframe_weights.get(interval, 0.10))
        for interval, analysis in signal.analyses.items()
    ]
    weighted_net = sum(score * weight for score, weight in signed_scores)
    weighted_strength = sum(abs(score) * weight for score, weight in signed_scores)
    alignment = (
        abs(weighted_net) / weighted_strength * 100.0
        if weighted_strength > 0
        else 0.0
    )

    risk_line = "Risk: N/A — no active setup"
    if signal.trade_plan is not None:
        plan = signal.trade_plan
        reward_quality = max(
            0.0,
            min(100.0, plan.reward_risk_tp2 / 3.0 * 100.0),
        )
        warning_quality = max(0.0, 100.0 - len(signal.warnings) * 12.0)
        volume_quality = volume
        risk_quality = (
            alignment * 0.35
            + reward_quality * 0.30
            + warning_quality * 0.20
            + volume_quality * 0.15
        )
        risk_label = (
            "LOW"
            if risk_quality >= 75
            else "MEDIUM"
            if risk_quality >= 50
            else "HIGH"
        )
        risk_line = f"Risk: {risk_label} ({risk_quality:.0f}/100 setup quality)"

    return [
        f"Trend: {abs(trend):.0f}% {_bias_label(trend)}",
        f"Momentum: {abs(momentum):.0f}% {_bias_label(momentum)}",
        f"Macro: {abs(macro):.0f}% {_bias_label(macro)}",
        f"Liquidity position: {abs(liquidity):.0f}% {_bias_label(liquidity)}",
        f"Volume activity: {volume:.0f}%",
        f"Timeframe alignment: {alignment:.0f}%",
        risk_line,
    ]


def build_active_setups_message() -> str:
    active_setups = get_active_setups()
    if not active_setups:
        return (
            "📋 ACTIVE MANAGED SETUPS\n\n"
            "None. The bot is waiting for a confirmed ENTRY.\n\n"
            "These are signal-management records, not exchange positions."
        )

    lines = ["📋 ACTIVE MANAGED SETUPS"]
    for symbol, state in sorted(active_setups.items()):
        plan = state.get("plan", {})
        progress: list[str] = []
        if state.get("tp1"):
            progress.append("TP1 reached")
        if state.get("tp2"):
            progress.append("TP2 reached")
        if state.get("breakeven"):
            progress.append("breakeven protection prompted")
        lines.extend(
            [
                "",
                f"{symbol} — {state.get('side', 'UNKNOWN')}",
                f"Entry: {price_text(plan.get('entry_low'))} to {price_text(plan.get('entry_high'))}",
                f"Stop: {price_text(plan.get('stop_loss'))}",
                f"TP1: {price_text(plan.get('tp1'))}",
                f"TP2: {price_text(plan.get('tp2'))}",
                f"TP3: {price_text(plan.get('tp3'))}",
                f"Progress: {', '.join(progress) if progress else 'Entry active; no milestone recorded'}",
            ]
        )
    lines.extend(
        [
            "",
            "Signal-management records only; verify actual positions on your exchange.",
        ]
    )
    return "\n".join(lines)


def execution_status(signal: MarketSignal) -> tuple[str, str]:
    plan = signal.trade_plan
    if plan is None:
        return "NO SETUP", "No entry zone exists while direction remains WAIT."

    price = signal.price
    width = max(plan.entry_high - plan.entry_low, plan.risk_per_unit * 0.10)
    approach_buffer = max(width * 3.0, price * 0.01)

    if plan.side == "LONG":
        if price > plan.entry_high:
            distance = (price - plan.entry_high) / max(price, 1e-9) * 100
            if price <= plan.entry_high + approach_buffer:
                return "WATCH", f"Price is {distance:.2f}% above the long pullback zone. Wait for price to come to the level."
            return "DO NOT CHASE", "Price remains too far above the planned long zone. Wait for the structural pullback."
        if plan.entry_low <= price <= plan.entry_high:
            return "PREPARE", "Price is inside the long entry zone. Wait for candle and volume confirmation."
        return "DO NOT CHASE", "Price traded below the long zone. Wait for a reclaim or a newly calculated setup."

    if price < plan.entry_low:
        distance = (plan.entry_low - price) / max(price, 1e-9) * 100
        if price >= plan.entry_low - approach_buffer:
            return "WATCH", f"Price is {distance:.2f}% below the short bounce zone. Wait for price to come to the level."
        return "DO NOT CHASE", "Price remains too far below the planned short zone. Wait for the structural bounce."
    if plan.entry_low <= price <= plan.entry_high:
        return "PREPARE", "Price is inside the short entry zone. Wait for rejection and volume confirmation."
    return "DO NOT CHASE", "Price traded above the short zone. Wait for rejection or a newly calculated setup."


def build_scan_message(signal: MarketSignal, context: Any | None = None) -> str:
    adjusted = float(getattr(context, "adjusted_score", signal.score))
    status, status_detail = execution_status(signal)
    session = get_session_context()
    reasons = list(signal.supporting_reasons)
    warnings = list(signal.warnings)
    if context is not None:
        reasons.extend(getattr(context, "reasons", []))
        reasons.extend(getattr(context, "macro_reasons", []))
        warnings.extend(getattr(context, "warnings", []))

    lines = [
        f"📡 {signal.symbol} MARKET SCAN",
        "",
        f"Price: {price_text(signal.price)}",
        f"{direction_emoji(signal.direction)} Direction: {signal.direction}",
        f"Technical score: {signal.score:+.1f}",
        f"Adjusted score: {adjusted:+.1f}",
        f"Confidence: {min(95, int(abs(adjusted)))}%",
        f"Grade: {get_signal_grade(signal)}",
        f"Readiness: {get_readiness_label(signal)}",
        f"Execution status: {status}",
        f"Action: {status_detail}",
        "",
        "SESSION CONTEXT",
        f"{session.label}: {session.detail}",
        f"Caution: {session.caution}",
        "",
        "TIMEFRAMES",
        *format_timeframes(signal),
        "",
        "CONFIDENCE BREAKDOWN",
        *build_confidence_breakdown(signal, context),
        "",
        "MARKET CONTEXT",
        *format_market_context(context),
    ]
    if signal.trade_plan:
        lines.extend(["", "TRADE MAP", *format_trade_plan(signal.trade_plan)])
    clean_reasons = unique_items(reasons, 8)
    if clean_reasons:
        lines.extend(["", "WHY", *[f"• {item}" for item in clean_reasons]])
    clean_warnings = unique_items(warnings, 6)
    if clean_warnings:
        lines.extend(["", "RISKS", *[f"• {item}" for item in clean_warnings]])
    lines.extend(["", "Analysis only. Wait for the planned level and confirmation; do not chase price."])
    return "\n".join(lines)


def build_alert_message(
    signal: MarketSignal,
    alert_type: str,
    context: Any | None = None,
    note: str = "",
) -> str:
    plan = signal.trade_plan
    adjusted = float(getattr(context, "adjusted_score", signal.score))
    session = get_session_context()
    headings = {
        "WATCH": f"👀 {signal.symbol} LEVEL APPROACHING",
        "PREPARE": f"🟠 {signal.symbol} INSIDE ENTRY ZONE",
        "ENTRY": f"🚨 {signal.symbol} ENTRY CONFIRMED",
        "DO_NOT_CHASE": f"⛔ {signal.symbol} DO NOT CHASE",
        "BREAKEVEN": f"🛡 {signal.symbol} PROTECT THE TRADE",
        "TP1": f"📈 {signal.symbol} TP1 REACHED",
        "TP2": f"📈 {signal.symbol} TP2 REACHED",
        "TP3": f"🏁 {signal.symbol} TP3 REACHED",
        "INVALIDATED": f"❌ {signal.symbol} SETUP INVALIDATED",
        "EXIT": f"🚪 {signal.symbol} EXIT CONDITION",
        "RAPID_CHANGE": f"⚡ {signal.symbol} RAPID MARKET CHANGE",
    }
    lines = [
        headings.get(alert_type, f"📡 {signal.symbol} ALERT"),
        "",
        f"Price: {price_text(signal.price)}",
        f"Direction: {signal.direction}",
        f"Adjusted score: {adjusted:+.1f}",
        f"Confidence: {min(95, int(abs(adjusted)))}%",
        f"Session: {session.label}",
        "",
        "CONFIDENCE BREAKDOWN",
        *build_confidence_breakdown(signal, context),
    ]
    if note:
        lines.extend(["", f"Action: {note}"])
    if plan and alert_type not in {"INVALIDATED", "EXIT"}:
        lines.extend(["", "TRADE MAP", *format_trade_plan(plan)])
    reasons = list(signal.supporting_reasons)
    warnings = list(signal.warnings)
    if context is not None:
        reasons.extend(getattr(context, "reasons", []))
        reasons.extend(getattr(context, "macro_reasons", []))
        warnings.extend(getattr(context, "warnings", []))
    reasons = unique_items(reasons, 5)
    warnings = unique_items(warnings, 4)
    if reasons:
        lines.extend(["", "CONFLUENCE", *[f"• {item}" for item in reasons]])
    if warnings:
        lines.extend(["", "RISKS", *[f"• {item}" for item in warnings]])
    lines.extend(["", "Decision support only. Use controlled risk and verify execution on your exchange."])
    return "\n".join(lines)


def build_derivatives_alert_message(
    signal: MarketSignal,
    derivatives: dict[str, Any],
    alert_type: str,
    action: str,
) -> str:
    headings = {
        "FUNDING_CROWDING": f"⚠️ {signal.symbol} FUNDING CROWDING",
        "OI_SURGE": f"⚡ {signal.symbol} OPEN INTEREST SURGE",
        "OI_DIVERGENCE": f"🔀 {signal.symbol} PRICE / OI DIVERGENCE",
        "DERIVATIVES_EXIT": f"🚪 {signal.symbol} DERIVATIVES EXIT WARNING",
    }
    return "\n".join(
        [
            headings.get(alert_type, f"⚠️ {signal.symbol} DERIVATIVES ALERT"),
            "",
            f"Price: {price_text(signal.price)}",
            f"Technical direction: {signal.direction}",
            f"Funding: {float(derivatives.get('funding_rate', 0.0)) * 100:+.4f}% "
            f"({derivatives.get('funding_label', 'UNKNOWN')})",
            f"Open interest: ${float(derivatives.get('open_interest_value', 0.0)):,.0f}",
            f"OI change: {float(derivatives.get('open_interest_change_5m', 0.0)):+.2f}% (5m), "
            f"{float(derivatives.get('open_interest_change_1h', 0.0)):+.2f}% (1h)",
            f"Provider: {derivatives.get('provider', 'UNKNOWN')}",
            "",
            f"Action: {action}",
            "",
            "Decision support only. Confirm price structure before acting.",
        ]
    )


def evaluate_derivatives_alert(
    signal: MarketSignal,
    derivatives: dict[str, Any] | None,
) -> AlertDecision:
    symbol = signal.symbol.upper()
    if not derivatives or not derivatives.get("live"):
        return AlertDecision(False, "NONE", symbol, "", "Derivatives data unavailable")

    funding = float(derivatives.get("funding_rate", 0.0))
    oi_5m = float(derivatives.get("open_interest_change_5m", 0.0))
    oi_1h = float(derivatives.get("open_interest_change_1h", 0.0))
    active = setup_states.get(symbol)

    if active:
        side = active["side"]
        adverse_funding = (
            (side == "LONG" and funding >= 0.001)
            or (side == "SHORT" and funding <= -0.001)
        )
        deleveraging = oi_5m <= -5.0 or oi_1h <= -8.0
        if adverse_funding or deleveraging:
            key = make_alert_key(symbol, "DERIVATIVES_EXIT", side)
            if alert_allowed(key, DERIVATIVES_EXIT_COOLDOWN_SECONDS):
                mark_alert_sent(key)
                action = (
                    "Crowded funding or rapid deleveraging is working against the managed setup. "
                    "Consider reducing exposure or tightening protection; verify on the exchange."
                )
                return AlertDecision(
                    True,
                    "DERIVATIVES_EXIT",
                    symbol,
                    build_derivatives_alert_message(signal, derivatives, "DERIVATIVES_EXIT", action),
                    "Derivatives conditions deteriorated against an active setup",
                )

    if abs(funding) >= 0.0005:
        key = make_alert_key(symbol, "FUNDING_CROWDING")
        if alert_allowed(key, DERIVATIVES_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            crowded_side = "longs" if funding > 0 else "shorts"
            action = (
                f"Leveraged {crowded_side} are crowded. Avoid chasing that side and watch for a squeeze."
            )
            return AlertDecision(
                True,
                "FUNDING_CROWDING",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "FUNDING_CROWDING", action),
                "Funding reached a crowded threshold",
            )

    if abs(oi_5m) >= 5.0 or abs(oi_1h) >= 10.0:
        key = make_alert_key(symbol, "OI_SURGE")
        if alert_allowed(key, DERIVATIVES_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            action = (
                "Leverage is changing unusually quickly. Wait for price confirmation and expect higher liquidation risk."
            )
            return AlertDecision(
                True,
                "OI_SURGE",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "OI_SURGE", action),
                "Open interest changed unusually quickly",
            )

    short_term = signal.analyses.get("5m") or signal.analyses.get("15m")
    short_score = short_term.score if short_term else signal.score
    divergence = (
        (short_score >= 25.0 and oi_1h <= -3.0)
        or (short_score <= -25.0 and oi_1h >= 3.0)
    )
    if divergence:
        key = make_alert_key(symbol, "OI_DIVERGENCE")
        if alert_allowed(key, DERIVATIVES_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            action = (
                "Price and leveraged positioning disagree. Treat the move as lower quality until both confirm."
            )
            return AlertDecision(
                True,
                "OI_DIVERGENCE",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "OI_DIVERGENCE", action),
                "Price and open interest diverged",
            )

    return AlertDecision(False, "NONE", symbol, "", "No derivatives alert condition")


def _decision(
    signal: MarketSignal,
    context: Any | None,
    alert_type: str,
    reason: str,
    note: str,
    cooldown: int,
) -> AlertDecision:
    side = side_from_direction(signal.direction)
    key = make_alert_key(signal.symbol, alert_type, side)
    if not alert_allowed(key, cooldown):
        return AlertDecision(False, "NONE", signal.symbol, "", "Cooldown active")
    mark_alert_sent(key)
    return AlertDecision(
        True,
        alert_type,
        signal.symbol,
        build_alert_message(signal, alert_type, context, note),
        reason,
    )


def evaluate_signal_alert(signal: MarketSignal, context: Any | None = None) -> AlertDecision:
    symbol = signal.symbol.upper()
    adjusted = float(getattr(context, "adjusted_score", signal.score))
    prior_score = previous_scores.get(symbol)
    previous_scores[symbol] = adjusted
    current_hash = signal_hash(signal)
    previous_hash = last_signal_hashes.get(symbol)
    last_signal_hashes[symbol] = current_hash
    state = setup_states.get(symbol)

    if state:
        plan = state["plan"]
        side = state["side"]
        price = signal.price
        if (side == "LONG" and price <= plan.stop_loss) or (side == "SHORT" and price >= plan.stop_loss):
            _clear_setup(symbol)
            return _decision(signal, context, "INVALIDATED", "Stop or invalidation reached", "Exit the setup; the planned invalidation level was reached.", MANAGEMENT_COOLDOWN_SECONDS)
        if side == "LONG":
            if price >= plan.tp3:
                _clear_setup(symbol)
                return _decision(signal, context, "TP3", "Final target reached", "Consider closing the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.tp2 and not state.get("tp2"):
                state["tp2"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP2", "Second target reached", "Consider scaling out further and trailing the stop.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.tp1 and not state.get("tp1"):
                state["tp1"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP1", "First target reached", "Consider partial profit and move protection toward breakeven.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.entry_high + plan.risk_per_unit * 0.75 and not state.get("breakeven"):
                state["breakeven"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "BREAKEVEN", "Trade moved in favor", "Consider moving the stop to breakeven after accounting for fees.", MANAGEMENT_COOLDOWN_SECONDS)
            if adjusted < -20:
                _clear_setup(symbol)
                return _decision(signal, context, "EXIT", "Direction reversed", "The model turned materially bearish; reassess or exit the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)
        else:
            if price <= plan.tp3:
                _clear_setup(symbol)
                return _decision(signal, context, "TP3", "Final target reached", "Consider closing the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.tp2 and not state.get("tp2"):
                state["tp2"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP2", "Second target reached", "Consider scaling out further and trailing the stop.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.tp1 and not state.get("tp1"):
                state["tp1"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP1", "First target reached", "Consider partial profit and move protection toward breakeven.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.entry_low - plan.risk_per_unit * 0.75 and not state.get("breakeven"):
                state["breakeven"] = True
                _persist_setup(symbol, state)
                return _decision(signal, context, "BREAKEVEN", "Trade moved in favor", "Consider moving the stop to breakeven after accounting for fees.", MANAGEMENT_COOLDOWN_SECONDS)
            if adjusted > 20:
                _clear_setup(symbol)
                return _decision(signal, context, "EXIT", "Direction reversed", "The model turned materially bullish; reassess or exit the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)

    if prior_score is not None and abs(adjusted - prior_score) >= RAPID_SCORE_CHANGE:
        return _decision(signal, context, "RAPID_CHANGE", "Rapid score change", "Pause and reassess; market conditions changed quickly.", RAPID_CHANGE_COOLDOWN_SECONDS)

    status, note = execution_status(signal)
    if status == "DO NOT CHASE":
        return _decision(signal, context, "DO_NOT_CHASE", "Price left the entry zone", note, DO_NOT_CHASE_COOLDOWN_SECONDS)
    if signal.trade_plan is None or signal.direction == "WAIT":
        return AlertDecision(False, "NONE", symbol, "", "No actionable setup")
    if previous_hash == current_hash:
        return AlertDecision(False, "NONE", symbol, "", "Duplicate signal")
    if status == "WATCH":
        return _decision(signal, context, "WATCH", "Price approaching planned level", note, WATCH_COOLDOWN_SECONDS)
    if status == "PREPARE" and signal.stage not in {"CONFIRMED", "STRONG"}:
        return _decision(signal, context, "PREPARE", "Price reached entry area", note, PREPARE_COOLDOWN_SECONDS)
    if status == "PREPARE" and signal.stage in {"CONFIRMED", "STRONG"}:
        setup_states[symbol] = {
            "side": signal.trade_plan.side,
            "plan": signal.trade_plan,
            "created_at": time.time(),
            "tp1": False,
            "tp2": False,
            "breakeven": False,
        }
        _persist_setup(symbol, setup_states[symbol])
        return _decision(signal, context, "ENTRY", "Setup confirmed at planned level", "Entry is confirmed near the planned zone. Avoid entering outside the displayed range.", ENTRY_COOLDOWN_SECONDS)
    return AlertDecision(False, "NONE", symbol, "", "No new alert condition")
