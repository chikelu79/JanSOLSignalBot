"""Offline structural smoke test. It does not contact Binance or Telegram."""

from dataclasses import asdict

from bot_state import get_active_setups, remove_active_setup, set_active_setup
from market_context import build_market_context
from notifier import (
    build_active_setups_message,
    build_confidence_breakdown,
    build_scan_message,
    evaluate_signal_alert,
)
from strategy import MarketSignal, TradePlan


def main() -> None:
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
        price=98.0,
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
            "provider_errors": {},
        },
    )
    message = build_scan_message(signal, context)
    decision = evaluate_signal_alert(signal, context)
    assert "Fear & Greed: 62" in message
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
    signal.trade_plan = None
    breakdown = build_confidence_breakdown(signal, context)
    assert "Risk: N/A — no active setup" in breakdown
    print("Smoke test passed")


if __name__ == "__main__":
    main()
