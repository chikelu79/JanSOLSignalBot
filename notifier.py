from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from bot_state import (
    get_active_setups, get_alert_history, get_armed_trade_plans, get_early_opportunities, get_early_opportunity_outcomes, get_risk_style, get_signal_performance, get_trading_horizon, is_auto_plan_enabled,
    record_alert_time, record_early_opportunity_outcome, record_signal_performance, remove_active_setup, remove_armed_trade_plans, remove_early_opportunity, set_active_setup, set_armed_trade_plans, set_early_opportunity, update_signal_performance,
)
from economic_calendar import format_event_time, get_profile_economic_risk as get_economic_risk
from lunar_context import get_lunar_context
from session_context import get_session_context, get_special_market_event
from strategy import MarketSignal, TradePlan, get_readiness_label, get_signal_grade
from trading_profile import estimate_position, get_profile, mfi_reversal_min_change

WATCH_COOLDOWN_SECONDS = 20 * 60
PREPARE_COOLDOWN_SECONDS = 10 * 60
ENTRY_COOLDOWN_SECONDS = 45 * 60
MANAGEMENT_COOLDOWN_SECONDS = 10 * 60
DO_NOT_CHASE_COOLDOWN_SECONDS = 20 * 60
RAPID_CHANGE_COOLDOWN_SECONDS = 15 * 60
DERIVATIVES_ALERT_COOLDOWN_SECONDS = 30 * 60
ORDER_FLOW_ALERT_COOLDOWN_SECONDS = 60 * 60
LARGE_TRADE_ALERT_COOLDOWN_SECONDS = 2 * 60 * 60
DERIVATIVES_EXIT_COOLDOWN_SECONDS = 15 * 60
ECONOMIC_ALERT_COOLDOWN_SECONDS = 6 * 60 * 60
SESSION_ALERT_COOLDOWN_SECONDS = 4 * 60 * 60
EARLY_OPPORTUNITY_COOLDOWN_SECONDS = 20 * 60
RAPID_SCORE_CHANGE = 22.0

last_alert_times: dict[str, float] = {}
last_signal_hashes: dict[str, str] = {}
previous_scores: dict[str, float] = {}
seen_news_ids: set[str] = set()
PERSIST_ALERT_HISTORY = os.getenv("JANBOT_DISABLE_PERSISTENT_ALERTS", "0") != "1"


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


ALERT_PRIORITY = {
    "DERIVATIVES_EXIT": 100,
    "ARMED_PLAN_CLOSED": 95,
    "ARMED_PLAN_READY": 90,
    "TACTICAL_ENTRY": 88,
    "ENTRY": 85,
    "EVENT_PLANS_REFRESHED": 82,
    "BREAKOUT_PLAN_CREATED": 80,
    "ARMED_PLAN_ZONE": 75,
    "ARMED_PLAN_APPROACHING": 65,
    "EARLY_OPPORTUNITY": 55,
    "LIQUIDATION_WAVE": 45,
    "ORDER_FLOW_SHIFT": 35,
    "LARGE_TRADE_FLOW": 30,
    "FUNDING_CROWDING": 25,
    "OI_SURGE": 20,
    "OI_DIVERGENCE": 20,
}


def select_monitor_alerts(*decisions: AlertDecision, maximum: int = 1) -> tuple[AlertDecision, ...]:
    """Keep the most actionable alert so supporting flow does not flood Telegram."""
    sendable = [decision for decision in decisions if decision.should_send]
    ranked = sorted(
        enumerate(sendable),
        key=lambda item: (-ALERT_PRIORITY.get(item[1].alert_type, 50), item[0]),
    )
    return tuple(decision for _, decision in ranked[:max(0, maximum)])


def valid_number(value: Any) -> bool:
    try:
        number = float(value)
        return number == number
    except (TypeError, ValueError):
        return False


def reversal_candle_confirmed(analysis: Any, side: str) -> bool:
    clues = " ".join(
        str(value).lower() for value in (
            list(getattr(analysis, "candle_patterns", []))
            + list(getattr(analysis, "chart_structures", []))
        )
    )
    bullish = ("bullish engulfing", "hammer", "morning star", "three white soldiers", "double bottom")
    bearish = ("bearish engulfing", "shooting star", "evening star", "three black crows", "double top")
    keywords = bullish if side == "LONG" else bearish
    return any(keyword in clues for keyword in keywords)


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
    persisted = get_alert_history().get(key, 0.0) if PERSIST_ALERT_HISTORY else 0.0
    last_sent = max(last_alert_times.get(key, 0.0), persisted)
    return time.time() - last_sent >= cooldown_seconds


def mark_alert_sent(key: str) -> None:
    sent_at = time.time()
    last_alert_times[key] = sent_at
    if PERSIST_ALERT_HISTORY:
        record_alert_time(key, sent_at)


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
    profile = get_profile(get_trading_horizon(), get_risk_style())
    lines: list[str] = []
    for interval in ("5m", "15m", "1h", "4h", "8h", "1d"):
        analysis = signal.analyses.get(interval)
        if analysis is None:
            lines.append(f"⚠️ {interval}: unavailable")
        else:
            lines.append(
                f"{direction_emoji(analysis.direction)} {interval}: "
                f"{analysis.direction} ({analysis.score:+.0f}; LONG ≥ +{profile.watch_threshold:.0f}, SHORT ≤ -{profile.watch_threshold:.0f})"
            )
        if interval in {"15m", "4h"}:
            lines.append("")
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


def build_early_opportunity_radar(signal: MarketSignal, context: Any | None = None) -> list[str]:
    """Show fresh lower-timeframe turns without promoting them to confirmed entries."""
    higher = [signal.analyses.get(interval) for interval in ("1h", "4h", "8h", "1d")]
    higher_scores = [analysis.score for analysis in higher if analysis is not None]
    higher_score = sum(higher_scores) / len(higher_scores) if higher_scores else 0.0
    higher_label = "BULLISH" if higher_score >= 20 else "BEARISH" if higher_score <= -20 else "MIXED"
    opportunities: list[str] = []
    profile = get_profile(get_trading_horizon(), get_risk_style())
    mfi_min_change = mfi_reversal_min_change(profile)
    for interval in ("5m", "15m"):
        analysis = signal.analyses.get(interval)
        if analysis is None:
            continue
        bullish: list[str] = []
        bearish: list[str] = []
        if analysis.previous_macd <= analysis.previous_macd_signal and analysis.macd > analysis.macd_signal:
            bullish.append("fresh bullish MACD line cross")
        elif analysis.previous_macd >= analysis.previous_macd_signal and analysis.macd < analysis.macd_signal:
            bearish.append("fresh bearish MACD line cross")
        if analysis.previous_macd_histogram <= 0 < analysis.macd_histogram:
            bullish.append("histogram flipped positive")
        elif analysis.previous_macd_histogram >= 0 > analysis.macd_histogram:
            bearish.append("histogram flipped negative")
        if analysis.previous_rsi < 30 <= analysis.rsi:
            bullish.append("RSI exited oversold")
        elif analysis.previous_rsi > 70 >= analysis.rsi:
            bearish.append("RSI exited overbought")
        rsi_6 = float(getattr(analysis, "rsi_6", 50.0))
        rsi_12 = float(getattr(analysis, "rsi_12", 50.0))
        rsi_24 = float(getattr(analysis, "rsi_24", 50.0))
        previous_rsi_6 = float(getattr(analysis, "previous_rsi_6", rsi_6))
        previous_rsi_12 = float(getattr(analysis, "previous_rsi_12", rsi_12))
        previous_rsi_24 = float(getattr(analysis, "previous_rsi_24", rsi_24))
        if previous_rsi_6 <= previous_rsi_12 and rsi_6 - rsi_12 >= 0.5:
            bullish.append(f"RSI 6 crossed above RSI 12 ({rsi_6:.1f}/{rsi_12:.1f})")
        elif previous_rsi_6 >= previous_rsi_12 and rsi_12 - rsi_6 >= 0.5:
            bearish.append(f"RSI 6 crossed below RSI 12 ({rsi_6:.1f}/{rsi_12:.1f})")
        if previous_rsi_12 <= previous_rsi_24 and rsi_12 - rsi_24 >= 0.5:
            bullish.append(f"RSI 12 crossed above RSI 24 ({rsi_12:.1f}/{rsi_24:.1f})")
        elif previous_rsi_12 >= previous_rsi_24 and rsi_24 - rsi_12 >= 0.5:
            bearish.append(f"RSI 12 crossed below RSI 24 ({rsi_12:.1f}/{rsi_24:.1f})")
        stoch_k = float(getattr(analysis, "stoch_rsi_k", 50.0))
        stoch_d = float(getattr(analysis, "stoch_rsi_d", 50.0))
        previous_stoch_k = float(getattr(analysis, "previous_stoch_rsi_k", stoch_k))
        previous_stoch_d = float(getattr(analysis, "previous_stoch_rsi_d", stoch_d))
        if previous_stoch_k <= previous_stoch_d and stoch_k - stoch_d >= 2.0:
            strength = " from oversold" if min(previous_stoch_k, previous_stoch_d) <= 20 else ""
            bullish.append(f"Stochastic RSI crossed bullish{strength}")
        elif previous_stoch_k >= previous_stoch_d and stoch_d - stoch_k >= 2.0:
            strength = " from overbought" if max(previous_stoch_k, previous_stoch_d) >= 80 else ""
            bearish.append(f"Stochastic RSI crossed bearish{strength}")
        mfi = float(getattr(analysis, "mfi", 50.0))
        previous_mfi = float(getattr(analysis, "previous_mfi", mfi))
        two_back_mfi = float(getattr(analysis, "two_back_mfi", previous_mfi))
        if mfi - previous_mfi >= mfi_min_change and previous_mfi <= two_back_mfi and previous_mfi <= 55.0:
            bullish.append(f"MFI money flow turned upward ({previous_mfi:.1f}→{mfi:.1f})")
        elif previous_mfi - mfi >= mfi_min_change and previous_mfi >= two_back_mfi and previous_mfi >= 45.0:
            bearish.append(f"MFI money flow turned downward ({previous_mfi:.1f}→{mfi:.1f})")
        if not bullish and not bearish:
            if analysis.rsi < 28:
                opportunities.extend([f"🟡 {interval} OVERSOLD EXHAUSTION WATCH — RSI {analysis.rsi:.1f}; wait for a bullish turn."])
            elif analysis.rsi > 72:
                opportunities.extend([f"🟡 {interval} OVERBOUGHT EXHAUSTION WATCH — RSI {analysis.rsi:.1f}; wait for a bearish turn."])
            continue
        side = "LONG" if len(bullish) > len(bearish) else "SHORT"
        triggers = bullish if side == "LONG" else bearish
        aligned = (side == "LONG" and higher_label == "BULLISH") or (side == "SHORT" and higher_label == "BEARISH")
        relationship = "TREND-ALIGNED" if aligned else "COUNTERTREND" if higher_label != "MIXED" else "MIXED-TREND"
        zone_low = min(analysis.ema20, analysis.vwap)
        zone_high = max(analysis.ema20, analysis.vwap)
        in_zone = zone_low <= signal.price <= zone_high
        invalidation = analysis.support if side == "LONG" else analysis.resistance
        icon = "🟢" if side == "LONG" else "🔴"
        required_volume = profile.volume_confirmation * (1.15 if relationship == "COUNTERTREND" else 1.0)
        volume_ok = analysis.relative_volume >= required_volume
        taker_flow = float(context.get("taker_flow_imbalance", 0.0) if isinstance(context, dict) else getattr(context, "taker_flow_imbalance", 0.0))
        large_flow = float(context.get("large_flow_imbalance", 0.0) if isinstance(context, dict) else getattr(context, "large_flow_imbalance", 0.0))
        supportive_flow = (side == "LONG" and (taker_flow >= 15 or large_flow >= 30)) or (side == "SHORT" and (taker_flow <= -15 or large_flow <= -30))
        opposing_flow = (side == "LONG" and (taker_flow <= -15 or large_flow <= -30)) or (side == "SHORT" and (taker_flow >= 15 or large_flow >= 30))
        taker_support = (side == "LONG" and taker_flow >= 15) or (side == "SHORT" and taker_flow <= -15)
        large_support = (side == "LONG" and large_flow >= 30) or (side == "SHORT" and large_flow <= -30)
        flow_status = "🟢 SUPPORTIVE" if supportive_flow else "🔴 OPPOSING" if opposing_flow else "🟡 BALANCED"
        confirmation_ready = volume_ok and (
            taker_support and large_support if relationship == "COUNTERTREND" else supportive_flow
        )
        if confirmation_ready and in_zone:
            verdict = "🟢 CONFIRMATION READY — price/candle confirmation is still required."
        elif confirmation_ready:
            verdict = "🟡 CONDITIONS SUPPORTIVE — price is not confirmed in the zone."
        elif opposing_flow or not volume_ok:
            verdict = "🔴 BLOCKED — volume or order flow does not confirm this opportunity."
        else:
            verdict = "🟡 DEVELOPING — confirmation is incomplete."
        if side == "LONG" and signal.price > zone_high:
            action = "Wait for a pullback into the decision zone; do not chase above it."
        elif side == "SHORT" and signal.price < zone_low:
            action = "Wait for a bounce into the decision zone; do not chase below it."
        elif side == "SHORT" and signal.price > zone_high:
            action = "Price is above the short zone. Require a bearish rejection close back below it; do not short while it remains above."
        elif zone_low <= signal.price <= zone_high:
            action = "Price is in the decision zone; require candle and flow confirmation before treating it as an entry."
        else:
            action = "Wait for price to reclaim the decision zone before reassessing."
        opportunities.extend([
            f"{icon} {interval} EARLY {side} WATCH — {relationship}",
            f"Trigger: {', '.join(triggers)}",
            *([f"Pattern context: 🔵 COMPRESSION (Bollinger width {analysis.bollinger_width:.2f}%). A breakout or fakeout can override the reversal; require a close and retest."] if float(getattr(analysis, "bollinger_width", 99.0)) < 3.0 else []),
            f"Decision zone: {price_text(zone_low)} to {price_text(zone_high)}; structural invalidation: {price_text(invalidation)}",
            f"Higher-timeframe trend: {higher_label} ({higher_score:+.0f}) — this is not a confirmed entry.",
            f"Volume confirmation: {'🟢 PASSED' if volume_ok else '🟡 MISSING'} ({analysis.relative_volume:.2f}×; required ≥ {required_volume:.2f}×)",
            f"Order-flow confirmation: {flow_status} (taker {taker_flow:+.1f}%; large trades {large_flow:+.1f}%)",
            f"Verdict: {verdict}",
            f"Action: {action}",
            "",
        ])
    if not opportunities:
        return ["No fresh 5m/15m MACD or RSI reversal trigger on this scan."]
    if opportunities[-1] == "":
        opportunities.pop()
    return opportunities


