from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionContext:
    label: str
    detail: str
    caution: str


def get_session_context(now: datetime | None = None) -> SessionContext:
    eastern = ZoneInfo("America/New_York")
    current = now.astimezone(eastern) if now else datetime.now(eastern)
    clock = current.time()
    weekday = current.weekday()

    if weekday >= 5:
        return SessionContext(
            "WEEKEND",
            "Traditional markets are closed; crypto liquidity can be thinner.",
            "Require stronger volume confirmation and allow wider slippage protection.",
        )
    if time(19, 45) <= clock < time(20, 30):
        return SessionContext(
            "ASIA OPEN",
            "Tokyo-session liquidity is opening and can reshape the overnight crypto range.",
            "Wait for the opening impulse to establish a range before following a breakout.",
        )
    if time(8, 0) <= clock < time(9, 15):
        return SessionContext(
            "US DATA WINDOW",
            "Macro releases and premarket positioning can move risk assets quickly.",
            "Avoid entering immediately after a headline candle; wait for a retest.",
        )
    if time(9, 15) <= clock < time(9, 35):
        return SessionContext(
            "US OPEN",
            "Equity-market opening flows often increase crypto volatility.",
            "Use no-chase rules and require confirmation after the opening burst.",
        )
    if time(6, 0) <= clock < time(8, 0):
        return SessionContext(
            "US PREMARKET",
            "US equity premarket positioning is active alongside European liquidity.",
            "Watch index futures and require volume confirmation before carrying a move into US data.",
        )
    if time(9, 35) <= clock < time(11, 30):
        return SessionContext(
            "US MORNING",
            "Liquidity and directional follow-through are usually stronger.",
            "Favor retests of planned levels over market orders.",
        )
    if time(11, 30) <= clock < time(13, 30):
        return SessionContext(
            "US MIDDAY",
            "Participation often softens and false breaks can increase.",
            "Reduce confidence when relative volume is weak.",
        )
    if time(15, 0) <= clock < time(16, 10):
        return SessionContext(
            "US POWER HOUR",
            "Closing flows can accelerate or reverse intraday trends.",
            "Tighten management on open positions and avoid late chasing.",
        )
    if time(3, 0) <= clock < time(3, 30):
        return SessionContext(
            "LONDON OPEN",
            "London opening flows can sweep the Asian range and create false first moves.",
            "Wait for a sweep-and-retest or a confirmed hold outside the Asian range.",
        )
    if time(3, 30) <= clock < time(6, 0):
        return SessionContext(
            "EUROPE MORNING",
            "European liquidity is active before the US session.",
            "Watch for London-driven breakouts that need a retest.",
        )
    if clock >= time(19, 0) or clock < time(2, 0):
        return SessionContext(
            "ASIA TRANSITION",
            "Asian-session positioning can reshape overnight ranges.",
            "Prefer range edges and confirmed liquidity sweeps.",
        )
    return SessionContext(
        "TRANSITION",
        "No major session transition is currently dominant.",
        "Follow the planned level and confirmation rules.",
    )


def get_special_market_event(now: datetime | None = None) -> str:
    eastern = ZoneInfo("America/New_York")
    current = now.astimezone(eastern) if now else datetime.now(eastern)
    tomorrow = current + timedelta(days=1)
    if tomorrow.month != current.month:
        if current.month in {3, 6, 9, 12}:
            return "QUARTER-END CLOSE — positioning and benchmark flows can distort normal signals."
        return "MONTH-END CLOSE — rebalancing flows can increase volatility near the daily close."
    if current.weekday() == 4:
        return "WEEKLY CLOSE — weekend liquidity conditions begin after the US session."
    return ""
