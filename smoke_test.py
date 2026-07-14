"""Offline structural smoke test. It does not contact Binance or Telegram."""

from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot_state import get_active_setups, remove_active_setup, set_active_setup
from economic_calendar import build_calendar_message, get_economic_risk
from lunar_context import get_lunar_context
from market_context import build_market_context
from notifier import (
    build_active_setups_message,
    build_confidence_breakdown,
    build_scan_message,
    evaluate_derivatives_alert,
    evaluate_economic_alert,
    evaluate_signal_alert,
    setup_states,
)
from strategy import MarketSignal, TradePlan, create_trade_plan


def main() -> None:
    eastern = ZoneInfo("America/New_York")
    upcoming = get_economic_risk(datetime(2026, 7, 13, 20, 0, tzinfo=eastern))
    blocked = get_economic_risk(datetime(2026, 7, 14, 8, 0, tzinfo=eastern))
    post_release = get_economic_risk(datetime(2026, 7, 14, 9, 15, tzinfo=eastern))
    assert upcoming.status == "UPCOMING" and not upcoming.block_new_entries
    assert blocked.status == "HIGH RISK" and blocked.block_new_entries
    assert post_release.status == "POST-RELEASE" and post_release.block_new_entries
    assert "US Employment / NFP" in build_calendar_message(datetime(2026, 7, 30, 12, 0, tzinfo=eastern))
    economic_alert = evaluate_economic_alert()
    assert economic_alert.alert_type in {"ECONOMIC_EVENT", "NONE"}
    lunar = get_lunar_context(datetime(2026, 7, 13, 20, 0, tzinfo=eastern))
    assert lunar.label == "NEAR NEW MOON"
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
    context = build_market_context(
        signal,
        {
            "global_crypto": {
                "btc_dominance": 55.0,
                "market_change_24h": 1.2,
            },
            "vix": {"value": 16.0, "change_percent": -1.0},
            "fear_greed": {
                "value": 62.0,
                "label": "GREED",
                "change": 2.0,
                "live": True,
            },
            "derivatives": {
                "funding_rate": 0.0006,
                "funding_label": "CROWDED LONGS",
                "open_interest_value": 250000000.0,
                "open_interest_change_5m": 1.2,
                "open_interest_change_1h": 6.5,
                "live": True,
                "provider": "Offline test",
            },
            "provider_errors": {},
        },
    )
    message = build_scan_message(signal, context)
    decision = evaluate_signal_alert(signal, context)
    assert "Fear & Greed: 62" in message
    assert "Funding: +0.0600%" in message
    assert "Derivatives source: Offline test" in message
    assert "OI change: +1.20% (5m), +6.50% (1h)" in message
    assert "crowded at ±0.0500%" in message
    assert "high ≥ 0.10%" in message
    assert "LONG ≥ +62; SHORT ≤ -62" in message
    assert "active ≥ 67%; strong ≥ 100%" in message
    assert "baseline; direction comes from its % change" in message
    assert "\n\nTechnical score:" in message
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
    assert "Execution status: WATCH" in message
    assert "CONFIDENCE BREAKDOWN" in message
    assert "Risk:" in message
    assert decision.should_send
    assert decision.alert_type == "WATCH"
    persisted_setup = {
        "side": plan.side,
        "plan": asdict(plan),
        "created_at": 1.0,
        "tp1": False,
        "tp2": False,
        "breakeven": False,
    }
    set_active_setup(signal.symbol, persisted_setup)
    assert get_active_setups()[signal.symbol]["plan"]["tp3"] == plan.tp3
    setups_message = build_active_setups_message()
    assert "TESTUSDT — LONG" in setups_message
    assert "TP3: $109.0000" in setups_message
    remove_active_setup(signal.symbol)
    assert signal.symbol not in get_active_setups()
    assert "None" in build_active_setups_message()
    setup_states[signal.symbol] = {
        "side": "LONG",
        "plan": plan,
        "created_at": 1.0,
        "tp1": False,
        "tp2": False,
        "breakeven": False,
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
