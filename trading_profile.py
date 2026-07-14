from __future__ import annotations

from dataclasses import dataclass


HORIZONS = {"SCALPING", "DAY", "SWING"}
RISK_STYLES = {"CONSERVATIVE", "BALANCED", "AGGRESSIVE"}


@dataclass(frozen=True)
class TradingProfile:
    horizon: str
    risk_style: str
    watch_threshold: float
    confirmed_threshold: float
    strong_threshold: float
    alignment_score: float
    higher_timeframe_count: int
    volume_confirmation: float
    minimum_reward_risk: float
    atr_stop_multiplier: float
    weights: dict[str, float]
    primary_timeframes: tuple[str, ...]
    confirmation_timeframes: tuple[str, ...]


HORIZON_SETTINGS = {
    "SCALPING": {
        "weights": {"5m": 0.30, "15m": 0.30, "1h": 0.20, "4h": 0.10, "8h": 0.06, "1d": 0.04},
        "primary": ("5m", "15m"),
        "confirmation": ("1h", "4h"),
        "atr": 1.25,
    },
    "DAY": {
        "weights": {"5m": 0.15, "15m": 0.25, "1h": 0.25, "4h": 0.18, "8h": 0.10, "1d": 0.07},
        "primary": ("15m", "1h"),
        "confirmation": ("4h", "8h", "1d"),
        "atr": 1.50,
    },
    "SWING": {
        "weights": {"5m": 0.03, "15m": 0.07, "1h": 0.15, "4h": 0.25, "8h": 0.25, "1d": 0.25},
        "primary": ("1h", "4h"),
        "confirmation": ("8h", "1d"),
        "atr": 1.85,
    },
}

RISK_SETTINGS = {
    "CONSERVATIVE": {"watch": 68.0, "confirmed": 80.0, "strong": 90.0, "align": 45.0, "higher": 2, "volume": 1.35, "rr": 2.0, "atr": 1.15},
    "BALANCED": {"watch": 62.0, "confirmed": 74.0, "strong": 84.0, "align": 35.0, "higher": 2, "volume": 1.20, "rr": 1.5, "atr": 1.0},
    "AGGRESSIVE": {"watch": 55.0, "confirmed": 66.0, "strong": 78.0, "align": 25.0, "higher": 1, "volume": 1.05, "rr": 1.25, "atr": 0.9},
}


def get_profile(horizon: str, risk_style: str) -> TradingProfile:
    horizon = horizon.upper()
    risk_style = risk_style.upper()
    if horizon not in HORIZONS or risk_style not in RISK_STYLES:
        raise ValueError("Unsupported trading profile")
    h = HORIZON_SETTINGS[horizon]
    r = RISK_SETTINGS[risk_style]
    required_higher = min(int(r["higher"]), len(h["confirmation"]))
    return TradingProfile(
        horizon=horizon,
        risk_style=risk_style,
        watch_threshold=float(r["watch"]),
        confirmed_threshold=float(r["confirmed"]),
        strong_threshold=float(r["strong"]),
        alignment_score=float(r["align"]),
        higher_timeframe_count=required_higher,
        volume_confirmation=float(r["volume"]),
        minimum_reward_risk=float(r["rr"]),
        atr_stop_multiplier=float(h["atr"]) * float(r["atr"]),
        weights=dict(h["weights"]),
        primary_timeframes=tuple(h["primary"]),
        confirmation_timeframes=tuple(h["confirmation"]),
    )


def estimate_position(side: str, entry: float, margin: float, leverage: float, stop: float | None = None, maintenance_margin_rate: float = 0.005) -> dict[str, float | str | None]:
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("Side must be LONG or SHORT")
    if entry <= 0 or margin <= 0 or leverage < 1 or leverage > 125:
        raise ValueError("Entry and margin must be positive; leverage must be between 1x and 125x")
    notional = margin * leverage
    quantity = notional / entry
    if side == "LONG":
        if stop is not None and stop >= entry:
            raise ValueError("A long stop must be below the entry price")
        liquidation = entry * (1.0 - 1.0 / leverage + maintenance_margin_rate)
        stop_loss = quantity * max(0.0, entry - stop) if stop is not None else None
    else:
        if stop is not None and stop <= entry:
            raise ValueError("A short stop must be above the entry price")
        liquidation = entry * (1.0 + 1.0 / leverage - maintenance_margin_rate)
        stop_loss = quantity * max(0.0, stop - entry) if stop is not None else None
    liquidation_distance = abs(entry - liquidation) / entry * 100.0
    stop_margin_percent = stop_loss / margin * 100.0 if stop_loss is not None else None
    liquidation_before_stop = bool(
        stop is not None
        and ((side == "LONG" and stop <= liquidation) or (side == "SHORT" and stop >= liquidation))
    )
    return {
        "side": side, "entry": entry, "margin": margin, "leverage": leverage,
        "notional": notional, "quantity": quantity, "liquidation": liquidation,
        "liquidation_distance": liquidation_distance, "stop": stop,
        "stop_loss": stop_loss, "stop_margin_percent": stop_margin_percent,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_before_stop": liquidation_before_stop,
    }