def evaluate_early_opportunity_alert(
    signal: MarketSignal,
    context: Any | None = None,
    derivatives: dict[str, Any] | None = None,
) -> AlertDecision:
    radar_context = context if context is not None else derivatives
    radar = build_early_opportunity_radar(signal, radar_context)
    profile = get_profile(get_trading_horizon(), get_risk_style())
    now = time.time()
    expiry_seconds = {"SCALPING": 30 * 60, "DAY": 2 * 60 * 60, "SWING": 12 * 60 * 60}[profile.horizon]
    fresh_blocks: dict[str, list[str]] = {}
    stored_opportunities = get_early_opportunities()
    for index, line in enumerate(radar):
        if " EARLY LONG WATCH" not in line and " EARLY SHORT WATCH" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        interval = parts[1]
        side = "LONG" if " EARLY LONG WATCH" in line else "SHORT"
        analysis = signal.analyses.get(interval)
        if analysis is None:
            continue
        zone_low = min(analysis.ema20, analysis.vwap)
        zone_high = max(analysis.ema20, analysis.vwap)
        block = [line]
        for detail in radar[index + 1:]:
            if not detail:
                break
            block.append(detail)
        relationship = "COUNTERTREND" if "COUNTERTREND" in line else "TREND-ALIGNED" if "TREND-ALIGNED" in line else "MIXED-TREND"
        opportunity_key = f"{signal.symbol}:{interval}:{side}"
        triggers = block[1].removeprefix("Trigger: ").split(", ") if len(block) > 1 else []
        invalidation = analysis.support if side == "LONG" else analysis.resistance
        midpoint = (zone_low + zone_high) / 2.0
        risk = midpoint - invalidation if side == "LONG" else invalidation - midpoint
        direction = 1.0 if side == "LONG" else -1.0
        invalid_at_creation = (
            (side == "LONG" and signal.price <= invalidation)
            or (side == "SHORT" and signal.price >= invalidation)
            or risk <= 0
        )
        if opportunity_key not in stored_opportunities and not invalid_at_creation:
            set_early_opportunity(opportunity_key, {
                "id": f"{opportunity_key}:{int(now)}",
                "symbol": signal.symbol, "interval": interval, "side": side,
                "zone_low": zone_low, "zone_high": zone_high,
                "invalidation": invalidation,
                "created_at": now, "expires_at": now + expiry_seconds,
                "relationship": relationship, "triggers": triggers,
                "profile": f"{profile.horizon} / {profile.risk_style}",
                "zone_reached": False,
                "target_1r": midpoint + direction * risk,
                "target_2r": midpoint + direction * risk * 2.0,
                "target_1r_hit": False, "target_2r_hit": False,
            })
        fresh_blocks[opportunity_key] = block

    taker_flow = float(radar_context.get("taker_flow_imbalance", 0.0) if isinstance(radar_context, dict) else getattr(radar_context, "taker_flow_imbalance", 0.0))
    large_flow = float(radar_context.get("large_flow_imbalance", 0.0) if isinstance(radar_context, dict) else getattr(radar_context, "large_flow_imbalance", 0.0))
    active = get_active_setups().get(signal.symbol)
    watch_candidates: list[tuple[float, str, dict[str, Any]]] = []
    for opportunity_key, opportunity in get_early_opportunities().items():
        if opportunity.get("symbol") != signal.symbol:
            continue
        side = str(opportunity["side"])
        interval = str(opportunity["interval"])
        analysis = signal.analyses.get(interval)
        if analysis is None:
            continue
        invalidated = (side == "LONG" and signal.price <= float(opportunity["invalidation"])) or (side == "SHORT" and signal.price >= float(opportunity["invalidation"]))
        if now >= float(opportunity["expires_at"]) or invalidated:
            record_early_opportunity_outcome(
                opportunity, "INVALIDATED" if invalidated else "EXPIRED", signal.price, now
            )
            remove_early_opportunity(opportunity_key)
            continue
        zone_low = float(opportunity["zone_low"])
        zone_high = float(opportunity["zone_high"])
        inside = zone_low <= signal.price <= zone_high
        if inside and not bool(opportunity.get("zone_reached", False)):
            opportunity["zone_reached"] = True
            set_early_opportunity(opportunity_key, opportunity)
            record_early_opportunity_outcome(opportunity, "ZONE_REACHED", signal.price, now)
        if bool(opportunity.get("zone_reached", False)):
            target_1r = float(opportunity.get("target_1r", 0.0))
            target_2r = float(opportunity.get("target_2r", 0.0))
            hit_1r = target_1r > 0 and ((side == "LONG" and signal.price >= target_1r) or (side == "SHORT" and signal.price <= target_1r))
            hit_2r = target_2r > 0 and ((side == "LONG" and signal.price >= target_2r) or (side == "SHORT" and signal.price <= target_2r))
            changed = False
            if hit_1r and not bool(opportunity.get("target_1r_hit", False)):
                opportunity["target_1r_hit"] = True
                changed = True
                record_early_opportunity_outcome(opportunity, "TARGET_1R", signal.price, now)
            if hit_2r and not bool(opportunity.get("target_2r_hit", False)):
                opportunity["target_2r_hit"] = True
                changed = True
                record_early_opportunity_outcome(opportunity, "TARGET_2R", signal.price, now)
            if changed:
                set_early_opportunity(opportunity_key, opportunity)
        distance = 0.0 if inside else min(abs(signal.price - zone_low), abs(signal.price - zone_high)) / max(signal.price, 1e-9) * 100.0
        taker_support = (side == "LONG" and taker_flow >= 15.0) or (side == "SHORT" and taker_flow <= -15.0)
        large_support = (side == "LONG" and large_flow >= 30.0) or (side == "SHORT" and large_flow <= -30.0)
        countertrend = opportunity.get("relationship") == "COUNTERTREND"
        required_volume = profile.volume_confirmation * (1.15 if countertrend else 1.0)
        flow_confirmed = (taker_support and large_support) if countertrend else (taker_support or large_support)
        proactive_ready = analysis.relative_volume >= required_volume and flow_confirmed
        if distance <= 1.0 and proactive_ready:
            watch_candidates.append((distance, opportunity_key, opportunity))
        candle_ok = reversal_candle_confirmed(analysis, side)
        if not active and inside and analysis.relative_volume >= required_volume and flow_confirmed and candle_ok:
            economic = get_economic_risk()
            if economic.block_new_entries:
                continue
            midpoint = (zone_low + zone_high) / 2.0
            risk = midpoint - float(opportunity["invalidation"]) if side == "LONG" else float(opportunity["invalidation"]) - midpoint
            if risk <= 0:
                remove_early_opportunity(opportunity_key)
                continue
            direction = 1.0 if side == "LONG" else -1.0
            plan = TradePlan(
                side=side, entry_low=zone_low, entry_high=zone_high,
                stop_loss=float(opportunity["invalidation"]), invalidation=float(opportunity["invalidation"]),
                tp1=midpoint + direction * risk * 1.25,
                tp2=midpoint + direction * risk * 2.0,
                tp3=midpoint + direction * risk * 3.0,
                risk_per_unit=risk, reward_risk_tp1=1.25,
                reward_risk_tp2=2.0, reward_risk_tp3=3.0,
            )
            signal_id = record_entry_signal("TACTICAL", signal.symbol, side, plan, interval)
            setup_states[signal.symbol] = {
                "side": side, "plan": plan, "created_at": now,
                "tp1": False, "tp2": False, "breakeven": False,
                "management_stop": plan.stop_loss, "exit_warning": False,
                "exit_risk_stage": 0, "last_exit_reasons": [],
                "tactical": True, "origin_interval": interval,
                "signal_id": signal_id,
            }
            _persist_setup(signal.symbol, setup_states[signal.symbol])
            record_early_opportunity_outcome(opportunity, "CONFIRMED", signal.price, now)
            remove_early_opportunity(opportunity_key)
            message = "\n".join([
                f"🚨 {signal.symbol} TACTICAL {side} ENTRY READY",
                "", f"Profile: {profile.horizon} / {profile.risk_style}",
                f"Origin: {interval} {opportunity['relationship']}",
                f"Entry zone: {price_text(zone_low)} to {price_text(zone_high)}",
                f"Stop / invalidation: {price_text(plan.stop_loss)}",
                f"TP1: {price_text(plan.tp1)} (1.25R)",
                f"TP2: {price_text(plan.tp2)} (2.00R)",
                f"TP3: {price_text(plan.tp3)} (3.00R)",
                f"Volume: {analysis.relative_volume:.2f}× (required {required_volume:.2f}×)",
                f"Taker flow: {taker_flow:+.1f}% | Large-trade flow: {large_flow:+.1f}%",
                "", "Reasons:", *[f"• {value}" for value in opportunity.get("triggers", [])[:6]],
                "", "Decision support only. Confirm the candle close and execution price; never enter outside the displayed zone.",
            ])
            return AlertDecision(True, "TACTICAL_ENTRY", signal.symbol, message, f"Stored {interval} opportunity reached its zone with confirmation")

    if not watch_candidates:
        return AlertDecision(False, "NONE", signal.symbol, "", "No stored early opportunity near its decision zone")
    distance, opportunity_key, opportunity = min(watch_candidates, key=lambda item: item[0])
    interval = str(opportunity["interval"])
    side = str(opportunity["side"])
    block = fresh_blocks.get(opportunity_key, [
        f"{'🟢' if side == 'LONG' else '🔴'} {interval} STORED {side} WATCH — {opportunity['relationship']}",
        f"Decision zone: {price_text(opportunity['zone_low'])} to {price_text(opportunity['zone_high'])}",
        f"Structural invalidation: {price_text(opportunity['invalidation'])}",
    ])
    key = make_alert_key(signal.symbol, "EARLY_OPPORTUNITY", f"{interval}:{side}")
    if not alert_allowed(key, EARLY_OPPORTUNITY_COOLDOWN_SECONDS):
        return AlertDecision(False, "NONE", signal.symbol, "", "Early-opportunity cooldown active")
    mark_alert_sent(key)
    economic = get_economic_risk()
    if economic.block_new_entries:
        event_label = "🔴 RELEASE IMPULSE — OBSERVE" if economic.status == "RELEASE IMPULSE" else "🔴 PRE-RELEASE SAFETY — STANDARD ENTRIES PAUSED"
        event_note = economic.detail
    elif economic.status == "EVENT OPPORTUNITY":
        event_label = "🟢 EVENT OPPORTUNITY WINDOW — CONFIRMATION REQUIRED"
        event_note = economic.detail
    elif economic.status not in {"CLEAR", "NONE"}:
        event_label = "🟡 EVENT APPROACHING — CAUTION"
        event_note = economic.detail
    else:
        event_label = "🟢 NO EVENT RESTRICTION"
        event_note = economic.detail
    message = "\n".join([
        f"🔔 {signal.symbol} EARLY {side} OPPORTUNITY",
        "",
        f"Current price: {price_text(signal.price)}",
        f"Distance to decision zone: {distance:.2f}%",
        *block,
        "",
        f"Economic risk: {event_label}",
        event_note,
        "",
        "This is an early watch, not a confirmed entry. Wait for the displayed price, candle, volume and flow conditions.",
    ])
    return AlertDecision(True, "EARLY_OPPORTUNITY", signal.symbol, message, f"Stored {interval} {side.lower()} opportunity near decision zone")


def build_radar_stats_message() -> str:
    active = list(get_early_opportunities().values())
    outcomes = get_early_opportunity_outcomes()
    counts = {status: sum(item.get("status") == status for item in outcomes) for status in ("ZONE_REACHED", "TARGET_1R", "TARGET_2R", "CONFIRMED", "INVALIDATED", "EXPIRED")}
    lifecycles: dict[str, list[dict[str, Any]]] = {}
    for item in outcomes:
        lifecycle_id = str(item.get("id", f"legacy:{item.get('symbol')}:{item.get('interval')}:{item.get('side')}:{item.get('timestamp')}"))
        lifecycles.setdefault(lifecycle_id, []).append(item)
    unconfirmed_1r = sum(
        any(item.get("status") == "TARGET_1R" for item in lifecycle)
        and not any(item.get("status") == "CONFIRMED" for item in lifecycle)
        for lifecycle in lifecycles.values()
    )
    unconfirmed_2r = sum(
        any(item.get("status") == "TARGET_2R" for item in lifecycle)
        and not any(item.get("status") == "CONFIRMED" for item in lifecycle)
        for lifecycle in lifecycles.values()
    )
    correctly_avoided = sum(
        any(item.get("status") == "INVALIDATED" for item in lifecycle)
        and not any(item.get("status") == "CONFIRMED" for item in lifecycle)
        for lifecycle in lifecycles.values()
    )
    calibration_sample = unconfirmed_1r + correctly_avoided
    missed_rate = unconfirmed_1r / calibration_sample * 100.0 if calibration_sample else 0.0
    relationship_misses: dict[str, int] = {}
    for lifecycle in lifecycles.values():
        if (
            any(item.get("status") == "TARGET_1R" for item in lifecycle)
            and not any(item.get("status") == "CONFIRMED" for item in lifecycle)
        ):
            label = str(lifecycle[0].get("relationship", "MIXED-TREND"))
            relationship_misses[label] = relationship_misses.get(label, 0) + 1
    if calibration_sample < 10:
        calibration_note = f"Collect more outcomes before tuning ({calibration_sample}/10 minimum sample)."
    elif missed_rate >= 60.0:
        calibration_note = "Filters may be too restrictive. Review the dominant missed setup type before lowering any threshold."
    elif missed_rate <= 25.0:
        calibration_note = "Filters are avoiding more invalidations than opportunities; keep current confirmation standards."
    else:
        calibration_note = "Miss/avoid balance is mixed; keep collecting evidence before changing thresholds."
    lines = [
        "📈 OPPORTUNITY RADAR TRACKING",
        "",
        f"Active watches: {len(active)}",
        f"Decision zones reached: {counts['ZONE_REACHED']}",
        f"Hypothetical 1R reached: {counts['TARGET_1R']}",
        f"Hypothetical 2R reached: {counts['TARGET_2R']}",
        f"Confirmed tactical entries: {counts['CONFIRMED']}",
        f"Invalidated: {counts['INVALIDATED']}",
        f"Expired without confirmation: {counts['EXPIRED']}",
        "",
        "MISSED-OPPORTUNITY AUDIT",
        f"Unconfirmed watches that later reached hypothetical 1R: {unconfirmed_1r}",
        f"Unconfirmed watches that later reached hypothetical 2R: {unconfirmed_2r}",
        f"Unconfirmed watches correctly avoided before invalidation: {correctly_avoided}",
        "Use these counts to tune thresholds; they do not assume an executable fill.",
        "",
        "CALIBRATION",
        f"Unconfirmed 1R rate: {missed_rate:.1f}% ({unconfirmed_1r}/{calibration_sample} auditable outcomes)",
        f"Guidance: {calibration_note}",
        *(["Misses by setup relationship:"] + [f"• {label}: {count}" for label, count in sorted(relationship_misses.items())] if relationship_misses else []),
        "",
        f"Recorded lifecycle events: {len(outcomes)} (latest 200 retained)",
    ]
    if active:
        lines.extend(["", "ACTIVE WATCHES"])
        for item in active[:10]:
            lines.append(
                f"• {item['symbol']} {item['interval']} {item['side']} — "
                f"{price_text(item['zone_low'])} to {price_text(item['zone_high'])}"
            )
    lines.extend(["", "These are radar lifecycle statistics, not profit results or exchange trades."])
    return "\n".join(lines)


