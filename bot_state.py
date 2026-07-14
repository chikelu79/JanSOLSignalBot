from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)


# =========================================================
# STATE SETTINGS
# =========================================================

STATE_FILE = Path(os.getenv("BOT_STATE_FILE", "bot_state.json"))
DEFAULT_AUTO_PLAN_ENABLED = os.getenv("AUTO_PLAN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

DEFAULT_STATE: dict[str, Any] = {
    "selected_pair": "SOLUSDT",
    "monitor_enabled": True,
    "auto_plan_enabled": DEFAULT_AUTO_PLAN_ENABLED,
    "auto_plan_fingerprints": {},
    "runtime_chat_id": "",
    "watchlist": [
        "SOLUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ],
    "active_setups": {},
    "early_opportunities": {},
    "early_opportunity_outcomes": [],
    "armed_trade_plans": {},
    "signal_performance": [],
    "alert_history": {},
    "trading_horizon": "DAY",
    "risk_style": "BALANCED",
}


state_lock = Lock()


# =========================================================
# SYMBOL NORMALIZATION
# =========================================================

def normalize_symbol(
    symbol: str,
) -> str:
    cleaned = re.sub(
        r"[^A-Za-z0-9]",
        "",
        symbol,
    ).upper()

    if not cleaned:
        raise ValueError(
            "The trading pair cannot be empty."
        )

    common_quotes = (
        "USDT",
        "USDC",
        "FDUSD",
        "BTC",
        "ETH",
        "BNB",
    )

    has_quote_asset = any(
        cleaned.endswith(quote)
        for quote in common_quotes
    )

    if not has_quote_asset:
        cleaned = f"{cleaned}USDT"

    if len(cleaned) < 5:
        raise ValueError(
            "The trading pair is too short."
        )

    if len(cleaned) > 20:
        raise ValueError(
            "The trading pair is too long."
        )

    return cleaned


# =========================================================
# STATE VALIDATION
# =========================================================

def validate_state(
    state: dict[str, Any],
) -> dict[str, Any]:
    validated = DEFAULT_STATE.copy()

    selected_pair = state.get(
        "selected_pair",
        DEFAULT_STATE["selected_pair"],
    )

    try:
        validated["selected_pair"] = (
            normalize_symbol(
                str(selected_pair)
            )
        )
    except ValueError:
        validated["selected_pair"] = (
            DEFAULT_STATE["selected_pair"]
        )

    validated["monitor_enabled"] = bool(
        state.get(
            "monitor_enabled",
            True,
        )
    )
    # Continuous monitoring always includes automatic plan preparation.
    validated["auto_plan_enabled"] = bool(state.get("auto_plan_enabled", DEFAULT_AUTO_PLAN_ENABLED)) or validated["monitor_enabled"]
    fingerprints = state.get("auto_plan_fingerprints", {})
    validated["auto_plan_fingerprints"] = {
        normalize_symbol(str(symbol)): str(value)
        for symbol, value in fingerprints.items()
        if isinstance(fingerprints, dict) and str(value)
    } if isinstance(fingerprints, dict) else {}

    validated["runtime_chat_id"] = str(
        state.get(
            "runtime_chat_id",
            "",
        )
    ).strip()

    horizon = str(state.get("trading_horizon", "DAY")).upper()
    risk_style = str(state.get("risk_style", "BALANCED")).upper()
    validated["trading_horizon"] = horizon if horizon in {"SCALPING", "DAY", "SWING"} else "DAY"
    validated["risk_style"] = risk_style if risk_style in {"CONSERVATIVE", "BALANCED", "AGGRESSIVE"} else "BALANCED"

    watchlist = state.get(
        "watchlist",
        DEFAULT_STATE["watchlist"],
    )

    normalized_watchlist: list[str] = []

    if isinstance(watchlist, list):
        for item in watchlist:
            try:
                normalized = normalize_symbol(
                    str(item)
                )

                if normalized not in normalized_watchlist:
                    normalized_watchlist.append(
                        normalized
                    )

            except ValueError:
                continue

    if not normalized_watchlist:
        normalized_watchlist = list(
            DEFAULT_STATE["watchlist"]
        )

    validated["watchlist"] = normalized_watchlist

    active_setups = state.get("active_setups", {})
    validated_setups: dict[str, Any] = {}
    if isinstance(active_setups, dict):
        for symbol, setup in active_setups.items():
            if not isinstance(setup, dict):
                continue
            try:
                normalized = normalize_symbol(str(symbol))
                side = str(setup.get("side", "")).upper()
                plan = setup.get("plan", {})
                if side not in {"LONG", "SHORT"} or not isinstance(plan, dict):
                    continue
                required_levels = (
                    "entry_low", "entry_high", "stop_loss", "invalidation",
                    "tp1", "tp2", "tp3", "risk_per_unit",
                    "reward_risk_tp1", "reward_risk_tp2", "reward_risk_tp3",
                )
                clean_plan = {"side": side}
                for field in required_levels:
                    clean_plan[field] = float(plan[field])
                validated_setups[normalized] = {
                    "side": side,
                    "plan": clean_plan,
                    "created_at": float(setup.get("created_at", 0.0)),
                    "tp1": bool(setup.get("tp1", False)),
                    "tp2": bool(setup.get("tp2", False)),
                    "breakeven": bool(setup.get("breakeven", False)),
                    "management_stop": float(setup.get("management_stop", clean_plan["stop_loss"])),
                    "exit_warning": bool(setup.get("exit_warning", False)),
                    "exit_risk_stage": int(max(0, min(3, int(setup.get("exit_risk_stage", 1 if setup.get("exit_warning") else 0))))),
                    "last_exit_reasons": [str(value) for value in setup.get("last_exit_reasons", [])[:8]],
                    "tactical": bool(setup.get("tactical", False)),
                    "origin_interval": str(setup.get("origin_interval", "")),
                    "signal_id": str(setup.get("signal_id", "")),
                }
            except (KeyError, TypeError, ValueError):
                continue

    validated["active_setups"] = validated_setups

    opportunities = state.get("early_opportunities", {})
    validated_opportunities: dict[str, Any] = {}
    if isinstance(opportunities, dict):
        for key, item in opportunities.items():
            if not isinstance(item, dict):
                continue
            try:
                side = str(item["side"]).upper()
                interval = str(item["interval"])
                if side not in {"LONG", "SHORT"} or interval not in {"5m", "15m"}:
                    continue
                validated_opportunities[str(key)] = {
                    "symbol": normalize_symbol(str(item["symbol"])),
                    "interval": interval,
                    "side": side,
                    "zone_low": float(item["zone_low"]),
                    "zone_high": float(item["zone_high"]),
                    "invalidation": float(item["invalidation"]),
                    "created_at": float(item["created_at"]),
                    "expires_at": float(item["expires_at"]),
                    "relationship": str(item.get("relationship", "MIXED-TREND")),
                    "profile": str(item.get("profile", "UNKNOWN")),
                    "id": str(item.get("id", f"{normalize_symbol(str(item['symbol']))}:{interval}:{side}:{int(float(item['created_at']))}")),
                    "triggers": [str(value) for value in item.get("triggers", [])][:8],
                    "zone_reached": bool(item.get("zone_reached", False)),
                    "target_1r": float(item.get("target_1r", 0.0)),
                    "target_2r": float(item.get("target_2r", 0.0)),
                    "target_1r_hit": bool(item.get("target_1r_hit", False)),
                    "target_2r_hit": bool(item.get("target_2r_hit", False)),
                }
            except (KeyError, TypeError, ValueError):
                continue
    validated["early_opportunities"] = validated_opportunities

    outcomes = state.get("early_opportunity_outcomes", [])
    validated_outcomes: list[dict[str, Any]] = []
    if isinstance(outcomes, list):
        for item in outcomes[-200:]:
            if not isinstance(item, dict):
                continue
            try:
                status = str(item["status"]).upper()
                if status not in {"ZONE_REACHED", "TARGET_1R", "TARGET_2R", "CONFIRMED", "INVALIDATED", "EXPIRED"}:
                    continue
                validated_outcomes.append({
                    "id": str(item.get("id", f"legacy:{item['symbol']}:{item['interval']}:{item['side']}:{int(float(item['timestamp']))}")),
                    "symbol": normalize_symbol(str(item["symbol"])),
                    "interval": str(item["interval"]),
                    "side": str(item["side"]).upper(),
                    "relationship": str(item.get("relationship", "MIXED-TREND")),
                    "profile": str(item.get("profile", "UNKNOWN")),
                    "status": status,
                    "price": float(item["price"]),
                    "timestamp": float(item["timestamp"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
    validated["early_opportunity_outcomes"] = validated_outcomes

    armed = state.get("armed_trade_plans", {})
    validated_armed: dict[str, Any] = {}
    if isinstance(armed, dict):
        for symbol, sides in armed.items():
            if not isinstance(sides, dict):
                continue
            clean_sides: dict[str, Any] = {}
            for side, item in sides.items():
                if side not in {"LONG", "SHORT"} or not isinstance(item, dict):
                    continue
                try:
                    clean_sides[side] = {
                        "side": side, "interval": str(item["interval"]),
                        "zone_low": float(item["zone_low"]), "zone_high": float(item["zone_high"]),
                        "stop": float(item["stop"]), "tp1": float(item["tp1"]),
                        "tp2": float(item["tp2"]), "tp3": float(item["tp3"]),
                        "created_at": float(item["created_at"]), "expires_at": float(item["expires_at"]),
                        "event_plan": bool(item.get("event_plan", False)),
                        "zone_state": str(item.get("zone_state", "WATCHING")),
                        "approach_alerted": bool(item.get("approach_alerted", False)),
                        "zone_alerted": bool(item.get("zone_alerted", False)),
                        "ready_alerted": bool(item.get("ready_alerted", False)),
                        "signal_id": str(item.get("signal_id", "")),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
            if clean_sides:
                validated_armed[normalize_symbol(str(symbol))] = clean_sides
    validated["armed_trade_plans"] = validated_armed

    performance = state.get("signal_performance", [])
    validated_performance: list[dict[str, Any]] = []
    if isinstance(performance, list):
        for item in performance[-500:]:
            if not isinstance(item, dict):
                continue
            try:
                status = str(item.get("status", "OPEN")).upper()
                if status not in {"OPEN", "WON", "LOST", "EXITED"}:
                    continue
                validated_performance.append({
                    "id": str(item["id"]), "source": str(item["source"]),
                    "symbol": normalize_symbol(str(item["symbol"])), "side": str(item["side"]).upper(),
                    "profile": str(item.get("profile", "UNKNOWN")), "timeframe": str(item.get("timeframe", "")),
                    "horizon": str(item.get("horizon", "UNKNOWN")).upper(),
                    "risk_style": str(item.get("risk_style", "UNKNOWN")).upper(),
                    "setup_type": str(item.get("setup_type", item.get("source", "UNKNOWN"))).upper(),
                    "event_plan": bool(item.get("event_plan", False)),
                    "setup_quality": int(max(0, min(100, int(item.get("setup_quality", 0))))),
                    "entry": float(item["entry"]), "stop": float(item["stop"]),
                    "tp1": float(item["tp1"]), "tp2": float(item["tp2"]), "tp3": float(item["tp3"]),
                    "sent_at": float(item["sent_at"]), "status": status,
                    "tp1_hit": bool(item.get("tp1_hit", False)), "tp2_hit": bool(item.get("tp2_hit", False)),
                    "tp3_hit": bool(item.get("tp3_hit", False)), "closed_at": float(item.get("closed_at", 0.0)),
                    "exit_reason": str(item.get("exit_reason", "")),
                })
            except (KeyError, TypeError, ValueError):
                continue
    validated["signal_performance"] = validated_performance

    history = state.get("alert_history", {})
    validated["alert_history"] = {
        str(key)[:240]: float(value)
        for key, value in history.items()
        if isinstance(history, dict) and isinstance(value, (int, float))
    } if isinstance(history, dict) else {}

    return validated


# =========================================================
# LOAD AND SAVE
# =========================================================

def load_state() -> dict[str, Any]:
    with state_lock:
        if not STATE_FILE.exists():
            return DEFAULT_STATE.copy()

        try:
            raw_state = json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8",
                )
            )

            if not isinstance(
                raw_state,
                dict,
            ):
                raise ValueError(
                    "State file does not contain an object."
                )

            return validate_state(
                raw_state
            )

        except Exception as error:
            logger.warning(
                "Could not load bot state: %s",
                error,
            )

            return DEFAULT_STATE.copy()


def save_state(
    state: dict[str, Any],
) -> None:
    validated = validate_state(
        state
    )

    with state_lock:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = STATE_FILE.with_suffix(
            ".tmp"
        )

        temporary_file.write_text(
            json.dumps(
                validated,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        temporary_file.replace(
            STATE_FILE
        )


STATE = load_state()


# =========================================================
# SELECTED PAIR
# =========================================================

def get_selected_pair() -> str:
    return str(
        STATE["selected_pair"]
    )


def set_selected_pair(
    symbol: str,
) -> str:
    normalized = normalize_symbol(
        symbol
    )

    STATE["selected_pair"] = normalized

    save_state(
        STATE
    )

    return normalized


# =========================================================
# MONITORING STATUS
# =========================================================

def is_monitor_enabled() -> bool:
    return bool(
        STATE["monitor_enabled"]
    )


def set_monitor_enabled(
    enabled: bool,
) -> bool:
    STATE["monitor_enabled"] = bool(
        enabled
    )
    if enabled:
        STATE["auto_plan_enabled"] = True

    save_state(
        STATE
    )

    return bool(
        STATE["monitor_enabled"]
    )


def is_auto_plan_enabled() -> bool:
    return bool(STATE.get("auto_plan_enabled", False))


def set_auto_plan_enabled(enabled: bool) -> bool:
    STATE["auto_plan_enabled"] = bool(enabled)
    if not enabled:
        STATE["monitor_enabled"] = False
    save_state(STATE)
    return bool(STATE["auto_plan_enabled"])


def get_auto_plan_fingerprint(symbol: str) -> str:
    return str(STATE.get("auto_plan_fingerprints", {}).get(normalize_symbol(symbol), ""))


def set_auto_plan_fingerprint(symbol: str, fingerprint: str) -> None:
    values = dict(STATE.get("auto_plan_fingerprints", {}))
    normalized = normalize_symbol(symbol)
    if fingerprint:
        values[normalized] = str(fingerprint)
    else:
        values.pop(normalized, None)
    STATE["auto_plan_fingerprints"] = values
    save_state(STATE)


# =========================================================
# TELEGRAM CHAT
# =========================================================

def get_runtime_chat_id() -> str:
    return str(
        STATE.get(
            "runtime_chat_id",
            "",
        )
    ).strip()


def set_runtime_chat_id(
    chat_id: str | int,
) -> str:
    normalized_chat_id = str(
        chat_id
    ).strip()

    if not normalized_chat_id:
        raise ValueError(
            "Telegram chat ID cannot be empty."
        )

    STATE["runtime_chat_id"] = (
        normalized_chat_id
    )

    save_state(
        STATE
    )

    return normalized_chat_id


# =========================================================
# WATCHLIST
# =========================================================

def get_watchlist() -> list[str]:
    return list(
        STATE["watchlist"]
    )


def set_watchlist(
    symbols: list[str],
) -> list[str]:
    normalized_symbols: list[str] = []

    for symbol in symbols:
        normalized = normalize_symbol(
            symbol
        )

        if normalized not in normalized_symbols:
            normalized_symbols.append(
                normalized
            )

    if not normalized_symbols:
        raise ValueError(
            "The watchlist cannot be empty."
        )

    STATE["watchlist"] = normalized_symbols

    save_state(
        STATE
    )

    return list(
        normalized_symbols
    )


def add_to_watchlist(
    symbol: str,
) -> list[str]:
    normalized = normalize_symbol(
        symbol
    )

    watchlist = get_watchlist()

    if normalized not in watchlist:
        watchlist.append(
            normalized
        )

    return set_watchlist(
        watchlist
    )


def remove_from_watchlist(
    symbol: str,
) -> list[str]:
    normalized = normalize_symbol(
        symbol
    )

    watchlist = [
        item
        for item in get_watchlist()
        if item != normalized
    ]

    if not watchlist:
        raise ValueError(
            "At least one trading pair must remain "
            "on the watchlist."
        )

    return set_watchlist(
        watchlist
    )


# =========================================================
# COMPLETE STATE SNAPSHOT
# =========================================================

def get_state_snapshot() -> dict[str, Any]:
    return {
        "selected_pair": get_selected_pair(),
        "monitor_enabled": is_monitor_enabled(),
        "auto_plan_enabled": is_auto_plan_enabled(),
        "runtime_chat_id": get_runtime_chat_id(),
        "watchlist": get_watchlist(),
        "active_setups": get_active_setups(),
        "early_opportunities": get_early_opportunities(),
        "early_opportunity_outcomes": get_early_opportunity_outcomes(),
        "armed_trade_plans": get_armed_trade_plans(),
        "signal_performance": get_signal_performance(),
        "trading_horizon": get_trading_horizon(),
        "risk_style": get_risk_style(),
    }


def get_trading_horizon() -> str:
    return str(STATE.get("trading_horizon", "DAY"))


def get_risk_style() -> str:
    return str(STATE.get("risk_style", "BALANCED"))


def set_trading_profile(horizon: str, risk_style: str) -> None:
    horizon = horizon.upper()
    risk_style = risk_style.upper()
    if horizon not in {"SCALPING", "DAY", "SWING"}:
        raise ValueError("Horizon must be SCALPING, DAY or SWING")
    if risk_style not in {"CONSERVATIVE", "BALANCED", "AGGRESSIVE"}:
        raise ValueError("Risk style must be CONSERVATIVE, BALANCED or AGGRESSIVE")
    STATE["trading_horizon"] = horizon
    STATE["risk_style"] = risk_style
    save_state(STATE)


# =========================================================
# ACTIVE TRADE SETUPS
# =========================================================

def get_active_setups() -> dict[str, Any]:
    return json.loads(json.dumps(STATE.get("active_setups", {})))


def set_active_setup(symbol: str, setup: dict[str, Any]) -> None:
    normalized = normalize_symbol(symbol)
    active_setups = dict(STATE.get("active_setups", {}))
    active_setups[normalized] = setup
    STATE["active_setups"] = active_setups
    save_state(STATE)


def remove_active_setup(symbol: str) -> None:
    normalized = normalize_symbol(symbol)
    active_setups = dict(STATE.get("active_setups", {}))
    active_setups.pop(normalized, None)
    STATE["active_setups"] = active_setups
    save_state(STATE)


def get_early_opportunities() -> dict[str, Any]:
    return json.loads(json.dumps(STATE.get("early_opportunities", {})))


def set_early_opportunity(key: str, opportunity: dict[str, Any]) -> None:
    opportunities = dict(STATE.get("early_opportunities", {}))
    opportunities[str(key)] = opportunity
    STATE["early_opportunities"] = opportunities
    save_state(STATE)


def remove_early_opportunity(key: str) -> None:
    opportunities = dict(STATE.get("early_opportunities", {}))
    opportunities.pop(str(key), None)
    STATE["early_opportunities"] = opportunities
    save_state(STATE)


def get_early_opportunity_outcomes() -> list[dict[str, Any]]:
    return json.loads(json.dumps(STATE.get("early_opportunity_outcomes", [])))


def record_early_opportunity_outcome(opportunity: dict[str, Any], status: str, price: float, timestamp: float) -> None:
    outcomes = list(STATE.get("early_opportunity_outcomes", []))
    outcomes.append({
        "id": str(opportunity.get("id", f"{opportunity['symbol']}:{opportunity['interval']}:{opportunity['side']}:{int(float(opportunity.get('created_at', timestamp)))}")),
        "symbol": normalize_symbol(str(opportunity["symbol"])),
        "interval": str(opportunity["interval"]),
        "side": str(opportunity["side"]).upper(),
        "relationship": str(opportunity.get("relationship", "MIXED-TREND")),
        "profile": str(opportunity.get("profile", "UNKNOWN")),
        "status": status.upper(),
        "price": float(price),
        "timestamp": float(timestamp),
    })
    STATE["early_opportunity_outcomes"] = outcomes[-200:]
    save_state(STATE)


def get_armed_trade_plans() -> dict[str, Any]:
    return json.loads(json.dumps(STATE.get("armed_trade_plans", {})))


def set_armed_trade_plans(symbol: str, plans: dict[str, Any]) -> None:
    armed = dict(STATE.get("armed_trade_plans", {}))
    armed[normalize_symbol(symbol)] = plans
    STATE["armed_trade_plans"] = armed
    save_state(STATE)


def remove_armed_trade_plans(symbol: str) -> None:
    armed = dict(STATE.get("armed_trade_plans", {}))
    armed.pop(normalize_symbol(symbol), None)
    STATE["armed_trade_plans"] = armed
    save_state(STATE)


def get_signal_performance() -> list[dict[str, Any]]:
    return json.loads(json.dumps(STATE.get("signal_performance", [])))


def record_signal_performance(record: dict[str, Any]) -> None:
    records = list(STATE.get("signal_performance", []))
    if not any(str(item.get("id")) == str(record.get("id")) for item in records):
        records.append(record)
    STATE["signal_performance"] = records[-500:]
    save_state(STATE)


def update_signal_performance(signal_id: str, **changes: Any) -> None:
    records = list(STATE.get("signal_performance", []))
    for item in records:
        if str(item.get("id")) == str(signal_id):
            item.update(changes)
            break
    STATE["signal_performance"] = records[-500:]
    save_state(STATE)


def get_alert_history() -> dict[str, float]:
    return {str(key): float(value) for key, value in STATE.get("alert_history", {}).items()}


def record_alert_time(key: str, timestamp: float) -> None:
    history = dict(STATE.get("alert_history", {}))
    history[str(key)[:240]] = float(timestamp)
    if len(history) > 500:
        history = dict(sorted(history.items(), key=lambda item: item[1], reverse=True)[:500])
    STATE["alert_history"] = history
    save_state(STATE)
