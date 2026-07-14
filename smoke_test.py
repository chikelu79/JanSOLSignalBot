"""Offline structural smoke test. It does not contact Binance or Telegram."""

import os
os.environ["JANBOT_DISABLE_PERSISTENT_ALERTS"] = "1"

from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot_state import get_active_setups, get_early_opportunities, remove_active_setup, remove_armed_trade_plans, remove_early_opportunity, set_active_setup, set_armed_trade_plans
from economic_calendar import build_calendar_message, get_economic_risk, get_profile_economic_risk
from news_intelligence import build_news_message
from lunar_context import get_lunar_context
from market_context import build_market_context, calculate_coinbase_premium
from notifier import (
    build_active_setups_message,
    build_balanced_evidence,
    build_confidence_breakdown,
    build_early_opportunity_radar,
    build_radar_stats_message,
    build_scan_message,
    build_success_stats_message,
    build_trade_dashboard,
    create_structural_trade_plans,
    evaluate_derivatives_alert,
    evaluate_early_opportunity_alert,
    evaluate_armed_trade_plan_alert,
    evaluate_economic_alert,
    evaluate_session_alert,
    evaluate_signal_alert,
    reversal_candle_confirmed,
    setup_states,
)
from trading_profile import get_profile, mfi_reversal_min_change
from strategy import MarketSignal, TradePlan, collect_supporting_reasons, create_trade_plan
from session_context import get_session_context, get_special_market_event
from trading_profile import estimate_position, get_profile