def record_entry_signal(source: str, symbol: str, side: str, plan: Any, timeframe: str = "") -> str:
    now = time.time()
    signal_id = f"{source}:{symbol}:{side}:{int(now * 1000)}"
    value = lambda name: float(plan[name] if isinstance(plan, dict) else getattr(plan, name))
    entry = (value("entry_low") + value("entry_high")) / 2.0 if not isinstance(plan, dict) else (value("zone_low") + value("zone_high")) / 2.0
    record_signal_performance({
        "id": signal_id, "source": source, "symbol": symbol, "side": side,
        "profile": f"{get_trading_horizon()} / {get_risk_style()}", "timeframe": timeframe,
        "horizon": get_trading_horizon(), "risk_style": get_risk_style(),
        "setup_type": str(plan.get("zone_state", source) if isinstance(plan, dict) else source),
        "event_plan": bool(plan.get("event_plan", False)) if isinstance(plan, dict) else False,
        "setup_quality": 0,
        "entry": entry, "stop": value("stop_loss") if not isinstance(plan, dict) else value("stop"),
        "tp1": value("tp1"), "tp2": value("tp2"), "tp3": value("tp3"),
        "sent_at": now, "status": "OPEN", "tp1_hit": False, "tp2_hit": False,
        "tp3_hit": False, "closed_at": 0.0,
    })
    return signal_id


