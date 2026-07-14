from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from lunar_context import get_lunar_context


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class EconomicEvent:
    name: str
    scheduled_at: datetime
    impact: str
    source: str


@dataclass(frozen=True)
class EconomicRisk:
    event: EconomicEvent | None
    status: str
    detail: str
    block_new_entries: bool


def _event(name: str, month: int, day: int, hour: int, minute: int, source: str) -> EconomicEvent:
    return EconomicEvent(
        name=name,
        scheduled_at=datetime(2026, month, day, hour, minute, tzinfo=EASTERN),
        impact="HIGH",
        source=source,
    )


# Official 2026 schedules published by BLS and the Federal Reserve. Keeping a
# local verified schedule makes the protection resilient when BLS blocks bots.
OFFICIAL_EVENTS_2026 = tuple(
    sorted(
        [
            *[
                _event("US CPI", month, day, 8, 30, "BLS")
                for month, day in ((7, 14), (8, 12), (9, 11), (10, 14), (11, 10), (12, 10))
            ],
            *[
                _event("US Employment / NFP", month, day, 8, 30, "BLS")
                for month, day in ((8, 7), (9, 4), (10, 2), (11, 6), (12, 4))
            ],
            *[
                _event("FOMC rate decision", month, day, 14, 0, "Federal Reserve")
                for month, day in ((7, 29), (9, 16), (10, 28), (12, 9))
            ],
        ],
        key=lambda item: item.scheduled_at,
    )
)


def upcoming_events(now: datetime | None = None, limit: int = 5) -> list[EconomicEvent]:
    current = now.astimezone(EASTERN) if now else datetime.now(EASTERN)
    return [event for event in OFFICIAL_EVENTS_2026 if event.scheduled_at >= current][:limit]


def get_economic_risk(now: datetime | None = None) -> EconomicRisk:
    current = now.astimezone(EASTERN) if now else datetime.now(EASTERN)
    relevant = [
        event
        for event in OFFICIAL_EVENTS_2026
        if event.scheduled_at - timedelta(hours=24) <= current <= event.scheduled_at + timedelta(hours=2)
    ]
    if not relevant:
        next_events = upcoming_events(current, 1)
        next_event = next_events[0] if next_events else None
        detail = "No high-impact US release is inside the next 24 hours."
        if next_event:
            detail = f"Next: {next_event.name} — {format_event_time(next_event)}."
        return EconomicRisk(next_event, "CLEAR", detail, False)

    event = min(relevant, key=lambda item: abs((item.scheduled_at - current).total_seconds()))
    delta = event.scheduled_at - current
    if timedelta(minutes=-30) <= delta <= timedelta(minutes=90):
        return EconomicRisk(
            event,
            "HIGH RISK",
            f"{event.name} is inside the release-risk window. Do not open a new setup.",
            True,
        )
    if timedelta(hours=-2) <= delta < timedelta(minutes=-30):
        return EconomicRisk(
            event,
            "POST-RELEASE",
            f"{event.name} was released recently. Wait for volatility to settle and structure to retest.",
            True,
        )
    hours = max(0.0, delta.total_seconds() / 3600)
    return EconomicRisk(
        event,
        "UPCOMING",
        f"{event.name} is due in {hours:.1f} hours. Reduce confidence and avoid carrying a fresh entry into it.",
        False,
    )


def format_event_time(event: EconomicEvent) -> str:
    return event.scheduled_at.strftime("%a %b %-d, %-I:%M %p ET")


def build_calendar_message(now: datetime | None = None) -> str:
    risk = get_economic_risk(now)
    lunar = get_lunar_context(now)
    lines = [
        "📅 ECONOMIC CALENDAR",
        "",
        f"{'🔴' if risk.block_new_entries else '🟡' if risk.status == 'UPCOMING' else '🟢'} Current risk: {risk.status}",
        risk.detail,
        "",
        "NEXT HIGH-IMPACT EVENTS",
    ]
    events = upcoming_events(now, 5)
    if events:
        lines.extend(f"• {format_event_time(event)} — {event.name} ({event.source})" for event in events)
    else:
        lines.append("• No remaining 2026 events in the verified schedule.")
    lines.extend([
        "",
        "LUNAR CONTEXT",
        f"{'🌑' if lunar.phase == 'NEW MOON' else '🌕'} Status: {lunar.label}",
        lunar.detail,
        "",
        "Times: America/New_York. Schedule sources: BLS, Federal Reserve and USNO.",
    ])
    return "\n".join(lines)
