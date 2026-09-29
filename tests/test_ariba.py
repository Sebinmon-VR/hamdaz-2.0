"""The Ariba reader's decisions — no portal, no database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.ariba import service
from app.ariba.portal import _end_time
from app.core.config import Settings
from app.models.ariba import AribaState

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        ariba_settle_seconds=180, ariba_min_gap_seconds=1800, ariba_max_visits_per_day=2
    )


def test_end_time_is_read_in_the_browser_zone() -> None:
    got = _end_time("10/08/2026 03:00 PM ", ZoneInfo("Asia/Dubai"))
    assert got == datetime(2026, 10, 8, 15, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    assert _end_time("", ZoneInfo("Asia/Dubai")) is None


def test_tender_number_is_found_in_either_title_style() -> None:
    pattern = _settings().ariba_reference_pattern
    assert service.references_in(pattern, "PR-10204577-RFQ-6000151811-COMPUTERS (PC)") == [
        "6000151811"
    ]
    assert service.references_in(pattern, "6000150626 AP Connect for ADNOC") == ["6000150626"]
    assert service.references_in(pattern, "Doc338825897") == []


def test_a_burst_is_waited_out() -> None:
    record = AribaState(visits_today=0)
    reason = service.refusal(_settings(), record, NOW - timedelta(seconds=60), NOW)
    assert reason == "waiting for the rest of the burst"
    assert service.refusal(_settings(), record, NOW - timedelta(seconds=200), NOW) is None


def test_the_gap_and_the_daily_cap_hold() -> None:
    settled = NOW - timedelta(hours=1)
    recent = AribaState(
        visits_today=1, visits_on=NOW.date(), last_visit_at=NOW - timedelta(minutes=5)
    )
    assert service.refusal(_settings(), recent, settled, NOW) == "visited too recently"
    assert service.refusal(_settings(), recent, settled, NOW, force=True) is None

    spent = AribaState(visits_today=2, visits_on=NOW.date())
    reason = service.refusal(_settings(), spent, settled, NOW, force=True)
    assert reason == "the day's visits are used"
    yesterday = AribaState(visits_today=2, visits_on=date(2026, 9, 28))
    assert service.refusal(_settings(), yesterday, settled, NOW) is None


def test_a_refused_sign_in_pauses_even_a_forced_visit() -> None:
    paused = AribaState(visits_today=0, paused_until=NOW + timedelta(hours=3))
    assert "paused" in (service.refusal(_settings(), paused, NOW, NOW, force=True) or "")


def test_visits_are_counted_per_day() -> None:
    record = AribaState(visits_today=5, visits_on=date(2026, 9, 28))
    service.count_visit(record, NOW)
    assert (record.visits_on, record.visits_today, record.last_visit_at) == (NOW.date(), 1, NOW)


def test_bcd_is_the_uae_wall_clock_in_the_site_zone() -> None:
    from app.ariba.bcd import expected_bcd

    dubai = ZoneInfo("Asia/Dubai")
    zones = {"uae": "Asia/Dubai", "site": "America/Los_Angeles"}
    # A UAE noon: 19:00 UTC in the Pacific summer, 20:00 in winter — as the list holds it.
    summer = expected_bcd(datetime(2026, 9, 29, 12, 0, tzinfo=dubai), **zones)
    winter = expected_bcd(datetime(2027, 1, 12, 12, 0, tzinfo=dubai), **zones)
    assert (summer, winter) == ("2026-09-29T19:00:00Z", "2027-01-12T20:00:00Z")
    # Late evening UAE crosses into the next UTC day.
    late = expected_bcd(datetime(2026, 9, 30, 22, 36, tzinfo=dubai), **zones)
    assert late == "2026-10-01T05:36:00Z"


def test_bcd_compares_to_the_minute() -> None:
    from app.ariba.bcd import same_minute

    assert same_minute("2026-09-29T19:00:00Z", "2026-09-29T19:00:00Z")
    assert same_minute("2026-09-29T19:00:40Z", "2026-09-29T19:00:00Z")
    assert not same_minute("2026-10-01T05:30:00Z", "2026-10-01T05:38:00Z")
    assert not same_minute(None, "2026-10-01T05:38:00Z")


def test_events_match_by_number_or_by_one_exact_title() -> None:
    from types import SimpleNamespace as Row

    from app.models.ariba import AribaEvent

    numbered = AribaEvent(doc_id="Doc1", reference="6000151811", title="PR-1-RFQ-6000151811-PC")
    eoi = AribaEvent(doc_id="Doc2", reference=None, title="EOI - LTPA for VR Learning Modules")
    twice = AribaEvent(doc_id="Doc3", reference=None, title="EOI - Spare parts for valves")
    rows = [
        Row(title="RFQ-6000151811 COMPUTERS"),
        Row(title=" EOI – LTPA for VR  learning modules"),
        Row(title="EOI - Spare parts for valves"),
        Row(title="EOI – spare parts for valves"),
    ]
    got = service.match(_settings(), [numbered, eoi, twice], rows)
    assert got["Doc1"] == [rows[0]]
    assert got["Doc2"] == [rows[1]]
    # Two rows fit the title: not a match, rather than a guess.
    assert got["Doc3"] == []