def main() -> None:
    success_stats = build_success_stats_message()
    assert "SIGNAL SUCCESS STATISTICS" in success_stats
    assert "Success rate:" in success_stats
    assert "Win = TP1 reached before the original stop." in success_stats
    normalized_premium = calculate_coinbase_premium(100.0, 100.1, 0.999)
    assert abs(normalized_premium - 0.0001) < 0.001
    conservative_scalp = get_profile("SCALPING", "CONSERVATIVE")
    assert conservative_scalp.watch_threshold == 68.0
    assert conservative_scalp.weights["5m"] == 0.30
    aggressive_swing = get_profile("SWING", "AGGRESSIVE")
    assert aggressive_swing.confirmed_threshold == 66.0
    assert aggressive_swing.primary_timeframes == ("1h", "4h")
    position = estimate_position("LONG", 75.0, 500.0, 5.0, 72.0)
    assert position["notional"] == 2500.0
    assert round(float(position["liquidation"]), 3) == 60.375
    assert round(float(position["stop_loss"]), 2) == 100.0
    assert float(position["recommended_max_leverage"]) >= 1.0
    dangerous = estimate_position("LONG", 75.0, 500.0, 20.0, 70.0)
    assert dangerous["liquidation_before_stop"] is True
    assert float(dangerous["recommended_max_leverage"]) < 20.0
    mixed_analyses = {
        interval: SimpleNamespace(score=score, reasons=[f"reason {number}" for number in range(3)])
        for interval, score in {"5m": 20, "15m": 15, "1h": -25, "4h": -30, "8h": -35, "1d": -40}.items()
    }
    mixed_reasons = collect_supporting_reasons(mixed_analyses, 0.0)
    assert len({reason.split(":", 1)[0] for reason in mixed_reasons}) >= 4
    eastern = ZoneInfo("America/New_York")
    upcoming = get_economic_risk(datetime(2026, 7, 13, 20, 0, tzinfo=eastern))
    blocked = get_economic_risk(datetime(2026, 7, 14, 8, 0, tzinfo=eastern))
    post_release = get_economic_risk(datetime(2026, 7, 14, 9, 15, tzinfo=eastern))
    day_blackout = get_profile_economic_risk(datetime(2026, 7, 14, 6, 36, tzinfo=eastern), "DAY", "BALANCED")
    scalp_caution = get_profile_economic_risk(datetime(2026, 7, 14, 6, 36, tzinfo=eastern), "SCALPING", "AGGRESSIVE")
    assert day_blackout.block_new_entries is True
    assert day_blackout.status == "PRE-RELEASE SAFETY"
    assert scalp_caution.block_new_entries is False
    assert upcoming.status == "UPCOMING" and not upcoming.block_new_entries
    assert blocked.status == "HIGH RISK" and blocked.block_new_entries
    assert post_release.status == "POST-RELEASE" and post_release.block_new_entries
    assert "US Employment / NFP" in build_calendar_message(datetime(2026, 7, 30, 12, 0, tzinfo=eastern))
    economic_alert = evaluate_economic_alert()
    assert economic_alert.alert_type in {"ECONOMIC_EVENT", "NONE"}
    if economic_alert.alert_type == "ECONOMIC_EVENT":
        assert "EVENT APPROACHING — CAUTION" in economic_alert.message or "PRE-RELEASE SAFETY" in economic_alert.message
    lunar = get_lunar_context(datetime(2026, 7, 13, 20, 0, tzinfo=eastern))
    assert lunar.label == "NEAR NEW MOON"
    assert get_session_context(datetime(2026, 7, 14, 9, 20, tzinfo=eastern)).label == "US OPEN"
    assert get_session_context(datetime(2026, 7, 14, 3, 10, tzinfo=eastern)).label == "LONDON OPEN"
    assert get_session_context(datetime(2026, 7, 14, 20, 0, tzinfo=eastern)).label == "ASIA OPEN"
    assert "QUARTER-END" in get_special_market_event(datetime(2026, 9, 30, 15, 0, tzinfo=eastern))
    assert evaluate_session_alert().alert_type in {"SESSION_TIMING", "NONE"}
    news_message = build_news_message({"label": "BEARISH", "score": -2, "relevant_items": [], "errors": []})
    assert "24h bias: BEARISH (-2; capped at ±6)" in news_message
    assert "free third-party archive RSS" in news_message
    structural_analysis = SimpleNamespace(
        atr=2.0,
        support=95.0,
        resistance=105.0,
        ema20=98.0,
        ema50=96.0,
        vwap=97.0,
        bollinger_middle=97.5,
    )
    structural_long = create_trade_plan(
        "LONG",
        100.0,
        {"15m": structural_analysis},
    )
    structural_short = create_trade_plan(
        "SHORT",
        100.0,
        {"15m": structural_analysis},
    )
    assert structural_long is not None and structural_long.entry_high < 100.0
    assert structural_short is not None and structural_short.entry_low > 100.0

    plan = TradePlan(
        side="LONG",
        entry_low=99.0,
        entry_high=100.0,
        stop_loss=97.0,
        invalidation=97.0,
        tp1=103.0,
        tp2=106.0,
        tp3=109.0,
        risk_per_unit=3.0,
        reward_risk_tp1=1.0,
        reward_risk_tp2=2.0,
        reward_risk_tp3=3.0,
    )
    signal = MarketSignal(
        symbol="TESTUSDT",
        direction="LONG",
        stage="WATCH",
        score=65.0,
        confidence=65,
        price=100.5,
        bullish_timeframes=4,
        bearish_timeframes=0,
        neutral_timeframes=2,
        analyses={},
        errors={},
        trade_plan=plan,
        supporting_reasons=["Offline smoke-test reason"],
        warnings=[],
    )
    signal.analyses = {
        "5m": SimpleNamespace(
            score=30.0, previous_macd=-0.2, previous_macd_signal=-0.1,
            macd=0.1, macd_signal=0.0, previous_macd_histogram=-0.1,
            macd_histogram=0.1, previous_rsi=28.0, rsi=32.0,
            previous_rsi_6=40.0, previous_rsi_12=42.0, previous_rsi_24=44.0,
            rsi_6=48.0, rsi_12=45.0, rsi_24=44.0,
            previous_stoch_rsi_k=12.0, previous_stoch_rsi_d=15.0,
            stoch_rsi_k=24.0, stoch_rsi_d=18.0,
            two_back_mfi=35.0, previous_mfi=33.0, mfi=39.0,
            bollinger_width=2.2,
            relative_volume=1.5,
            ema20=74.8, vwap=75.0, support=73.5, resistance=76.0,
        ),
        "1h": SimpleNamespace(score=-35.0),
        "4h": SimpleNamespace(score=-30.0),
    }
    radar = build_early_opportunity_radar(signal)
    assert "EARLY LONG WATCH — COUNTERTREND" in radar[0]
    assert "fresh bullish MACD line cross" in radar[1]
    assert "RSI 6 crossed above RSI 12" in radar[1]
    assert "Stochastic RSI crossed bullish from oversold" in radar[1]
    assert "MFI money flow turned upward" in radar[1]
    assert any("Pattern context: 🔵 COMPRESSION" in line for line in radar)
    assert mfi_reversal_min_change(get_profile("SCALPING", "BALANCED")) == 1.0
    assert mfi_reversal_min_change(get_profile("DAY", "BALANCED")) == 3.0
    assert mfi_reversal_min_change(get_profile("SWING", "BALANCED")) == 5.0
    assert any("Verdict: 🟡 DEVELOPING" in line for line in radar)
    blocked_radar = build_early_opportunity_radar(
        signal,
        {"taker_flow_imbalance": -40.0, "large_flow_imbalance": -50.0},
    )
    assert any("required ≥ 1.38×" in line for line in blocked_radar)
    assert any("Verdict: 🔴 BLOCKED" in line for line in blocked_radar)
    signal.price = 75.1
    early_alert = evaluate_early_opportunity_alert(
        signal,
        derivatives={"taker_flow_imbalance": 20.0, "large_flow_imbalance": 40.0},
    )
    assert early_alert.should_send
    assert early_alert.alert_type == "EARLY_OPPORTUNITY"
    assert "Distance to decision zone" in early_alert.message
    radar_stats = build_radar_stats_message()
    assert "OPPORTUNITY RADAR TRACKING" in radar_stats
    assert "Active watches:" in radar_stats
    assert "Hypothetical 1R reached:" in radar_stats
    assert "Hypothetical 2R reached:" in radar_stats
    assert "Confirmed tactical entries:" in radar_stats
    stored_key = "TESTUSDT:5m:LONG"
    assert stored_key in get_early_opportunities()
    stored_watch = get_early_opportunities()[stored_key]
    assert stored_watch["target_1r"] > stored_watch["zone_high"]
    assert stored_watch["target_2r"] > stored_watch["target_1r"]
    remove_early_opportunity(stored_key)
    signal.symbol = "WEAKUSDT"
    signal.analyses["5m"].relative_volume = 0.5
    weak_countertrend_alert = evaluate_early_opportunity_alert(
        signal,
        derivatives={"taker_flow_imbalance": 20.0, "large_flow_imbalance": 40.0},
    )
    assert not weak_countertrend_alert.should_send
    remove_early_opportunity("WEAKUSDT:5m:LONG")
    signal.symbol = "TESTUSDT"
    tiny_cross = signal.analyses["5m"]
    tiny_cross.previous_macd = tiny_cross.macd = 0.1
    tiny_cross.previous_macd_signal = tiny_cross.macd_signal = 0.0
    tiny_cross.previous_macd_histogram = tiny_cross.macd_histogram = 0.1
    tiny_cross.previous_rsi = tiny_cross.rsi = 50.0
    tiny_cross.previous_rsi_6, tiny_cross.previous_rsi_12 = 50.0, 50.1
    tiny_cross.rsi_6, tiny_cross.rsi_12 = 50.2, 50.0
    tiny_cross.previous_rsi_24 = 50.1
    tiny_cross.rsi_24 = 49.9
    tiny_cross.previous_stoch_rsi_k = tiny_cross.stoch_rsi_k = 50.0
    tiny_cross.previous_stoch_rsi_d = tiny_cross.stoch_rsi_d = 50.0
    tiny_cross.two_back_mfi = tiny_cross.previous_mfi = tiny_cross.mfi = 50.0
    tiny_radar = build_early_opportunity_radar(signal)
    assert not any("RSI 6 crossed" in line or "RSI 12 crossed" in line for line in tiny_radar)
    tiny_cross.two_back_mfi, tiny_cross.previous_mfi, tiny_cross.mfi = 72.0, 70.9, 73.5
    hot_mfi_radar = build_early_opportunity_radar(signal)
    assert not any("MFI money flow turned upward" in line for line in hot_mfi_radar)
    tiny_cross.two_back_mfi, tiny_cross.previous_mfi, tiny_cross.mfi = 69.0, 70.6, 69.1
    weak_mfi_radar = build_early_opportunity_radar(signal)
    assert not any("MFI money flow turned downward" in line for line in weak_mfi_radar)
    signal.analyses = {
        interval: SimpleNamespace(
            support=99.0 - index * 0.5, resistance=102.0 + index * 0.5,
            atr=1.0, relative_volume=1.3, score=25.0 if interval == "15m" else -10.0,
        )
        for index, interval in enumerate(("15m", "1h", "4h"))
    }
    signal.price = 100.5
    trade_dashboard = build_trade_dashboard(
        signal,
        SimpleNamespace(adjusted_score=10.0, taker_flow_imbalance=20.0, large_flow_imbalance=35.0),
    )
    assert "TRADE PLANNER" in trade_dashboard
    assert "Automatic planning:" in trade_dashboard
    assert reversal_candle_confirmed(SimpleNamespace(candle_patterns=["Bullish hammer"], chart_structures=[]), "LONG")
    assert reversal_candle_confirmed(SimpleNamespace(candle_patterns=["Bearish engulfing"], chart_structures=[]), "SHORT")
    assert not reversal_candle_confirmed(SimpleNamespace(candle_patterns=["Doji"], chart_structures=[]), "LONG")
    assert "🎯 FOCUS:" in trade_dashboard
    assert "Directional agreement:" in trade_dashboard or "NO CLEAR DIRECTION" in trade_dashboard
    if "NO CLEAR DIRECTION" not in trade_dashboard:
        assert "Entry readiness:" in trade_dashboard and "checks passed" in trade_dashboard
        assert "Price at zone:" in trade_dashboard
        assert "Momentum:" in trade_dashboard and "Volume:" in trade_dashboard
        assert "Reversal candle:" in trade_dashboard
        assert "Order flow:" in trade_dashboard
    assert "Economic event:" in trade_dashboard
    assert "EVENT APPROACHING — CAUTION" in trade_dashboard or "PRE-RELEASE SAFETY" in trade_dashboard or "RELEASE IMPULSE" in trade_dashboard or "EVENT OPPORTUNITY WINDOW" in trade_dashboard or "NO EVENT RESTRICTION" in trade_dashboard
    assert "Plan control:" in trade_dashboard and "Next action:" in trade_dashboard
    assert "LONG PLAN" in trade_dashboard and "SHORT PLAN" in trade_dashboard
    assert "KEY LEVEL MAP" in trade_dashboard
    assert "S1:" in trade_dashboard and "R1:" in trade_dashboard
    assert "REVERSAL / PATTERN CLUES" in trade_dashboard
    assert "Provisional TP1:" in trade_dashboard and "Provisional TP3:" in trade_dashboard
    assert "Use /scan for the complete evidence report." in trade_dashboard
    signal.analyses["15m"].divergences = ["Bearish regular divergence"]
    conflicting_dashboard = build_trade_dashboard(
        signal,
        SimpleNamespace(adjusted_score=25.0, taker_flow_imbalance=-79.7, large_flow_imbalance=-100.0),
    )
    assert "FOCUS: NO CLEAR DIRECTION — WAIT" in conflicting_dashboard
    assert "BLOCKED: Bearish regular divergence" in conflicting_dashboard
    signal.analyses["15m"].divergences = []
    generated_plans = create_structural_trade_plans(signal)
    assert set(generated_plans) == {"LONG", "SHORT"}
    armed_long = {
        **generated_plans["LONG"], "created_at": 1.0, "expires_at": 9999999999.0,
        "approach_alerted": False, "zone_alerted": False, "ready_alerted": False,
    }
    set_armed_trade_plans(signal.symbol, {"LONG": armed_long})
    signal.price = armed_long["zone_high"] * 1.003
    approach_alert = evaluate_armed_trade_plan_alert(
        signal,
        {"taker_flow_imbalance": 25.0, "large_flow_imbalance": 40.0},
    )
    assert approach_alert.alert_type == "ARMED_PLAN_APPROACHING"
    assert "Advance warning only" in approach_alert.message
    signal.price = (armed_long["zone_low"] + armed_long["zone_high"]) / 2.0
    armed_alert = evaluate_armed_trade_plan_alert(
        signal,
        {"taker_flow_imbalance": 25.0, "large_flow_imbalance": 40.0},
    )
    assert armed_alert.alert_type in {"ARMED_PLAN_READY", "ARMED_PLAN_ZONE"}
    remove_armed_trade_plans(signal.symbol)
    signal.price = 100.5
    signal.analyses = {}
    signal.analyses = {
        "15m": SimpleNamespace(
            score=-70.0, reasons=["Supertrend is bullish", "RSI 12 crossed below RSI 24"],
            price=74.0, ema20=75.0, ema50=76.0, macd=-0.3, macd_signal=-0.1,
            rsi_6=35.0, rsi_12=40.0, rsi_24=45.0,
        )
    }
    balanced = build_balanced_evidence(signal)
    assert any("🔴" in line for line in balanced)
    assert any("🟢" in line for line in balanced)
    assert balanced[0].startswith("• 🔴")
    signal.analyses = {}
    context = build_market_context(
        signal,
        {
            "global_crypto": {
                "btc_dominance": 55.0,
                "eth_dominance": 17.5,
                "market_change_24h": 1.2,
            },
            "vix": {"value": 16.0, "change_percent": -1.0},
            "fear_greed": {
                "value": 62.0,
                "label": "GREED",
                "change": 2.0,
                "live": True,
            },
            "coinbase_premium": {
                "btc": 0.125,
                "eth": -0.135,
                "live": True,
            },
            "derivatives": {
                "funding_rate": 0.0006,
                "funding_label": "CROWDED LONGS",
                "perp_spot_basis": 0.32,
                "open_interest_value": 250000000.0,
                "open_interest_change_5m": 1.2,
                "open_interest_change_1h": 6.5,
                "live": True,
                "provider": "Offline test",
                "orderbook_imbalance": 24.0,
                "bid_wall_price": 98.5,
                "bid_wall_strength": 4.2,
                "ask_wall_price": 102.0,
                "ask_wall_strength": 2.1,
                "taker_buy_ratio": 61.0,
                "taker_flow_imbalance": 22.0,
                "large_trade_threshold": 25000.0,
                "large_trade_count": 4,
                "large_flow_share": 28.0,
                "large_flow_imbalance": 72.0,
                "largest_trade_value": 150000.0,
                "largest_trade_side": "BUY",
                "largest_trade_multiple": 31.0,
            },
            "provider_errors": {},
        },
    )
    message = build_scan_message(signal, context)
    decision = evaluate_signal_alert(signal, context)
    assert "Fear & Greed: 62" in message
    assert "BTC Coinbase Premium: +0.125% — US BUYING" in message
    assert "ETH Coinbase Premium: -0.135% — US SELLING" in message
    assert "ETH dominance: 17.50%" in message
    assert "ETH vs BTC momentum (12h):" in message
    weak_eth_macro = build_market_context(
        signal,
        {
            "global_crypto": {
                "btc_dominance": 56.0,
                "eth_dominance": 10.0,
                "market_change_24h": 0.0,
            }
        },
    )
    assert "Weak ETH dominance signals limited broad altcoin participation." in weak_eth_macro.macro_reasons
    assert "Funding: +0.0600%" in message
    assert "Perpetual vs spot basis: +0.320% — LONG PREMIUM" in message
    assert "Derivatives source: Offline test" in message
    assert "OI change: +1.20% (5m), +6.50% (1h)" in message
    assert "crowded at ±0.0500%" in message
    assert "high ≥ 0.10%" in message
    assert "LONG ≥ +62; SHORT ≤ -62" in message
    assert "Volume activity:" in message and "strong ≥ 100%" in message
    assert "baseline; direction comes from its % change" in message
    assert "Book imbalance: +24.0% — BID HEAVY" in message
    assert "Buy wall: $98.5000 — 4.2× median level" in message
    assert "Recent taker flow: 61.0% buys — BUY DOMINANT" in message
    assert "LARGE TRADE FLOW" in message
    assert "Largest trade: $150,000.00 BUY — 31.0× average" in message
    assert "\n\nTechnical score:" in message
    # OKX sizes are contracts, not whole coins; live parsing must apply ctVal.
    btc_contracts = 100.0
    btc_contract_value = 0.01
    btc_price = 62500.0
    assert btc_contracts * btc_contract_value * btc_price == 62500.0
    derivatives_alert = evaluate_derivatives_alert(
        signal,
        {
            "funding_rate": 0.0006,
            "funding_label": "CROWDED LONGS",
            "open_interest_value": 250000000.0,
            "open_interest_change_5m": 1.2,
            "open_interest_change_1h": 6.5,
            "provider": "Offline test",
            "live": True,
            "long_liquidations_1h": 0.0,
            "short_liquidations_1h": 300000.0,
            "liquidation_pressure": "SHORT SQUEEZE",
        },
    )
    assert derivatives_alert.should_send
    assert derivatives_alert.alert_type == "FUNDING_CROWDING"
    liquidation_alert = evaluate_derivatives_alert(
        signal,
        {
            "funding_rate": 0.0,
            "funding_label": "BALANCED",
            "open_interest_value": 250000000.0,
            "open_interest_change_5m": 0.0,
            "open_interest_change_1h": 0.0,
            "long_liquidations_1h": 300000.0,
            "short_liquidations_1h": 0.0,
            "liquidation_pressure": "LONG FLUSH",
            "provider": "Offline test",
            "live": True,
        },
    )
    assert liquidation_alert.alert_type == "LIQUIDATION_WAVE"
    order_flow_data = {
            "funding_rate": 0.0,
            "funding_label": "BALANCED",
            "open_interest_value": 250000000.0,
            "open_interest_change_5m": 0.0,
            "open_interest_change_1h": 0.0,
            "orderbook_imbalance": -31.0,
            "taker_flow_imbalance": -28.0,
            "taker_buy_ratio": 36.0,
            "provider": "Offline test",
            "live": True,
        }
    remove_armed_trade_plans(signal.symbol)
    order_flow_without_zone = evaluate_derivatives_alert(signal, order_flow_data)
    assert order_flow_without_zone.alert_type != "ORDER_FLOW_SHIFT"
    set_armed_trade_plans(signal.symbol, {"LONG": {
        "side": "LONG", "interval": "15m",
        "zone_low": signal.price * 0.999, "zone_high": signal.price * 1.001,
        "created_at": 1.0, "expires_at": 9999999999.0,
    }})
    order_flow_alert = evaluate_derivatives_alert(signal, order_flow_data)
    assert order_flow_alert.alert_type == "ORDER_FLOW_SHIFT"
    assert "OPPOSES LONG" in order_flow_alert.message
    assert "Decision impact: 🔴 OPPOSES LONG" in order_flow_alert.message
    assert "breakout/invalidation warning" in order_flow_alert.message
    remove_armed_trade_plans(signal.symbol)
    large_trade_data = {
            "funding_rate": 0.0,
            "funding_label": "BALANCED",
            "open_interest_value": 250000000.0,
            "open_interest_change_5m": 0.0,
            "open_interest_change_1h": 0.0,
            "large_flow_share": 32.0,
            "large_flow_imbalance": 75.0,
            "largest_trade_multiple": 40.0,
            "largest_trade_value": 200000.0,
            "largest_trade_side": "BUY",
            "provider": "Offline test",
            "live": True,
        }
    remove_armed_trade_plans(signal.symbol)
    large_trade_without_zone = evaluate_derivatives_alert(signal, large_trade_data)
    assert large_trade_without_zone.alert_type != "LARGE_TRADE_FLOW"
    set_armed_trade_plans(signal.symbol, {"LONG": {
        "side": "LONG", "interval": "15m",
        "zone_low": signal.price * 0.999, "zone_high": signal.price * 1.001,
        "created_at": 1.0, "expires_at": 9999999999.0,
    }})
    large_trade_alert = evaluate_derivatives_alert(
        signal,
        large_trade_data,
    )
    assert large_trade_alert.alert_type == "LARGE_TRADE_FLOW"
    assert "Largest trade: $200,000.00 BUY — 40.0× average\nLarge-trade net flow:" in large_trade_alert.message
    assert "armed LONG zone" in large_trade_alert.message
    assert "SUPPORTS LONG" in large_trade_alert.message
    assert "Decision impact: 🟢 SUPPORTS LONG" in large_trade_alert.message
    opposing_large_trade = evaluate_derivatives_alert(signal, {
        **large_trade_data, "large_flow_imbalance": -75.0,
        "largest_trade_side": "SELL",
    })
    assert opposing_large_trade.alert_type == "LARGE_TRADE_FLOW"
    assert "OPPOSES LONG" in opposing_large_trade.message
    assert "breakout/invalidation warning" in opposing_large_trade.message
    remove_armed_trade_plans(signal.symbol)
    assert "Execution status: WATCH" in message
    assert "CONFIDENCE BREAKDOWN" in message
    assert "Risk:" in message
    assert decision.should_send
    assert decision.alert_type in {"WATCH", "EVENT_RISK"}
    persisted_setup = {
        "side": plan.side,
        "plan": asdict(plan),
        "created_at": 1.0,
        "tp1": False,
        "tp2": False,
        "breakeven": False,
        "management_stop": plan.stop_loss,
        "exit_warning": False,
    }
    set_active_setup(signal.symbol, persisted_setup)
    assert get_active_setups()[signal.symbol]["plan"]["tp3"] == plan.tp3
    setups_message = build_active_setups_message()
    assert "TESTUSDT — LONG" in setups_message
    assert "TP3: $109.0000" in setups_message
    assert "Managed protection: $97.0000" in setups_message
    remove_active_setup(signal.symbol)
    assert signal.symbol not in get_active_setups()
    assert "None" in build_active_setups_message()
    adverse_context = SimpleNamespace(
        adjusted_score=10.0,
        funding_rate=0.0006,
        open_interest_change_1h=6.0,
        taker_flow_imbalance=-25.0,
        large_flow_imbalance=-45.0,
        macro_bias="BEARISH",
        news_label="NEUTRAL",
        reasons=[],
        macro_reasons=[],
        warnings=[],
    )
    setup_states[signal.symbol] = {
        "side": "LONG", "plan": plan, "created_at": 1.0,
        "tp1": False, "tp2": False, "breakeven": False,
        "management_stop": plan.stop_loss, "exit_warning": False,
    }
    smart_exit = evaluate_signal_alert(signal, adverse_context)
    assert smart_exit.alert_type == "EXIT_40"
    assert "REDUCE 40%" in smart_exit.message
    remove_active_setup(signal.symbol)
    setup_states.pop(signal.symbol, None)
    signal.price = 103.1
    setup_states[signal.symbol] = {
        "side": "LONG", "plan": plan, "created_at": 1.0,
        "tp1": False, "tp2": False, "breakeven": False,
        "management_stop": plan.stop_loss, "exit_warning": False,
    }
    tp1_alert = evaluate_signal_alert(signal, SimpleNamespace(adjusted_score=65.0))
    assert tp1_alert.alert_type == "TP1"
    assert "taking 30%" in tp1_alert.message
    assert setup_states[signal.symbol]["management_stop"] == 99.5
    remove_active_setup(signal.symbol)
    setup_states.pop(signal.symbol, None)
    signal.price = 100.5
    setup_states[signal.symbol] = {
        "side": "LONG",
        "plan": plan,
        "created_at": 1.0,
        "tp1": False,
        "tp2": False,
        "breakeven": False,
        "management_stop": plan.stop_loss,
        "exit_warning": False,
    }
    exit_alert = evaluate_derivatives_alert(
        signal,
        {
            "funding_rate": 0.0011,
            "funding_label": "EXTREME LONGS",
            "open_interest_value": 250000000.0,
            "open_interest_change_5m": -6.0,
            "open_interest_change_1h": -9.0,
            "provider": "Offline test",
            "live": True,
        },
    )
    assert exit_alert.should_send
    assert exit_alert.alert_type == "DERIVATIVES_EXIT"
    setup_states.pop(signal.symbol, None)
    signal.trade_plan = None
    breakdown = build_confidence_breakdown(signal, context)
    assert "Risk: N/A — no active setup" in breakdown
    print("Smoke test passed")


if __name__ == "__main__":
    main()
