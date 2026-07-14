from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LunarContext:
    phase: str
    phase_at: datetime
    hours_away: float
    label: str
    detail: str


# US Naval Observatory primary-phase times (UTC), remaining 2026.
PRIMARY_PHASES = tuple(
    sorted(
        [
            (datetime(2026, m, d, h, minute, tzinfo=timezone.utc), "NEW MOON")
            for m, d, h, minute in ((7, 14, 9, 43), (8, 12, 17, 37), (9, 11, 3, 27), (10, 10, 15, 50), (11, 9, 7, 2), (12, 9, 0, 52))
        ]
        + [
            (datetime(2026, m, d, h, minute, tzinfo=timezone.utc), "FULL MOON")
            for m, d, h, minute in ((7, 29, 14, 36), (8, 28, 4, 18), (9, 26, 16, 49), (10, 26, 4, 12), (11, 24, 14, 53), (12, 24, 1, 28))
        ],
        key=lambda item: item[0],
    )
)


def get_lunar_context(now: datetime | None = None) -> LunarContext:
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    phase_at, phase = min(PRIMARY_PHASES, key=lambda item: abs((item[0] - current).total_seconds()))
    hours = (phase_at - current).total_seconds() / 3600.0
    distance = abs(hours)
    if distance <= 36:
        timing = "in" if hours >= 0 else "occurred"
        detail = f"{phase.title()} {timing} {distance:.1f} hours{' ago' if hours < 0 else ''}. Observational context only; no directional score is applied."
        label = f"NEAR {phase}"
    else:
        detail = f"Nearest primary phase: {phase.title()} ({distance / 24:.1f} days away). No directional score is applied."
        label = "NORMAL"
    return LunarContext(phase, phase_at, hours, label, detail)
