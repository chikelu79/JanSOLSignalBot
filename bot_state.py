from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)


# =========================================================
# STATE SETTINGS
# =========================================================

STATE_FILE = Path("bot_state.json")

DEFAULT_STATE: dict[str, Any] = {
    "selected_pair": "SOLUSDT",
    "monitor_enabled": True,
    "runtime_chat_id": "",
    "watchlist": [
        "SOLUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ],
    "active_setups": {},
    "early_opportunities": {},
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
                    "tactical": bool(setup.get("tactical", False)),
                    "origin_interval": str(setup.get("origin_interval", "")),
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
                    "triggers": [str(value) for value in item.get("triggers", [])][:8],
                }
            except (KeyError, TypeError, ValueError):
                continue
    validated["early_opportunities"] = validated_opportunities

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

    save_state(
        STATE
    )

    return bool(
        STATE["monitor_enabled"]
    )


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
        "runtime_chat_id": get_runtime_chat_id(),
        "watchlist": get_watchlist(),
        "active_setups": get_active_setups(),
        "early_opportunities": get_early_opportunities(),
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