def build_success_stats_message() -> str:
    records = get_signal_performance()
    open_records = [item for item in records if item.get("status") == "OPEN"]
    won = [item for item in records if item.get("status") == "WON"]
    lost = [item for item in records if item.get("status") == "LOST"]
    exited = [item for item in records if item.get("status") == "EXITED"]
    resolved = len(won) + len(lost)
    success_rate = len(won) / resolved * 100.0 if resolved else 0.0
    tp1 = sum(bool(item.get("tp1_hit")) for item in records)
    tp2 = sum(bool(item.get("tp2_hit")) for item in records)
    tp3 = sum(bool(item.get("tp3_hit")) for item in records)
    lines = [
        "📊 SIGNAL SUCCESS STATISTICS", "",
        f"Confirmed entry signals sent: {len(records)}", f"Open: {len(open_records)}",
        f"Won (TP1 before stop): {len(won)}", f"Lost (stop before TP1): {len(lost)}",
        f"Exited without TP1: {len(exited)}", "",
        f"Success rate: {success_rate:.1f}% ({len(won)}/{resolved} resolved signals)",
        f"TP1 hits: {tp1} | TP2 hits: {tp2} | TP3 hits: {tp3}",
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        groups.setdefault(f"{item.get('profile', 'UNKNOWN')} • {item.get('timeframe') or 'profile setup'}", []).append(item)
    if groups:
        lines.extend(["", "BY PROFILE / TIMEFRAME"])
        for label, items in sorted(groups.items()):
            group_won = sum(item.get("status") == "WON" for item in items)
            group_lost = sum(item.get("status") == "LOST" for item in items)
            denominator = group_won + group_lost
            rate = group_won / denominator * 100.0 if denominator else 0.0
            lines.append(f"• {label}: {rate:.1f}% ({group_won}/{denominator}; {sum(item.get('status') == 'OPEN' for item in items)} open)")
    def add_breakdown(title: str, key) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            grouped.setdefault(str(key(item)), []).append(item)
        if not grouped:
            return
        lines.extend(["", title])
        for label, items in sorted(grouped.items()):
            group_won = sum(item.get("status") == "WON" for item in items)
            group_lost = sum(item.get("status") == "LOST" for item in items)
            denominator = group_won + group_lost
            rate = group_won / denominator * 100.0 if denominator else 0.0
            sample_note = "early sample" if denominator < 10 else "tracked sample"
            lines.append(f"• {label}: {rate:.1f}% ({group_won}/{denominator}; {sample_note})")
    add_breakdown("BY DIRECTION", lambda item: item.get("side", "UNKNOWN"))
    add_breakdown("BY SETUP TYPE", lambda item: item.get("setup_type", item.get("source", "UNKNOWN")))
    add_breakdown("EVENT COMPARISON", lambda item: "POST-EVENT" if item.get("event_plan") else "STANDARD")
    quality_values = [int(item.get("setup_quality", 0)) for item in records if int(item.get("setup_quality", 0)) > 0]
    if quality_values:
        lines.extend(["", f"Average recorded setup quality: {sum(quality_values) / len(quality_values):.1f}%"])
    lines.extend([
        "", "COUNTING RULES",
        "• Win = TP1 reached before the original stop.",
        "• Open and discretionary exits are excluded from the success-rate denominator.",
        "• Early watches, news, liquidation and whale-flow alerts are not counted as entry signals.",
        "• Results are signal observations, not verified exchange P&L.",
    ])
    return "\n".join(lines)


def create_structural_trade_plans(signal: MarketSignal) -> dict[str, dict[str, Any]]:
    profile = get_profile(get_trading_horizon(), get_risk_style())
    analyses = [(interval, signal.analyses[interval]) for interval in profile.primary_timeframes if interval in signal.analyses]
    if not analyses:
        return {}
    price = signal.price
    supports = [(a.support, i, a) for i, a in analyses if 0 < a.support <= price]
    resistances = [(a.resistance, i, a) for i, a in analyses if a.resistance >= price]
    continuation_distance = {"SCALPING": 1.0, "DAY": 2.5, "SWING": 5.0}.get(profile.horizon, 2.5)
    broken_resistances = [
        (a.resistance, i, a) for i, a in analyses
        if 0 < a.resistance < price and (price - a.resistance) / max(price, 1e-9) * 100 <= continuation_distance
    ]
    broken_supports = [
        (a.support, i, a) for i, a in analyses
        if a.support > price and (a.support - price) / max(price, 1e-9) * 100 <= continuation_distance
    ]
    if not supports:
        supports = [(price - max(float(a.atr), price * 0.005), i, a) for i, a in analyses]
    if not resistances:
        resistances = [(price + max(float(a.atr), price * 0.005), i, a) for i, a in analyses]
    bullish_continuation = signal.score >= 20.0 or any(float(a.score) >= 20.0 for _, a in analyses)
    bearish_continuation = signal.score <= -20.0 or any(float(a.score) <= -20.0 for _, a in analyses)
    selected = {
        "LONG": (
            max(broken_resistances, key=lambda item: item[0])
            if bullish_continuation and broken_resistances
            else max(supports, key=lambda item: item[0])
        ),
        "SHORT": (
            min(broken_supports, key=lambda item: item[0])
            if bearish_continuation and broken_supports
            else min(resistances, key=lambda item: item[0])
        ),
    }
    plans: dict[str, dict[str, Any]] = {}
    for side, (level, interval, analysis) in selected.items():
        atr = max(float(analysis.atr), price * 0.002)
        breakout_retest = (
            (side == "LONG" and any(level == item[0] and interval == item[1] for item in broken_resistances))
            or (side == "SHORT" and any(level == item[0] and interval == item[1] for item in broken_supports))
        )
        zone_low, zone_high = (
            (level - atr * 0.10, level + atr * 0.20)
            if breakout_retest
            else ((level, level + atr * 0.25) if side == "LONG" else (level - atr * 0.25, level))
        )
        stop = level - atr * 0.75 if side == "LONG" else level + atr * 0.75
        midpoint = (zone_low + zone_high) / 2.0
        risk = abs(midpoint - stop)
        direction = 1.0 if side == "LONG" else -1.0
        plans[side] = {
            "side": side, "interval": interval, "zone_low": zone_low, "zone_high": zone_high,
            "stop": stop, "tp1": midpoint + direction * risk * 1.25,
            "tp2": midpoint + direction * risk * 2.0,
            "tp3": midpoint + direction * risk * 3.0,
            "zone_state": "BREAKOUT RETEST" if breakout_retest else "REVERSAL WATCH",
        }
    return plans


def evaluate_armed_trade_plan_alert(signal: MarketSignal, context: Any | None = None) -> AlertDecision:
    all_armed = get_armed_trade_plans()
    plans = all_armed.get(signal.symbol, {})
    if not plans:
        return AlertDecision(False, "NONE", signal.symbol, "", "No armed trade plan")
    now = time.time()
    profile = get_profile(get_trading_horizon(), get_risk_style())
    taker = float(context.get("taker_flow_imbalance", 0.0) if isinstance(context, dict) else getattr(context, "taker_flow_imbalance", 0.0)) if context is not None else 0.0
    large = float(context.get("large_flow_imbalance", 0.0) if isinstance(context, dict) else getattr(context, "large_flow_imbalance", 0.0)) if context is not None else 0.0
    provider = str(context.get("provider", "UNAVAILABLE")) if isinstance(context, dict) else str(getattr(context, "derivatives_provider", "UNAVAILABLE")) if context is not None else "UNAVAILABLE"
    data_live = bool(context.get("live", False)) if isinstance(context, dict) else bool(getattr(context, "derivatives_live", False)) if context is not None else False
    fetched_at = float(context.get("fetched_at", 0.0)) if isinstance(context, dict) else now
    data_age = max(0.0, now - fetched_at) if fetched_at > 0 else float("inf")
    data_fresh = data_live and data_age <= 180.0
    data_label = (
        f"🟢 LIVE — {provider} ({data_age:.0f}s old)"
        if data_fresh and "fallback" not in provider.lower()
        else f"🔵 LIVE FALLBACK — {provider} ({data_age:.0f}s old)"
        if data_fresh
        else f"🔴 STALE / UNAVAILABLE — {provider}"
    )
    for side in ("LONG", "SHORT"):
        plan = plans.get(side)
        if not plan:
            continue
        signal_id = str(plan.get("signal_id", ""))
        performance_record = next((item for item in get_signal_performance() if item.get("id") == signal_id), None) if signal_id else None
        if performance_record:
            hit_tp1 = signal.price >= float(plan["tp1"]) if side == "LONG" else signal.price <= float(plan["tp1"])
            hit_tp2 = signal.price >= float(plan["tp2"]) if side == "LONG" else signal.price <= float(plan["tp2"])
            hit_tp3 = signal.price >= float(plan["tp3"]) if side == "LONG" else signal.price <= float(plan["tp3"])
            if hit_tp3:
                update_signal_performance(signal_id, status="WON", tp1_hit=True, tp2_hit=True, tp3_hit=True, closed_at=now)
            elif hit_tp2:
                update_signal_performance(signal_id, status="WON", tp1_hit=True, tp2_hit=True)
            elif hit_tp1:
                update_signal_performance(signal_id, status="WON", tp1_hit=True)
        invalidated = signal.price <= float(plan["stop"]) if side == "LONG" else signal.price >= float(plan["stop"])
        expired = now >= float(plan["expires_at"])
        if invalidated or expired:
            if signal_id:
                current = next((item for item in get_signal_performance() if item.get("id") == signal_id), {})
                update_signal_performance(
                    signal_id,
                    status="WON" if current.get("tp1_hit") else "LOST" if invalidated else "EXITED",
                    closed_at=now,
                )
            plans.pop(side, None)
            set_armed_trade_plans(signal.symbol, plans)
            reason = "INVALIDATED" if invalidated else "EXPIRED"
            follow_up = (
                "The level broke. Wait for a candle close beyond it and a retest before considering the breakout direction; automatic planning will rebuild the next structure."
                if invalidated
                else "The plan timed out. Automatic planning will prepare a new structure when qualified levels change."
            )
            return AlertDecision(True, "ARMED_PLAN_CLOSED", signal.symbol, "\n".join([
                f"{'🔴' if invalidated else '⌛'} {signal.symbol} {side} PLAN {reason}", "",
                f"Current price: {price_text(signal.price)}", f"Plan stop: {price_text(plan['stop'])}",
                "The preplanned setup has been removed. No trade is assumed.",
                f"Next: {follow_up}",
            ]), f"Armed {side.lower()} plan {reason.lower()}")
        zone_low, zone_high = float(plan["zone_low"]), float(plan["zone_high"])
        inside = zone_low <= signal.price <= zone_high
        analysis = signal.analyses.get(str(plan["interval"]))
        if analysis is None:
            continue
        volume_ok = analysis.relative_volume >= profile.volume_confirmation
        flow_ok = (side == "LONG" and (taker >= 15 or large >= 30)) or (side == "SHORT" and (taker <= -15 or large <= -30))
        directional_clues = [
            str(value).lower()
            for value in (
                list(getattr(analysis, "candle_patterns", []))
                + list(getattr(analysis, "chart_structures", []))
                + list(getattr(analysis, "divergences", []))
            )
        ]
        opposing_clue = any(("bearish" if side == "LONG" else "bullish") in clue for clue in directional_clues)
        momentum_ok = (analysis.score >= 20 if side == "LONG" else analysis.score <= -20) and not opposing_clue
        candle_ok = reversal_candle_confirmed(analysis, side)
        opposite_flow = (side == "LONG" and (taker <= -15 or large <= -30)) or (side == "SHORT" and (taker >= 15 or large >= 30))
        opposite_momentum = analysis.score <= -20 if side == "LONG" else analysis.score >= 20
        breakout_flag = bool(getattr(analysis, "breakout_down" if side == "LONG" else "breakout_up", False))
        breakout_pressure = opposite_flow and (opposite_momentum or breakout_flag)
        event_block = get_economic_risk().block_new_entries
        ready = inside and volume_ok and flow_ok and momentum_ok and candle_ok and data_fresh and not event_block
        passed_checks = sum((inside, volume_ok, flow_ok, momentum_ok, candle_ok, data_fresh, not event_block))
        if ready and not bool(plan.get("ready_alerted", False)):
            plan["ready_alerted"] = True
            plan["signal_id"] = record_entry_signal("ARMED", signal.symbol, side, plan, str(plan["interval"]))
            midpoint = (zone_low + zone_high) / 2.0
            risk = abs(midpoint - float(plan["stop"]))
            risk_percent = risk / max(midpoint, 1e-9) * 100.0
            momentum_quality = min(100.0, abs(float(analysis.score)) / max(profile.watch_threshold, 1.0) * 100.0)
            volume_quality = min(100.0, float(analysis.relative_volume) / max(profile.volume_confirmation, 0.01) * 100.0)
            flow_quality = min(100.0, max(abs(taker) / 15.0, abs(large) / 30.0) * 100.0)
            setup_quality = round(momentum_quality * 0.35 + volume_quality * 0.25 + flow_quality * 0.25 + 15.0)
            quality_label = "STRONG" if setup_quality >= 84 else "CONFIRMED" if setup_quality >= 74 else "QUALIFIED"
            leverage_estimate = estimate_position(side, midpoint, 100.0, 1.0, float(plan["stop"]))
            horizon_cap = {"SCALPING": 20.0, "DAY": 10.0, "SWING": 5.0}.get(profile.horizon, 10.0)
            style_factor = {"CONSERVATIVE": 0.50, "BALANCED": 0.75, "AGGRESSIVE": 1.0}.get(profile.risk_style, 0.75)
            profile_leverage_cap = max(1.0, horizon_cap * style_factor)
            recommended_leverage = min(float(leverage_estimate["recommended_max_leverage"]), profile_leverage_cap)
            reasons = [
                f"{plan['interval']} momentum score is {float(analysis.score):+.0f}",
                f"Volume is {analysis.relative_volume:.2f}× versus {profile.volume_confirmation:.2f}× required",
                f"Order flow agrees: taker {taker:+.1f}%, large trades {large:+.1f}%",
                f"Reversal candle confirmed on {plan['interval']}",
            ]
            if bool(plan.get("event_plan", False)):
                reasons.append("Levels were rebuilt from post-event price structure")
            update_signal_performance(
                plan["signal_id"],
                setup_quality=setup_quality,
                setup_type=str(plan.get("zone_state", "REVERSAL TEST")),
                event_plan=bool(plan.get("event_plan", False)),
            )
            managed_plan = TradePlan(
                side=side, entry_low=zone_low, entry_high=zone_high,
                stop_loss=float(plan["stop"]), invalidation=float(plan["stop"]),
                tp1=float(plan["tp1"]), tp2=float(plan["tp2"]), tp3=float(plan["tp3"]),
                risk_per_unit=risk, reward_risk_tp1=1.25,
                reward_risk_tp2=2.0, reward_risk_tp3=3.0,
            )
            setup_states[signal.symbol] = {
                "side": side, "plan": managed_plan, "created_at": now,
                "tp1": False, "tp2": False, "breakeven": False,
                "management_stop": managed_plan.stop_loss, "exit_warning": False,
                "exit_risk_stage": 0, "last_exit_reasons": [],
                "tactical": True, "origin_interval": str(plan["interval"]),
                "signal_id": plan["signal_id"],
            }
            _persist_setup(signal.symbol, setup_states[signal.symbol])
            remove_armed_trade_plans(signal.symbol)
            return AlertDecision(True, "ARMED_PLAN_READY", signal.symbol, "\n".join([
                f"🚨 {signal.symbol} {side} PLAN CONFIRMATION READY", "",
                f"Profile: {profile.horizon} / {profile.risk_style}",
                f"Setup quality: {setup_quality}% — {quality_label}",
                f"Price: {price_text(signal.price)}", f"Entry zone: {price_text(zone_low)} to {price_text(zone_high)}",
                f"Stop: {price_text(plan['stop'])} ({risk_percent:.2f}% from entry midpoint)",
                f"TP1: {price_text(plan['tp1'])} (1.25R)",
                f"TP2: {price_text(plan['tp2'])} (2.00R)",
                f"TP3: {price_text(plan['tp3'])} (3.00R)",
                f"Conservative leverage ceiling: {recommended_leverage:.0f}× for this profile and stop distance",
                f"Data quality: {data_label}",
                f"Volume: {analysis.relative_volume:.2f}× | Taker: {taker:+.1f}% | Large: {large:+.1f}%",
                "Reversal candle: 🟢 CONFIRMED",
                f"Entry checklist: 🟢 {passed_checks}/7 checks passed",
                "", "WHY THIS QUALIFIED", *[f"• {reason}" for reason in reasons],
                "", "Confirm the actual execution price and use the visual risk form before choosing margin. The leverage ceiling is an estimate, not a recommendation to maximize leverage.",
            ]), f"Armed {side.lower()} plan reached confirmation")
        if inside and not bool(plan.get("zone_alerted", False)):
            plan["zone_alerted"] = True
            if plan.get("zone_state") == "BREAKOUT RETEST" and flow_ok and (momentum_ok or candle_ok):
                zone_state = "BREAKOUT RETEST"
                zone_action = "The broken level is being retested with directional support. Require the full checklist before entering; do not chase away from the zone."
            elif flow_ok and (momentum_ok or candle_ok):
                zone_state = "REVERSAL TEST"
                zone_action = "Reversal evidence is developing. Still require every checklist item and event clearance before an entry alert."
            elif breakout_pressure:
                zone_state = "BREAKOUT PRESSURE"
                zone_action = "Do not fade this level. Wait for a close beyond it and a retest before considering the breakout direction."
            else:
                zone_state = "UNRESOLVED TEST"
                zone_action = "The level is being tested without directional agreement. Wait for reversal confirmation or a close-and-retest breakout."
            plan["zone_state"] = zone_state
            plans[side] = plan
            set_armed_trade_plans(signal.symbol, plans)
            status = "EVENT BLOCK" if event_block else "NOT YET CONFIRMED"
            return AlertDecision(True, "ARMED_PLAN_ZONE", signal.symbol, "\n".join([
                f"🔔 {signal.symbol} ENTERED {side} ZONE — {status}", "",
                f"Zone behavior: {'🟢' if zone_state == 'REVERSAL TEST' else '🔴' if zone_state == 'BREAKOUT PRESSURE' else '🟡'} {zone_state}",
                f"Price: {price_text(signal.price)}", f"Zone: {price_text(zone_low)} to {price_text(zone_high)}",
                f"Volume: {analysis.relative_volume:.2f}× ({'passed' if volume_ok else 'missing'})",
                f"Taker: {taker:+.1f}% | Large: {large:+.1f}%", f"Invalidation: {price_text(plan['stop'])}",
                f"Data quality: {data_label}",
                f"Reversal candle: {'🟢 CONFIRMED' if candle_ok else '🟡 WAITING'}",
                f"Entry checklist: {'🟢' if passed_checks == 7 else '🟡'} {passed_checks}/7 checks passed",
                "", f"Action: {zone_action}",
            ]), f"Armed {side.lower()} plan entered zone")
        distance = 0.0 if inside else min(abs(signal.price - zone_low), abs(signal.price - zone_high)) / max(signal.price, 1e-9) * 100.0
        approach_distance = {"SCALPING": 0.35, "DAY": 0.75, "SWING": 1.50}.get(profile.horizon, 0.75)
        directional_watch = flow_ok and (momentum_ok or candle_ok) and not opposing_clue
        if not inside and distance <= approach_distance and directional_watch and not bool(plan.get("approach_alerted", False)):
            plan["approach_alerted"] = True
            plans[side] = plan
            set_armed_trade_plans(signal.symbol, plans)
            missing = []
            if not volume_ok:
                missing.append("volume")
            if not momentum_ok:
                missing.append("momentum turn")
            if not candle_ok:
                missing.append("reversal candle")
            if event_block:
                missing.append("event safety clearance")
            if not data_fresh:
                missing.append("fresh derivatives/order-flow data")
            return AlertDecision(True, "ARMED_PLAN_APPROACHING", signal.symbol, "\n".join([
                f"👀 {signal.symbol} {side} PLAN APPROACHING", "",
                f"Current price: {price_text(signal.price)}",
                f"Decision zone: {price_text(zone_low)} to {price_text(zone_high)} ({distance:.2f}% away)",
                f"Invalidation: {price_text(plan['stop'])}",
                f"Directional evidence: {'momentum' if momentum_ok else 'reversal'} + order flow",
                f"Volume: {analysis.relative_volume:.2f}× ({'passed' if volume_ok else 'missing'})",
                f"Taker: {taker:+.1f}% | Large: {large:+.1f}%",
                f"Data quality: {data_label}",
                f"Still required: {', '.join(missing) if missing else 'price entering the zone'}",
                f"Event mode: {'monitoring continues; standard entry is paused' if event_block else 'clear'}",
                "", "Advance warning only. Let price reach the zone and wait for the confirmation-ready alert.",
            ]), f"Armed {side.lower()} plan is approaching its zone")
    return AlertDecision(False, "NONE", signal.symbol, "", "Armed plans are waiting for price")


def build_trade_dashboard(signal: MarketSignal, context: Any | None = None) -> str:
    profile = get_profile(get_trading_horizon(), get_risk_style())
    analyses = [
        (interval, signal.analyses[interval])
        for interval in profile.primary_timeframes
        if interval in signal.analyses
    ]
    if not analyses:
        return f"⚠️ No {profile.horizon.lower()} planning timeframes are available for {signal.symbol}."

    price = signal.price
    all_supports = [(analysis.support, interval, analysis) for interval, analysis in analyses if analysis.support > 0]
    all_resistances = [(analysis.resistance, interval, analysis) for interval, analysis in analyses if analysis.resistance > 0]
    if not all_supports:
        interval, analysis = analyses[0]
        all_supports = [(price - max(float(analysis.atr), price * 0.005), interval, analysis)]
    if not all_resistances:
        interval, analysis = analyses[0]
        all_resistances = [(price + max(float(analysis.atr), price * 0.005), interval, analysis)]
    supports = [item for item in all_supports if item[0] <= price]
    resistances = [item for item in all_resistances if item[0] >= price]
    continuation_distance = {"SCALPING": 1.0, "DAY": 2.5, "SWING": 5.0}.get(profile.horizon, 2.5)
    broken_resistances = [
        item for item in all_resistances
        if item[0] < price and (price - item[0]) / max(price, 1e-9) * 100 <= continuation_distance
    ]
    broken_supports = [
        item for item in all_supports
        if item[0] > price and (item[0] - price) / max(price, 1e-9) * 100 <= continuation_distance
    ]
    long_level, long_interval, long_analysis = (
        max(broken_resistances, key=lambda item: item[0])
        if (signal.score >= 20.0 or any(float(a.score) >= 20.0 for _, a in analyses)) and broken_resistances
        else
        max(supports, key=lambda item: item[0])
        if supports else min(all_supports, key=lambda item: abs(item[0] - price))
    )
    short_level, short_interval, short_analysis = (
        min(broken_supports, key=lambda item: item[0])
        if (signal.score <= -20.0 or any(float(a.score) <= -20.0 for _, a in analyses)) and broken_supports
        else
        min(resistances, key=lambda item: item[0])
        if resistances else min(all_resistances, key=lambda item: abs(item[0] - price))
    )

    taker_flow = float(getattr(context, "taker_flow_imbalance", 0.0)) if context is not None else 0.0
    large_flow = float(getattr(context, "large_flow_imbalance", 0.0)) if context is not None else 0.0
    economic = get_economic_risk()

    level_intervals = list(dict.fromkeys(profile.primary_timeframes + profile.confirmation_timeframes))
    level_analyses = [(interval, signal.analyses[interval]) for interval in level_intervals if interval in signal.analyses]

    def clustered_levels(attribute: str, side: str) -> list[str]:
        raw = sorted(
            [(float(getattr(analysis, attribute, 0.0)), interval) for interval, analysis in level_analyses if float(getattr(analysis, attribute, 0.0)) > 0],
            key=lambda item: item[0],
        )
        clusters: list[dict[str, Any]] = []
        tolerance = price * 0.0035
        for level, interval in raw:
            match = next((cluster for cluster in clusters if abs(level - cluster["level"]) <= tolerance), None)
            if match:
                count = len(match["intervals"])
                match["level"] = (match["level"] * count + level) / (count + 1)
                match["intervals"].append(interval)
            else:
                clusters.append({"level": level, "intervals": [interval]})
        relevant = [cluster for cluster in clusters if (cluster["level"] <= price if side == "SUPPORT" else cluster["level"] >= price)]
        relevant.sort(key=lambda cluster: abs(cluster["level"] - price))
        rows: list[str] = []
        for index, cluster in enumerate(relevant[:3], start=1):
            distance = abs(cluster["level"] - price) / max(price, 1e-9) * 100.0
            strength = "STRONG CLUSTER" if len(cluster["intervals"]) >= 2 else "single timeframe"
            icon = "🟢" if side == "SUPPORT" else "🔴"
            rows.append(
                f"{icon} {'S' if side == 'SUPPORT' else 'R'}{index}: {price_text(cluster['level'])} "
                f"({distance:.2f}% away) — {strength}; {', '.join(cluster['intervals'])}"
            )
        return rows

    support_rows = clustered_levels("support", "SUPPORT")
    resistance_rows = clustered_levels("resistance", "RESISTANCE")
    pattern_rows: list[str] = []
    for interval, analysis in level_analyses:
        clues = list(getattr(analysis, "candle_patterns", [])) + list(getattr(analysis, "chart_structures", [])) + list(getattr(analysis, "divergences", []))
        for clue in clues:
            entry = f"• {interval}: {clue}"
            if entry not in pattern_rows:
                pattern_rows.append(entry)
            if len(pattern_rows) >= 4:
                break
        if len(pattern_rows) >= 4:
            break

    def plan(side: str, level: float, interval: str, analysis: Any) -> tuple[list[str], dict[str, Any]]:
        atr = max(float(analysis.atr), price * 0.002)
        directional_clues = [
            str(value).lower()
            for value in (
                list(getattr(analysis, "candle_patterns", []))
                + list(getattr(analysis, "chart_structures", []))
                + list(getattr(analysis, "divergences", []))
            )
        ]
        if side == "LONG":
            zone_low, zone_high = level, level + atr * 0.25
            stop = level - atr * 0.75
            direction = 1.0
            flow_support = taker_flow >= 15.0 or large_flow >= 30.0
            opposing_clues = [clue for clue in directional_clues if "bearish" in clue]
            opposing_clue = bool(opposing_clues)
            momentum_support = analysis.score >= 20.0 and not opposing_clue
            trigger = "bullish reversal close + RSI/Stoch turn upward"
        else:
            zone_low, zone_high = level - atr * 0.25, level
            stop = level + atr * 0.75
            direction = -1.0
            flow_support = taker_flow <= -15.0 or large_flow <= -30.0
            opposing_clues = [clue for clue in directional_clues if "bullish" in clue]
            opposing_clue = bool(opposing_clues)
            momentum_support = analysis.score <= -20.0 and not opposing_clue
            trigger = "bearish rejection close + RSI/Stoch turn downward"
        midpoint = (zone_low + zone_high) / 2.0
        risk = abs(midpoint - stop)
        in_zone = zone_low <= price <= zone_high
        distance = 0.0 if in_zone else min(abs(price - zone_low), abs(price - zone_high)) / max(price, 1e-9) * 100.0
        volume_ok = analysis.relative_volume >= profile.volume_confirmation
        candle_ok = reversal_candle_confirmed(analysis, side)
        if economic.block_new_entries:
            status = "🔴 RELEASE IMPULSE" if economic.status == "RELEASE IMPULSE" else "🔴 PRE-RELEASE PAUSE"
        elif in_zone and volume_ok and flow_support and momentum_support and candle_ok:
            status = "🟢 CONFIRMATION READY"
        elif in_zone:
            status = "🔴 IN ZONE — NOT CONFIRMED"
        else:
            status = "🟡 WAITING FOR PRICE"
        icon = "🟢" if side == "LONG" else "🔴"
        breakout_retest = (
            (side == "LONG" and any(level == item[0] and interval == item[1] for item in broken_resistances))
            or (side == "SHORT" and any(level == item[0] and interval == item[1] for item in broken_supports))
        )
        structure_label = "breakout retest" if breakout_retest else f"{interval} structure"
        rows = [
            f"{icon} {side} PLAN — {status}",
            f"Zone: {price_text(zone_low)} to {price_text(zone_high)} ({distance:.2f}% away; {structure_label})",
            f"Invalidation: {price_text(stop)}",
            f"Trigger required: {trigger}",
            f"Reversal candle: {'🟢 CONFIRMED' if candle_ok else '🟡 WAITING'}",
            f"Volume / flow: {'🟢' if volume_ok else '🟡'} {analysis.relative_volume:.2f}× / "
            f"{'🟢' if flow_support else '🔴'} taker {taker_flow:+.1f}%, large {large_flow:+.1f}%",
            f"Provisional TP1: {price_text(midpoint + direction * risk * 1.25)} (1.25R)",
            f"Provisional TP2: {price_text(midpoint + direction * risk * 2.0)} (2.00R)",
            f"Provisional TP3: {price_text(midpoint + direction * risk * 3.0)} (3.00R)",
        ]
        return rows, {
            "side": side, "distance": distance, "in_zone": in_zone,
            "volume_ok": volume_ok, "flow_support": flow_support,
            "momentum_support": momentum_support, "candle_ok": candle_ok, "trigger": trigger,
            "directional_support": sum((momentum_support, candle_ok, flow_support)),
            "opposing_clue": opposing_clue,
            "opposing_reason": opposing_clues[0].capitalize() if opposing_clues else "",
            "compressed": float(getattr(analysis, "bollinger_width", 99.0)) < 3.0,
        }

    score = float(getattr(context, "adjusted_score", signal.score)) if context is not None else signal.score
    bias = "BULLISH" if score >= profile.watch_threshold else "BEARISH" if score <= -profile.watch_threshold else "MIXED / WAIT"
    armed_sides = list(get_armed_trade_plans().get(signal.symbol, {}))
    long_rows, long_focus = plan("LONG", long_level, long_interval, long_analysis)
    short_rows, short_focus = plan("SHORT", short_level, short_interval, short_analysis)
    ranked_focus = sorted(
        (long_focus, short_focus),
        key=lambda item: (-item["directional_support"], item["distance"]),
    )
    focus, alternative = ranked_focus
    focus_is_clear = (
        focus["flow_support"]
        and (focus["momentum_support"] or focus["candle_ok"])
        and not focus["opposing_clue"]
        and focus["directional_support"] > alternative["directional_support"]
    )
    focus_side = focus["side"]
    focus_armed = focus_side in armed_sides
    readiness_checks = (
        focus["in_zone"], focus["momentum_support"], focus["candle_ok"],
        focus["volume_ok"], focus["flow_support"], not economic.block_new_entries,
    )
    readiness_passed = sum(bool(value) for value in readiness_checks)
    readiness_icon = "🟢" if readiness_passed == len(readiness_checks) else "🟡" if readiness_passed >= 3 else "🔴"
    if economic.status == "RELEASE IMPULSE":
        next_action = "Observe the first move. Do not chase; wait for the event opportunity window and a close/retest."
    elif economic.block_new_entries:
        next_action = "Keep both plans armed. Standard entries are paused until release; then wait for the impulse to settle."
    elif not focus_is_clear:
        next_action = "Directional evidence conflicts or is incomplete. Keep both plans armed and wait for one side to gain at least two confirming clues."
    elif not focus["in_zone"]:
        next_action = f"Let price reach the {focus_side} zone; do not chase it."
    elif not focus["volume_ok"] or not focus["flow_support"]:
        next_action = "Price is in zone, but confirmation is incomplete. Wait for volume and flow to agree."
    elif not focus["momentum_support"]:
        next_action = f"Wait for the required {focus['trigger']}."
    elif not focus["candle_ok"]:
        next_action = f"Momentum supports the plan, but wait for the required {focus['trigger']}."
    else:
        next_action = f"Require the {focus['trigger']} and wait for the confirmed-entry alert."
    focus_proximity = "IN ZONE" if focus["in_zone"] else f"{focus['distance']:.2f}% AWAY"
    if economic.status == "RELEASE IMPULSE":
        economic_summary = "RELEASE IMPULSE — OBSERVE, DO NOT CHASE"
        economic_check = f"🔴 {economic_summary}"
    elif economic.block_new_entries:
        economic_summary = "PRE-RELEASE SAFETY — STANDARD ENTRIES PAUSED; EVENT MODE ARMED"
        economic_check = f"🔴 {economic_summary}"
    elif economic.status == "EVENT OPPORTUNITY":
        economic_summary = "EVENT OPPORTUNITY WINDOW — CONFIRMATION REQUIRED"
        economic_check = f"🟢 {economic_summary}"
    elif economic.status not in {"CLEAR", "NONE"}:
        economic_summary = "EVENT APPROACHING — CAUTION"
        economic_check = f"🟡 {economic_summary}; entries allowed for now"
    else:
        economic_summary = "NO EVENT RESTRICTION"
        economic_check = f"🟢 {economic_summary} — entries allowed"
    if focus_is_clear:
        focus_lines = [
            f"🎯 FOCUS: {focus_side} WATCH — {focus_proximity}",
            f"Directional agreement: 🟢 {focus['directional_support']}/3 clues support {focus_side}",
            f"Entry readiness: {readiness_icon} {readiness_passed}/{len(readiness_checks)} checks passed",
            f"Price at zone: {'🟢 YES' if focus['in_zone'] else '🟡 NOT YET'}",
            f"Momentum: {'🟢 SUPPORTIVE' if focus['momentum_support'] else '🟡 WAITING FOR TURN'}",
            f"Reversal candle: {'🟢 CONFIRMED' if focus['candle_ok'] else '🟡 WAITING'}",
            f"Volume: {'🟢 PASSED' if focus['volume_ok'] else '🟡 MISSING'}",
            f"Order flow: {'🟢 SUPPORTIVE' if focus['flow_support'] else '🔴 OPPOSING / UNCONFIRMED'}",
            f"Economic event: {economic_check}",
            f"Plan control: {'🟢 ARMED' if focus_armed else f'⚪ NOT ARMED — tap Arm {focus_side.title()} to monitor it'}",
        ]
        if focus["compressed"]:
            focus_lines.append("Breakout risk: 🔵 COMPRESSION — require a close/retest; the level can break instead of reverse.")
    else:
        long_block = (
            long_focus["opposing_reason"]
            or ("Order flow opposes long" if not long_focus["flow_support"] else "Waiting for momentum or reversal confirmation")
        )
        short_block = (
            short_focus["opposing_reason"]
            or ("Order flow opposes short" if not short_focus["flow_support"] else "Waiting for momentum or reversal confirmation")
        )
        focus_lines = [
            "🎯 FOCUS: NO CLEAR DIRECTION — WAIT",
            f"🟢 Long evidence: {long_focus['directional_support']}/3; zone {long_focus['distance']:.2f}% away — BLOCKED: {long_block}",
            f"🔴 Short evidence: {short_focus['directional_support']}/3; zone {short_focus['distance']:.2f}% away — BLOCKED: {short_block}",
            "Decision: Proximity alone cannot select a trade. Momentum, reversal candle and order flow must establish one preferred side.",
            f"Economic event: {economic_check}",
            "Plan control: Keep LONG and SHORT armed until one direction becomes dominant.",
        ]
    focus_lines.append(f"Next action: {next_action}")
    lines = [
        f"🎯 {signal.symbol} TRADE PLANNER",
        f"Profile: {profile.horizon} / {profile.risk_style}",
        "",
        f"Current price: {price_text(price)}",
        f"Decision bias: {bias} ({score:+.1f}; directional at ±{profile.watch_threshold:.0f})",
        f"Economic risk: {economic_summary}",
        f"Armed plans: {', '.join(armed_sides) if armed_sides else 'NONE'}",
        f"Automatic planning: {'ON' if is_auto_plan_enabled() else 'OFF'}",
        "",
        *focus_lines,
        "",
        "KEY LEVEL MAP",
        *(resistance_rows or ["🔴 No valid resistance above current price."]),
        f"🔵 NOW: {price_text(price)}",
        *(support_rows or ["🟢 No valid support below current price."]),
        "",
        "REVERSAL / PATTERN CLUES",
        *(pattern_rows or ["• No active multi-timeframe reversal pattern; wait for a candle/oscillator trigger at a key level."]),
        "",
        *long_rows,
        "",
        *short_rows,
        "",
        "RULES",
        "• Let price enter a zone; do not chase it.",
        "• A zone is not an entry without the displayed reversal, volume and flow confirmation.",
        "• Targets are provisional until an entry is confirmed.",
        "",
        "Use /scan for the complete evidence report.",
    ]
    return "\n".join(lines)


def build_trade_brief(signal: MarketSignal, context: Any | None = None) -> str:
    """Return the decision-first portion of the planner for quick Telegram use."""
    dashboard = build_trade_dashboard(signal, context)
    decision_section = dashboard.split("\nKEY LEVEL MAP", 1)[0].strip()
    dashboard_lines = dashboard.splitlines()

    def compact_plan(marker: str) -> list[str]:
        try:
            start = next(index for index, line in enumerate(dashboard_lines) if marker in line)
        except StopIteration:
            return []
        block: list[str] = []
        for line in dashboard_lines[start:]:
            if not line.strip() and block:
                break
            if (
                line.startswith(("🟢 LONG PLAN", "🔴 SHORT PLAN", "Zone:", "Invalidation:", "Trigger required:", "Reversal candle:", "Volume / flow:"))
            ):
                block.append(line)
        return block

    compact_levels = [
        *compact_plan("LONG PLAN"),
        "",
        *compact_plan("SHORT PLAN"),
    ]
    return "\n".join([
        decision_section.replace("TRADE PLANNER", "QUICK TRADE VIEW", 1),
        "",
        "PLANNED LEVELS",
        *compact_levels,
        "",
        "DETAILS",
        "• /trade — full plans, key levels, stops and targets",
        "• /scan — complete technical, macro and order-flow evidence",
        "• /health — live source and decision-gate status",
    ])


def evidence_icon(reason: str) -> str:
    text = reason.lower()
    bearish = ("crossed below", "bearish", "turned downward", "selling pressure", "flipped negative", "below its", "below ema")
    bullish = ("crossed above", "bullish", "turned upward", "buying pressure", "flipped positive", "above", "improving", "rising")
    if any(term in text for term in bearish):
        return "🔴"
    if any(term in text for term in bullish):
        return "🟢"
    return "🔵"


def build_balanced_evidence(signal: MarketSignal) -> list[str]:
    profile = get_profile(get_trading_horizon(), get_risk_style())
    order = list(profile.primary_timeframes + profile.confirmation_timeframes)
    order.extend(interval for interval in ("5m", "15m", "1h", "4h", "8h", "1d") if interval not in order)
    evidence: list[str] = []
    for interval in order:
        analysis = signal.analyses.get(interval)
        if analysis is None:
            continue
        bullish: list[str] = []
        bearish: list[str] = []
        for reason in analysis.reasons:
            formatted = f"{interval}: {reason}"
            icon = evidence_icon(formatted)
            if icon == "🟢" and formatted not in bullish:
                bullish.append(formatted)
            elif icon == "🔴" and formatted not in bearish:
                bearish.append(formatted)
        structural_bullish = [
            f"{interval}: Price is above EMA 20",
            f"{interval}: EMA 20 is above EMA 50",
            f"{interval}: MACD is above its signal line",
            f"{interval}: RSI stack is bullish (6 > 12 > 24)",
        ]
        structural_bearish = [
            f"{interval}: Price is below EMA 20",
            f"{interval}: EMA 20 is below EMA 50",
            f"{interval}: MACD is below its signal line",
            f"{interval}: RSI stack is bearish (6 < 12 < 24)",
        ]
        checks = [
            (analysis.price > analysis.ema20, structural_bullish[0], structural_bearish[0]),
            (analysis.ema20 > analysis.ema50, structural_bullish[1], structural_bearish[1]),
            (analysis.macd > analysis.macd_signal, structural_bullish[2], structural_bearish[2]),
        ]
        for condition, bull_text, bear_text in checks:
            target = bullish if condition else bearish
            value = bull_text if condition else bear_text
            if value not in target:
                target.append(value)
        if analysis.rsi_6 > analysis.rsi_12 > analysis.rsi_24:
            bullish.append(structural_bullish[3])
        elif analysis.rsi_6 < analysis.rsi_12 < analysis.rsi_24:
            bearish.append(structural_bearish[3])
        selected = []
        if analysis.score < 0:
            selected.extend([("🔴", bearish[0])] if bearish else [])
            selected.extend([("🟢", bullish[0])] if bullish else [])
        else:
            selected.extend([("🟢", bullish[0])] if bullish else [])
            selected.extend([("🔴", bearish[0])] if bearish else [])
        for icon, item in selected:
            line = f"• {icon} {item}"
            if line not in evidence:
                evidence.append(line)
            if len(evidence) >= 8:
                return evidence
    return evidence


def format_market_context(context: Any | None) -> list[str]:
    if context is None:
        return ["Macro context: unavailable"]
    fear_live = bool(getattr(context, "fear_greed_live", False))
    fear_suffix = "LIVE" if fear_live else "FALLBACK"
    funding_rate = float(getattr(context, "funding_rate", 0.0))
    perp_basis = float(getattr(context, "perp_spot_basis", 0.0))
    perp_basis_live = bool(getattr(context, "perp_spot_basis_live", False))
    basis_label = "LONG PREMIUM" if perp_basis >= 0.25 else "SHORT DISCOUNT" if perp_basis <= -0.25 else "BALANCED"
    basis_icon = "🔴" if abs(perp_basis) >= 0.25 else "🟡"
    funding_effect = "bearish crowding risk" if funding_rate >= 0.0005 else "bullish squeeze risk" if funding_rate <= -0.0005 else "neutral"
    oi_5m = float(getattr(context, "open_interest_change_5m", 0.0))
    oi_1h = float(getattr(context, "open_interest_change_1h", 0.0))
    oi_regime = "HIGH EXPANSION" if oi_1h >= 5.0 else "HIGH CONTRACTION" if oi_1h <= -5.0 else "NORMAL"
    long_liq = float(getattr(context, "long_liquidations_1h", 0.0))
    short_liq = float(getattr(context, "short_liquidations_1h", 0.0))
    oi_value = float(getattr(context, "open_interest_value", 0.0))
    liq_intensity = (long_liq + short_liq) / oi_value * 100.0 if oi_value else 0.0
    liq_regime = "HIGH" if liq_intensity >= 0.10 else "ELEVATED" if liq_intensity >= 0.02 else "LOW"
    correlation = float(getattr(context, "btc_correlation", 0.0))
    correlation_regime = "HIGH" if abs(correlation) >= 0.70 else "MEDIUM" if abs(correlation) >= 0.40 else "LOW"
    market_change = float(getattr(context, "crypto_market_change_24h", 0.0))
    vix_value = float(getattr(context, "vix_value", 0.0))
    fear_value = float(getattr(context, "fear_greed_value", 50.0))
    btc_premium = float(getattr(context, "btc_coinbase_premium", 0.0))
    eth_premium = float(getattr(context, "eth_coinbase_premium", 0.0))
    eth_relative = float(getattr(context, "eth_btc_relative_strength", 0.0))
    eth_dominance = float(getattr(context, "eth_dominance", 0.0))
    premium_live = bool(getattr(context, "coinbase_premium_live", False))

    def premium_line(asset: str, value: float) -> str:
        label = "US BUYING" if value >= 0.10 else "US SELLING" if value <= -0.10 else "BALANCED"
        icon = "🟢" if value >= 0.10 else "🔴" if value <= -0.10 else "🟡"
        availability = "LIVE" if premium_live else "UNAVAILABLE"
        return f"{icon} {asset} Coinbase Premium: {value:+.3f}% — {label} (directional at ±0.10%; {availability})"
    funding_icon = "🔴" if abs(funding_rate) >= 0.0005 else "🟡" if abs(funding_rate) >= 0.0001 else "🟢"
    oi_icon = "🟡" if abs(oi_1h) >= 5.0 else "🔵"
    liquidation_pressure = str(getattr(context, "liquidation_pressure", "UNAVAILABLE"))
    liquidation_icon = "🟢" if liquidation_pressure == "SHORT SQUEEZE" else "🔴" if liquidation_pressure == "LONG FLUSH" else "🟡" if liq_regime != "LOW" else "🔵"
    book_imbalance = float(getattr(context, "orderbook_imbalance", 0.0))
    book_label = "BID HEAVY" if book_imbalance >= 15.0 else "ASK HEAVY" if book_imbalance <= -15.0 else "BALANCED"
    book_icon = "🟢" if book_imbalance >= 15.0 else "🔴" if book_imbalance <= -15.0 else "🟡"
    taker_ratio = float(getattr(context, "taker_buy_ratio", 50.0))
    taker_imbalance = float(getattr(context, "taker_flow_imbalance", 0.0))
    taker_label = "BUY DOMINANT" if taker_imbalance >= 15.0 else "SELL DOMINANT" if taker_imbalance <= -15.0 else "BALANCED"
    taker_icon = "🟢" if taker_imbalance >= 15.0 else "🔴" if taker_imbalance <= -15.0 else "🟡"
    large_flow = float(getattr(context, "large_flow_imbalance", 0.0))
    large_flow_label = "BUY DOMINANT" if large_flow >= 30.0 else "SELL DOMINANT" if large_flow <= -30.0 else "BALANCED"
    large_flow_icon = "🟢" if large_flow >= 30.0 else "🔴" if large_flow <= -30.0 else "🟡"
    profile = get_profile(get_trading_horizon(), get_risk_style())
    return [
        f"{direction_emoji(getattr(context, 'btc_direction', 'UNKNOWN'))} BTC: {getattr(context, 'btc_direction', 'UNKNOWN')} "
        f"({getattr(context, 'btc_score', 0):+.1f}; directional at ±{profile.watch_threshold:.0f})",
        f"{direction_emoji(getattr(context, 'eth_direction', 'UNKNOWN'))} ETH: {getattr(context, 'eth_direction', 'UNKNOWN')} "
        f"({getattr(context, 'eth_score', 0):+.1f}; directional at ±{profile.watch_threshold:.0f})",
        premium_line("BTC", btc_premium),
        premium_line("ETH", eth_premium),
        f"{'🟢' if eth_relative >= 0.50 else '🔴' if eth_relative <= -0.50 else '🟡'} ETH vs BTC momentum (12h): "
        f"{eth_relative:+.2f}% — {'OUTPERFORMING' if eth_relative >= 0.50 else 'UNDERPERFORMING' if eth_relative <= -0.50 else 'BALANCED'} "
        f"(directional at ±0.50%)",
        f"{'🟢' if eth_dominance >= 18.0 else '🔴' if 0 < eth_dominance <= 14.0 else '🟡'} ETH dominance: {eth_dominance:.2f}% "
        f"(broad alt support ≥ 18%; weak ≤ 14%)",
        "",
        f"🔵 BTC correlation: {correlation:.2f} ({correlation_regime}; high ≥ 0.70)",
        f"🔵 BTC dominance: {getattr(context, 'btc_dominance', 0):.2f}% (altcoin headwind ≥ 58%; support ≤ 52%)",
        f"{'🟢' if market_change >= 1 else '🔴' if market_change <= -1 else '🟡'} Crypto market 24h: {market_change:+.2f}% (strong move at ±3%)",
        f"{'🔴' if vix_value >= 25 else '🟡' if vix_value >= 18 else '🟢'} VIX: {vix_value:.2f} "
        f"({getattr(context, 'vix_regime', 'UNKNOWN')}; risk stress usually ≥ 25)",
        f"{'🔴' if fear_value < 25 else '🟢' if fear_value > 75 else '🟡'} Fear & Greed: {fear_value:.0f} "
        f"({getattr(context, 'fear_greed_label', 'NEUTRAL')}, {fear_suffix}; extreme fear < 25, greed > 75)",
        f"{funding_icon} Funding: {funding_rate * 100:+.4f}% "
        f"({getattr(context, 'funding_label', 'UNAVAILABLE')}, "
        f"{funding_effect}; crowded at ±0.0500%)",
        (
            f"{basis_icon} Perpetual vs spot basis: {perp_basis:+.3f}% — {basis_label} (crowded at ±0.25%)"
            if perp_basis_live
            else "🔵 Perpetual vs spot basis: UNAVAILABLE (requires matching OKX spot and perpetual data)"
        ),
        "",
        f"🔵 Open interest: ${getattr(context, 'open_interest_value', 0.0):,.0f} (baseline; direction comes from its % change)",
        f"{oi_icon} OI change: {oi_5m:+.2f}% (5m), {oi_1h:+.2f}% (1h) — {oi_regime} (high at ±5%/1h)",
        f"🔵 Liquidations 1h: longs ${long_liq:,.0f} / shorts ${short_liq:,.0f}",
        f"{liquidation_icon} Liquidation pressure: {liquidation_pressure} — "
        f"{liq_regime} intensity {liq_intensity:.3f}% of OI (high ≥ 0.10%)",
        "",
        "LIQUIDITY & ORDER FLOW",
        f"{book_icon} Book imbalance: {book_imbalance:+.1f}% — {book_label} (directional at ±15%)",
        f"🟢 Buy wall: {price_text(getattr(context, 'bid_wall_price', 0.0))} — "
        f"{getattr(context, 'bid_wall_strength', 0.0):.1f}× median level (significant ≥ 3×)",
        f"🔴 Sell wall: {price_text(getattr(context, 'ask_wall_price', 0.0))} — "
        f"{getattr(context, 'ask_wall_strength', 0.0):.1f}× median level (significant ≥ 3×)",
        f"{taker_icon} Recent taker flow: {taker_ratio:.1f}% buys — {taker_label} "
        f"(directional beyond 57.5% / below 42.5%)",
        "",
        "LARGE TRADE FLOW",
        f"🔵 Dynamic large-trade threshold: {price_text(getattr(context, 'large_trade_threshold', 0.0))} "
        f"(top 1% or ≥ 5× average trade)",
        f"🔵 Large trades: {getattr(context, 'large_trade_count', 0)} — "
        f"{getattr(context, 'large_flow_share', 0.0):.1f}% of sampled flow (concentrated ≥ 20%)",
        f"{'🟢' if getattr(context, 'largest_trade_side', 'UNKNOWN') == 'BUY' else '🔴'} Largest trade: "
        f"{price_text(getattr(context, 'largest_trade_value', 0.0))} {getattr(context, 'largest_trade_side', 'UNKNOWN')} — "
        f"{getattr(context, 'largest_trade_multiple', 0.0):.1f}× average (exceptional ≥ 10×)",
        f"{large_flow_icon} Large-trade net flow: {large_flow:+.1f}% — {large_flow_label} (directional at ±30%)",
        f"🔵 Derivatives source: {getattr(context, 'derivatives_provider', 'UNKNOWN')}",
        "",
        f"{direction_emoji('LONG' if getattr(context, 'macro_bias', 'NEUTRAL') == 'BULLISH' else 'SHORT' if getattr(context, 'macro_bias', 'NEUTRAL') == 'BEARISH' else 'WAIT')} Macro bias: {getattr(context, 'macro_bias', 'NEUTRAL')} "
        f"({getattr(context, 'macro_score', 0):+.1f}; bullish ≥ +8, bearish ≤ -8)",
        f"🟡 Context adjustment: {getattr(context, 'score_adjustment', 0):+.1f} (meaningful at ±5; capped at ±30)",
        "",
        f"{'🟢' if getattr(context, 'news_label', 'NEUTRAL') == 'BULLISH' else '🔴' if getattr(context, 'news_label', 'NEUTRAL') == 'BEARISH' else '🟡'} "
        f"News intelligence 24h: {getattr(context, 'news_label', 'NEUTRAL')} "
        f"({getattr(context, 'news_score', 0):+.0f}/6; score impact capped at ±3)",
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
    profile = get_profile(get_trading_horizon(), get_risk_style())
    timeframe_weights = profile.weights
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
    active_volume_percent = profile.volume_confirmation / 1.5 * 100.0
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
        f"Trend: {abs(trend):.0f}% {_bias_label(trend)} (directional at ±18%)",
        f"Momentum: {abs(momentum):.0f}% {_bias_label(momentum)} (directional at ±18%)",
        "",
        f"Macro: {abs(macro):.0f}% {_bias_label(macro)} (directional at ±18%)",
        f"Liquidity position: {abs(liquidity):.0f}% {_bias_label(liquidity)} (directional at ±18%)",
        "",
        f"Volume activity: {volume:.0f}% (active ≥ {active_volume_percent:.0f}% for {profile.horizon.lower()} {profile.risk_style.lower()}; strong ≥ 100%)",
        f"Timeframe alignment: {alignment:.0f}% (strong ≥ 70%; mixed < 50%)",
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
        if state.get("exit_warning"):
            progress.append("40% reduction warning sent")
        if int(state.get("exit_risk_stage", 0)):
            progress.append(f"exit-risk stage {int(state.get('exit_risk_stage', 0))}/3")
        lines.extend(
            [
                "",
                f"{symbol} — {state.get('side', 'UNKNOWN')}",
                f"Setup type: {'TACTICAL ' + str(state.get('origin_interval', '')) if state.get('tactical') else 'CONFIRMED TREND'}",
                f"Entry: {price_text(plan.get('entry_low'))} to {price_text(plan.get('entry_high'))}",
                f"Stop: {price_text(plan.get('stop_loss'))}",
                f"Managed protection: {price_text(state.get('management_stop', plan.get('stop_loss')))}",
                f"TP1: {price_text(plan.get('tp1'))}",
                f"TP2: {price_text(plan.get('tp2'))}",
                f"TP3: {price_text(plan.get('tp3'))}",
                f"Progress: {', '.join(progress) if progress else 'Entry active; no milestone recorded'}",
                f"Latest exit risks: {', '.join(state.get('last_exit_reasons', [])) if state.get('last_exit_reasons') else 'none detected'}",
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
    profile = get_profile(get_trading_horizon(), get_risk_style())
    adjusted = float(getattr(context, "adjusted_score", signal.score))
    status, status_detail = execution_status(signal)
    session = get_session_context()
    special_event = get_special_market_event()
    economic = get_economic_risk()
    lunar = get_lunar_context()
    reasons = list(signal.supporting_reasons)
    warnings = list(signal.warnings)
    if context is not None:
        reasons.extend(getattr(context, "reasons", []))
        reasons.extend(getattr(context, "macro_reasons", []))
        warnings.extend(getattr(context, "warnings", []))

    lines = [
        f"📡 {signal.symbol} MARKET SCAN",
        "",
        f"PROFILE: {profile.horizon} / {profile.risk_style}",
        f"Thresholds: setup ±{profile.watch_threshold:.0f}; confirmed ±{profile.confirmed_threshold:.0f}; strong ±{profile.strong_threshold:.0f}",
        "",
        f"Price: {price_text(signal.price)} (reference: planned entry zone when a setup exists)",
        f"{direction_emoji(signal.direction)} Direction: {signal.direction}",
        "",
        f"Technical score: {signal.score:+.1f} (LONG ≥ +{profile.watch_threshold:.0f}; SHORT ≤ -{profile.watch_threshold:.0f})",
        f"Adjusted score: {adjusted:+.1f} (LONG ≥ +{profile.watch_threshold:.0f}; SHORT ≤ -{profile.watch_threshold:.0f})",
        f"Confidence: {min(95, int(abs(adjusted)))}% (setup ≥ {profile.watch_threshold:.0f}%; confirmed ≥ {profile.confirmed_threshold:.0f}%; strong ≥ {profile.strong_threshold:.0f}%)",
        "",
        f"Grade: {get_signal_grade(signal)} (A+ ≥ 95; A ≥ 90; B ≥ 80; C ≥ 70; D < 70)",
        f"Readiness: {get_readiness_label(signal)} (BUILDING ≥ {profile.watch_threshold * 0.80:.0f}; NEAR TRIGGER ≥ {profile.confirmed_threshold:.0f}; HIGH QUALITY ≥ {profile.strong_threshold:.0f})",
        "",
        f"Execution status: {status}",
        f"Action: {status_detail}",
        "",
        "SESSION CONTEXT",
        f"{session.label}: {session.detail}",
        f"Caution: {session.caution}",
        f"Special timing: {special_event or 'No weekly, month-end or quarter-end close event.'}",
        "",
        "ECONOMIC CALENDAR",
        f"{'🔴' if economic.block_new_entries else '🟡' if economic.status == 'UPCOMING' else '🟢'} Risk: {economic.status}",
        economic.detail,
        "",
        f"{'🌑' if lunar.phase == 'NEW MOON' else '🌕'} Lunar: {lunar.label} — {lunar.detail}",
        "",
        "TIMEFRAMES",
        *format_timeframes(signal),
        "",
        "EARLY OPPORTUNITY RADAR",
        *build_early_opportunity_radar(signal, context),
        "",
        "CONFIDENCE BREAKDOWN",
        *build_confidence_breakdown(signal, context),
        "",
        "MARKET CONTEXT",
        *format_market_context(context),
    ]
    if signal.trade_plan:
        lines.extend(["", "TRADE MAP", *format_trade_plan(signal.trade_plan)])
    balanced_evidence = build_balanced_evidence(signal)
    if balanced_evidence:
        lines.extend(["", "BULLISH / BEARISH EVIDENCE", *balanced_evidence])
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
    economic = get_economic_risk()
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
        "EXIT_WATCH": f"👁 {signal.symbol} SMART EXIT WATCH",
        "EXIT_40": f"⚠️ {signal.symbol} SMART EXIT — REDUCE 40%",
        "EXIT_HIGH": f"🚨 {signal.symbol} SMART EXIT — HIGH RISK",
        "PROTECTED_STOP": f"🛡 {signal.symbol} PROTECTED STOP REACHED",
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
        f"Economic risk: {economic.status}",
        "",
        "CONFIDENCE BREAKDOWN",
        *build_confidence_breakdown(signal, context),
    ]
    if note:
        lines.extend(["", f"Action: {note}"])
    if plan and alert_type not in {"INVALIDATED", "PROTECTED_STOP", "EXIT"}:
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
        "LIQUIDATION_WAVE": f"🌊 {signal.symbol} LIQUIDATION WAVE",
        "ORDER_FLOW_SHIFT": f"📚 {signal.symbol} ORDER-FLOW SHIFT",
        "LARGE_TRADE_FLOW": f"🐋 {signal.symbol} LARGE-TRADE FLOW",
    }
    oi_value = float(derivatives.get("open_interest_value", 0.0))
    liquidation_total = float(derivatives.get("long_liquidations_1h", 0.0)) + float(derivatives.get("short_liquidations_1h", 0.0))
    liquidation_intensity = liquidation_total / oi_value * 100.0 if oi_value else 0.0
    impact_lines: list[str] = []
    if action.startswith("SUPPORTS "):
        impact_lines = [f"Decision impact: 🟢 {action.split(':', 1)[0]}", ""]
    elif action.startswith("OPPOSES "):
        impact_lines = [f"Decision impact: 🔴 {action.split(':', 1)[0]}", ""]
    return "\n".join(
        [
            headings.get(alert_type, f"⚠️ {signal.symbol} DERIVATIVES ALERT"),
            "",
            *impact_lines,
            f"Price: {price_text(signal.price)}",
            f"Technical direction: {signal.direction}",
            f"Funding: {float(derivatives.get('funding_rate', 0.0)) * 100:+.4f}% "
            f"({derivatives.get('funding_label', 'UNKNOWN')})",
            f"Open interest: ${float(derivatives.get('open_interest_value', 0.0)):,.0f}",
            f"OI change: {float(derivatives.get('open_interest_change_5m', 0.0)):+.2f}% (5m), "
            f"{float(derivatives.get('open_interest_change_1h', 0.0)):+.2f}% (1h)",
            f"Liquidations 1h: longs ${float(derivatives.get('long_liquidations_1h', 0.0)):,.0f} / "
            f"shorts ${float(derivatives.get('short_liquidations_1h', 0.0)):,.0f}",
            f"Liquidation pressure: {derivatives.get('liquidation_pressure', 'UNAVAILABLE')}",
            f"Intensity: {liquidation_intensity:.3f}% of OI (high ≥ 0.10%)",
            f"Book imbalance: {float(derivatives.get('orderbook_imbalance', 0.0)):+.1f}% (directional at ±15%)",
            f"Recent taker buys: {float(derivatives.get('taker_buy_ratio', 50.0)):.1f}% "
            f"(directional beyond 57.5% / below 42.5%)",
            f"Largest trade: {price_text(derivatives.get('largest_trade_value', 0.0))} "
            f"{derivatives.get('largest_trade_side', 'UNKNOWN')} — "
            f"{float(derivatives.get('largest_trade_multiple', 0.0)):.1f}× average",
            f"Large-trade net flow: {float(derivatives.get('large_flow_imbalance', 0.0)):+.1f}% "
            f"across {float(derivatives.get('large_flow_share', 0.0)):.1f}% of sampled value",
            f"Provider: {derivatives.get('provider', 'UNKNOWN')}",
            "",
            f"Action: {action}",
            "",
            "Decision support only. Confirm price structure before acting.",
        ]
    )


def evaluate_economic_alert() -> AlertDecision:
    risk = get_economic_risk()
    event = risk.event
    if event is None or risk.status == "CLEAR":
        return AlertDecision(False, "NONE", "MARKET", "", "No nearby economic event")

    key = make_alert_key("MARKET", "ECONOMIC_EVENT", f"{event.name}:{risk.status}")
    if not alert_allowed(key, ECONOMIC_ALERT_COOLDOWN_SECONDS):
        return AlertDecision(False, "NONE", "MARKET", "", "Economic alert cooldown active")

    mark_alert_sent(key)
    heading = "⚡ EVENT RELEASE MODE" if risk.status in {"RELEASE IMPULSE", "EVENT OPPORTUNITY"} else "🚨 HIGH-IMPACT EVENT SAFETY" if risk.block_new_entries else "📅 HIGH-IMPACT EVENT AHEAD"
    risk_label = (
        "🔴 RELEASE IMPULSE — OBSERVE, DO NOT CHASE"
        if risk.status == "RELEASE IMPULSE" else
        "🔴 PRE-RELEASE SAFETY — STANDARD ENTRIES PAUSED; EVENT MODE ARMED"
        if risk.block_new_entries else
        "🟢 EVENT OPPORTUNITY WINDOW — CONFIRMATION REQUIRED"
        if risk.status == "EVENT OPPORTUNITY" else
        "🟡 EVENT APPROACHING — CAUTION"
    )
    action = (
        "Observe the impulse. Wait for a candle close and level retest; do not chase the first move."
        if risk.status == "RELEASE IMPULSE"
        else "Standard entries are paused, but armed plans remain active for the post-release opportunity."
        if risk.block_new_entries
        else "A dedicated event setup may trigger only with price structure, candle, volume and flow confirmation."
        if risk.status == "EVENT OPPORTUNITY"
        else "Avoid chasing and be cautious with fresh exposure as the release approaches."
    )
    message = "\n".join(
        [
            heading,
            "",
            f"Event: {event.name}",
            f"Time: {format_event_time(event)}",
            f"Risk status: {risk_label}",
            f"Source: {event.source}",
            "",
            risk.detail,
            f"Action: {action}",
        ]
    )
    return AlertDecision(True, "ECONOMIC_EVENT", "MARKET", message, risk.detail)


def evaluate_session_alert() -> AlertDecision:
    session = get_session_context()
    special_event = get_special_market_event()
    important_sessions = {"LONDON OPEN", "US PREMARKET", "US OPEN", "US POWER HOUR", "ASIA OPEN", "WEEKEND"}
    if session.label not in important_sessions and not special_event:
        return AlertDecision(False, "NONE", "MARKET", "", "No major timing transition")

    identity = special_event or session.label
    key = make_alert_key("MARKET", "SESSION_TIMING", identity)
    if not alert_allowed(key, SESSION_ALERT_COOLDOWN_SECONDS):
        return AlertDecision(False, "NONE", "MARKET", "", "Session alert cooldown active")
    mark_alert_sent(key)
    message = "\n".join(
        [
            "🕒 MARKET TIMING ALERT",
            "",
            f"Session: {session.label}",
            session.detail,
            "",
            f"Special timing: {special_event or 'No special close event.'}",
            f"Action: {session.caution}",
        ]
    )
    return AlertDecision(True, "SESSION_TIMING", "MARKET", message, identity)


def evaluate_news_alert(data: dict[str, Any]) -> AlertDecision:
    now = time.time()
    candidates = sorted(data.get("recent_items", []), key=lambda item: item["published_at"], reverse=True)
    for item in candidates:
        item_id = str(item.get("id", ""))
        age = now - item["published_at"].timestamp()
        if not item_id or item_id in seen_news_ids or age > 30 * 60:
            continue
        seen_news_ids.add(item_id)
        if item.get("label") == "NEUTRAL":
            continue
        icon = "🟢" if item["label"] == "BULLISH" else "🔴"
        source_note = "THIRD-PARTY ARCHIVE" if item.get("source_type") == "THIRD_PARTY_ARCHIVE" else "OFFICIAL"
        message = "\n".join([
            f"{icon} NEWS INTELLIGENCE ALERT",
            "",
            f"Source: {item['source']}",
            f"Source type: {source_note}",
            f"Classification: {item['label']} ({int(item['score']):+d}; maximum item weight ±3)",
            f"Headline: {item['title']}",
            f"Link: {item['link']}",
            "",
            "Action: Reassess market structure and liquidity. A headline never creates an entry by itself.",
        ])
        return AlertDecision(True, "NEWS", "MARKET", message, "New relevant market headline")
    return AlertDecision(False, "NONE", "MARKET", "", "No new relevant market headline")


def _large_trade_zone_context(symbol: str, price: float) -> tuple[bool, str, float, str]:
    """Return whether exceptional flow is close enough to matter to a planned trade."""
    symbol = symbol.upper()
    if setup_states.get(symbol):
        return True, "active managed setup", 0.0, str(setup_states[symbol].get("side", "")).upper()

    candidates: list[tuple[float, str, str]] = []
    for side, plan in get_armed_trade_plans().get(symbol, {}).items():
        try:
            zone_low = float(plan["zone_low"])
            zone_high = float(plan["zone_high"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = 0.0 if zone_low <= price <= zone_high else min(
            abs(price - zone_low), abs(price - zone_high)
        ) / max(price, 1e-9) * 100.0
        candidates.append((distance, f"armed {side.upper()} zone", side.upper()))

    for opportunity in get_early_opportunities().values():
        if str(opportunity.get("symbol", "")).upper() != symbol:
            continue
        try:
            zone_low = float(opportunity["zone_low"])
            zone_high = float(opportunity["zone_high"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = 0.0 if zone_low <= price <= zone_high else min(
            abs(price - zone_low), abs(price - zone_high)
        ) / max(price, 1e-9) * 100.0
        planned_side = str(opportunity.get("side", "")).upper()
        label = f"{opportunity.get('interval', '?')} {planned_side} radar zone"
        candidates.append((distance, label, planned_side))

    if not candidates:
        return False, "no active decision zone", float("inf"), ""
    distance, label, planned_side = min(candidates)
    return distance <= 0.50, label, distance, planned_side


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
    long_liquidations = float(derivatives.get("long_liquidations_1h", 0.0))
    short_liquidations = float(derivatives.get("short_liquidations_1h", 0.0))
    liquidation_total = long_liquidations + short_liquidations
    book_imbalance = float(derivatives.get("orderbook_imbalance", 0.0))
    taker_flow_imbalance = float(derivatives.get("taker_flow_imbalance", 0.0))
    large_flow_share = float(derivatives.get("large_flow_share", 0.0))
    large_flow_imbalance = float(derivatives.get("large_flow_imbalance", 0.0))
    largest_trade_multiple = float(derivatives.get("largest_trade_multiple", 0.0))
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

    liquidation_intensity = liquidation_total / float(derivatives.get("open_interest_value", 0.0)) * 100.0 if float(derivatives.get("open_interest_value", 0.0)) else 0.0
    if liquidation_intensity >= 0.10:
        key = make_alert_key(symbol, "LIQUIDATION_WAVE")
        if alert_allowed(key, DERIVATIVES_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            pressure = derivatives.get("liquidation_pressure", "TWO-WAY")
            action = (
                f"A live {pressure.lower()} liquidation wave is underway. Avoid entering the impulse; wait for the forced flow to settle and retest."
            )
            return AlertDecision(
                True,
                "LIQUIDATION_WAVE",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "LIQUIDATION_WAVE", action),
                "Live one-hour liquidations crossed the alert threshold",
            )

    aligned_order_flow = (
        (book_imbalance >= 25.0 and taker_flow_imbalance >= 25.0)
        or (book_imbalance <= -25.0 and taker_flow_imbalance <= -25.0)
    )
    zone_relevant, zone_label, zone_distance, planned_side = _large_trade_zone_context(symbol, signal.price)
    if aligned_order_flow and zone_relevant:
        flow_side = "BUY" if book_imbalance > 0 else "SELL"
        key = make_alert_key(symbol, "ORDER_FLOW_SHIFT", flow_side)
        if alert_allowed(key, ORDER_FLOW_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            side = "buying" if book_imbalance > 0 else "selling"
            proximity = "during an active managed setup" if zone_label == "active managed setup" else (
                f"while price is {zone_distance:.2f}% from the {zone_label}"
            )
            supportive = (flow_side == "BUY" and planned_side == "LONG") or (flow_side == "SELL" and planned_side == "SHORT")
            relationship = f"SUPPORTS {planned_side}" if supportive else f"OPPOSES {planned_side}"
            guidance = (
                "Use it as supporting evidence only; still require the planned price and candle trigger."
                if supportive else
                "Treat it as a breakout/invalidation warning and do not enter the planned direction until flow changes."
            )
            action = f"{relationship}: order-book depth and recent aggressive {side} agree {proximity}. {guidance}"
            return AlertDecision(
                True,
                "ORDER_FLOW_SHIFT",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "ORDER_FLOW_SHIFT", action),
                "Order-book depth and taker flow aligned strongly",
            )

    concentrated_large_flow = (
        large_flow_share >= 15.0
        and abs(large_flow_imbalance) >= 60.0
        and largest_trade_multiple >= 10.0
    )
    if concentrated_large_flow and zone_relevant:
        side = "BUY" if large_flow_imbalance > 0 else "SELL"
        key = make_alert_key(symbol, "LARGE_TRADE_FLOW", side)
        if alert_allowed(key, LARGE_TRADE_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            proximity = "during an active managed setup" if zone_label == "active managed setup" else (
                f"while price is {zone_distance:.2f}% from the {zone_label}"
            )
            supportive = (side == "BUY" and planned_side == "LONG") or (side == "SELL" and planned_side == "SHORT")
            relationship = f"SUPPORTS {planned_side}" if supportive else f"OPPOSES {planned_side}"
            guidance = (
                "Use it as supporting flow evidence, not a standalone entry."
                if supportive else
                "Treat it as a breakout/invalidation warning; do not call it confirmation for the planned trade."
            )
            action = f"{relationship}: unusually concentrated large {side.lower()} trades appeared {proximity}. {guidance} Large traders can hedge or reverse."
            return AlertDecision(
                True,
                "LARGE_TRADE_FLOW",
                symbol,
                build_derivatives_alert_message(signal, derivatives, "LARGE_TRADE_FLOW", action),
                "Exceptional large trades became directionally concentrated",
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
    economic = get_economic_risk()
    session = get_session_context()

    if state:
        plan = state["plan"]
        side = state["side"]
        price = signal.price
        management_stop = float(state.get("management_stop", plan.stop_loss))
        if (side == "LONG" and price <= management_stop) or (side == "SHORT" and price >= management_stop):
            signal_id = str(state.get("signal_id", ""))
            if signal_id:
                update_signal_performance(
                    signal_id,
                    status="WON" if state.get("tp1") else "LOST",
                    closed_at=time.time(),
                )
            _clear_setup(symbol)
            alert_type = "PROTECTED_STOP" if state.get("tp1") or state.get("breakeven") else "INVALIDATED"
            reason = "Protected stop reached" if alert_type == "PROTECTED_STOP" else "Stop or invalidation reached"
            return _decision(signal, context, alert_type, reason, f"Exit the remaining managed position; protection at {price_text(management_stop)} was reached.", MANAGEMENT_COOLDOWN_SECONDS)
        if side == "LONG":
            if price >= plan.tp3:
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True, tp2_hit=True, tp3_hit=True, closed_at=time.time())
                _clear_setup(symbol)
                return _decision(signal, context, "TP3", "Final target reached", "Close the remaining 40% and record the completed setup.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.tp2 and not state.get("tp2"):
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True, tp2_hit=True)
                state["tp2"] = True
                state["management_stop"] = plan.tp1
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP2", "Second target reached", f"Consider taking another 30% and protecting the remainder near TP1 at {price_text(plan.tp1)}.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.tp1 and not state.get("tp1"):
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True)
                state["tp1"] = True
                state["breakeven"] = True
                state["management_stop"] = (plan.entry_low + plan.entry_high) / 2.0
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP1", "First target reached", f"Consider taking 30% and protecting the remainder near breakeven at {price_text(state['management_stop'])}.", MANAGEMENT_COOLDOWN_SECONDS)
            if price >= plan.entry_high + plan.risk_per_unit * 0.75 and not state.get("breakeven"):
                state["breakeven"] = True
                state["management_stop"] = (plan.entry_low + plan.entry_high) / 2.0
                _persist_setup(symbol, state)
                return _decision(signal, context, "BREAKEVEN", "Trade moved in favor", f"Consider protecting near breakeven at {price_text(state['management_stop'])}, after accounting for fees.", MANAGEMENT_COOLDOWN_SECONDS)
            tactical_analysis = signal.analyses.get(str(state.get("origin_interval", ""))) if state.get("tactical") else None
            reversed_now = tactical_analysis.score <= -get_profile(get_trading_horizon(), get_risk_style()).watch_threshold if tactical_analysis is not None else adjusted < -20
            if reversed_now:
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON" if state.get("tp1") else "EXITED", closed_at=time.time())
                _clear_setup(symbol)
                return _decision(signal, context, "EXIT", "Direction reversed", "The model turned materially bearish; reassess or exit the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)
        else:
            if price <= plan.tp3:
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True, tp2_hit=True, tp3_hit=True, closed_at=time.time())
                _clear_setup(symbol)
                return _decision(signal, context, "TP3", "Final target reached", "Close the remaining 40% and record the completed setup.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.tp2 and not state.get("tp2"):
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True, tp2_hit=True)
                state["tp2"] = True
                state["management_stop"] = plan.tp1
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP2", "Second target reached", f"Consider taking another 30% and protecting the remainder near TP1 at {price_text(plan.tp1)}.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.tp1 and not state.get("tp1"):
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON", tp1_hit=True)
                state["tp1"] = True
                state["breakeven"] = True
                state["management_stop"] = (plan.entry_low + plan.entry_high) / 2.0
                _persist_setup(symbol, state)
                return _decision(signal, context, "TP1", "First target reached", f"Consider taking 30% and protecting the remainder near breakeven at {price_text(state['management_stop'])}.", MANAGEMENT_COOLDOWN_SECONDS)
            if price <= plan.entry_low - plan.risk_per_unit * 0.75 and not state.get("breakeven"):
                state["breakeven"] = True
                state["management_stop"] = (plan.entry_low + plan.entry_high) / 2.0
                _persist_setup(symbol, state)
                return _decision(signal, context, "BREAKEVEN", "Trade moved in favor", f"Consider protecting near breakeven at {price_text(state['management_stop'])}, after accounting for fees.", MANAGEMENT_COOLDOWN_SECONDS)
            tactical_analysis = signal.analyses.get(str(state.get("origin_interval", ""))) if state.get("tactical") else None
            reversed_now = tactical_analysis.score >= get_profile(get_trading_horizon(), get_risk_style()).watch_threshold if tactical_analysis is not None else adjusted > 20
            if reversed_now:
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], status="WON" if state.get("tp1") else "EXITED", closed_at=time.time())
                _clear_setup(symbol)
                return _decision(signal, context, "EXIT", "Direction reversed", "The model turned materially bullish; reassess or exit the remaining position.", MANAGEMENT_COOLDOWN_SECONDS)

        if context is not None:
            adverse: list[str] = []
            funding = float(getattr(context, "funding_rate", 0.0))
            oi_change = float(getattr(context, "open_interest_change_1h", 0.0))
            taker_flow = float(getattr(context, "taker_flow_imbalance", 0.0))
            large_flow = float(getattr(context, "large_flow_imbalance", 0.0))
            macro_bias = str(getattr(context, "macro_bias", "NEUTRAL"))
            news_label = str(getattr(context, "news_label", "NEUTRAL"))
            if (side == "LONG" and funding >= 0.0005) or (side == "SHORT" and funding <= -0.0005):
                adverse.append("funding is crowded against the position")
            if oi_change >= 5.0 and ((side == "LONG" and taker_flow <= -15.0) or (side == "SHORT" and taker_flow >= 15.0)):
                adverse.append("open interest is expanding with adverse taker flow")
            if (side == "LONG" and taker_flow <= -15.0) or (side == "SHORT" and taker_flow >= 15.0):
                adverse.append("taker flow has turned against the position")
            if (side == "LONG" and large_flow <= -30.0) or (side == "SHORT" and large_flow >= 30.0):
                adverse.append("large-trade flow has turned against the position")
            if (side == "LONG" and macro_bias == "BEARISH") or (side == "SHORT" and macro_bias == "BULLISH"):
                adverse.append("macro bias opposes the position")
            if (side == "LONG" and news_label == "BEARISH") or (side == "SHORT" and news_label == "BULLISH"):
                adverse.append("news intelligence opposes the position")
            origin_interval = str(state.get("origin_interval", ""))
            technical = signal.analyses.get(origin_interval) if origin_interval else None
            if technical is None:
                profile = get_profile(get_trading_horizon(), get_risk_style())
                technical = next((signal.analyses.get(interval) for interval in profile.primary_timeframes if signal.analyses.get(interval) is not None), None)
            if technical is not None:
                clues = [
                    str(value).lower()
                    for value in (
                        list(getattr(technical, "candle_patterns", []))
                        + list(getattr(technical, "divergences", []))
                    )
                ]
                bearish_turn = (
                    any("bearish" in clue for clue in clues)
                    or (float(getattr(technical, "macd", 0.0)) < float(getattr(technical, "macd_signal", 0.0)) and float(getattr(technical, "macd_histogram", 0.0)) < 0)
                    or float(getattr(technical, "rsi_6", 50.0)) < float(getattr(technical, "rsi_12", 50.0)) < float(getattr(technical, "rsi_24", 50.0))
                )
                bullish_turn = (
                    any("bullish" in clue for clue in clues)
                    or (float(getattr(technical, "macd", 0.0)) > float(getattr(technical, "macd_signal", 0.0)) and float(getattr(technical, "macd_histogram", 0.0)) > 0)
                    or float(getattr(technical, "rsi_6", 50.0)) > float(getattr(technical, "rsi_12", 50.0)) > float(getattr(technical, "rsi_24", 50.0))
                )
                if (side == "LONG" and bearish_turn) or (side == "SHORT" and bullish_turn):
                    adverse.append("the origin timeframe shows a technical reversal")
            current_stage = int(state.get("exit_risk_stage", 1 if state.get("exit_warning") else 0))
            target_stage = 3 if len(adverse) >= 5 else 2 if len(adverse) >= 3 else 1 if len(adverse) >= 2 else 0
            if target_stage > current_stage:
                state["exit_risk_stage"] = target_stage
                state["exit_warning"] = target_stage >= 2
                state["last_exit_reasons"] = adverse[:6]
                _persist_setup(symbol, state)
                detail = "; ".join(adverse[:6])
                if state.get("signal_id"):
                    update_signal_performance(state["signal_id"], exit_reason=detail)
                if target_stage == 1:
                    return _decision(signal, context, "EXIT_WATCH", "Exit risk is building", f"Two adverse factors are developing: {detail}. Monitor closely and avoid adding risk.", MANAGEMENT_COOLDOWN_SECONDS)
                if target_stage == 2:
                    return _decision(signal, context, "EXIT_40", "Multiple exit risks aligned", f"Consider reducing 40% and tightening protection because {detail}.", MANAGEMENT_COOLDOWN_SECONDS)
                return _decision(signal, context, "EXIT_HIGH", "Exit risk escalated", f"Five or more adverse factors now align: {detail}. Consider protecting or closing the remainder rather than waiting for the original stop.", MANAGEMENT_COOLDOWN_SECONDS)

    if prior_score is not None and abs(adjusted - prior_score) >= RAPID_SCORE_CHANGE:
        return _decision(signal, context, "RAPID_CHANGE", "Rapid score change", "Pause and reassess; market conditions changed quickly.", RAPID_CHANGE_COOLDOWN_SECONDS)

    status, note = execution_status(signal)
    if status == "DO NOT CHASE":
        return _decision(signal, context, "DO_NOT_CHASE", "Price left the entry zone", note, DO_NOT_CHASE_COOLDOWN_SECONDS)
    if signal.trade_plan is None or signal.direction == "WAIT":
        return AlertDecision(False, "NONE", symbol, "", "No actionable setup")
    if economic.block_new_entries:
        key = make_alert_key(symbol, "EVENT_RISK", economic.event.name if economic.event else "MACRO")
        if alert_allowed(key, ECONOMIC_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            return AlertDecision(
                True,
                "EVENT_RISK",
                symbol,
                build_alert_message(signal, "PREPARE", context, economic.detail),
                "High-impact economic event blocks new entries",
            )
        return AlertDecision(False, "NONE", symbol, "", "Economic event risk blocks entry")
    if session.label == "US OPEN":
        key = make_alert_key(symbol, "SESSION_RISK", session.label)
        if alert_allowed(key, SESSION_ALERT_COOLDOWN_SECONDS):
            mark_alert_sent(key)
            return AlertDecision(
                True,
                "SESSION_RISK",
                symbol,
                build_alert_message(signal, "PREPARE", context, "US opening volatility temporarily blocks new entries. Wait for the opening range and retest."),
                "US opening volatility blocks a new entry",
            )
        return AlertDecision(False, "NONE", symbol, "", "US open risk blocks entry")
    if previous_hash == current_hash:
        return AlertDecision(False, "NONE", symbol, "", "Duplicate signal")
    if status == "WATCH":
        return _decision(signal, context, "WATCH", "Price approaching planned level", note, WATCH_COOLDOWN_SECONDS)
    if status == "PREPARE" and signal.stage not in {"CONFIRMED", "STRONG"}:
        return _decision(signal, context, "PREPARE", "Price reached entry area", note, PREPARE_COOLDOWN_SECONDS)
    if status == "PREPARE" and signal.stage in {"CONFIRMED", "STRONG"}:
        entry_key = make_alert_key(symbol, "ENTRY", signal.trade_plan.side)
        if not alert_allowed(entry_key, ENTRY_COOLDOWN_SECONDS):
            return AlertDecision(False, "NONE", symbol, "", "Entry alert cooldown active")
        signal_id = record_entry_signal("PROFILE", symbol, signal.trade_plan.side, signal.trade_plan)
        setup_states[symbol] = {
            "side": signal.trade_plan.side,
            "plan": signal.trade_plan,
            "created_at": time.time(),
            "tp1": False,
            "tp2": False,
            "breakeven": False,
            "management_stop": signal.trade_plan.stop_loss,
            "exit_warning": False,
            "exit_risk_stage": 0,
            "last_exit_reasons": [],
            "tactical": False,
            "origin_interval": "",
            "signal_id": signal_id,
        }
        _persist_setup(symbol, setup_states[symbol])
        return _decision(signal, context, "ENTRY", "Setup confirmed at planned level", "Entry is confirmed near the planned zone. Avoid entering outside the displayed range.", ENTRY_COOLDOWN_SECONDS)
    return AlertDecision(False, "NONE", symbol, "", "No new alert condition")
