import asyncio
import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.adapters import calendar_creator as c

SUBJECT = "MO KRISHNABROOKFIELDT17: Owner & Tenant move-out video guides (Day 3)"
PACK = {
    "emailsToSend": [],
    "reminders": [
        {"subject": SUBJECT, "startISO": "2026-11-15T10:00:00",
         "endISO": "2026-11-15T10:30:00"},
        {"subject": "MO X: Step 28", "startISO": "2026-11-22T10:00:00",
         "endISO": "2026-11-22T10:30:00"},
    ],
}


def test_load_reminders_keeps_subject_verbatim() -> None:
    reminders = c.load_reminders(PACK)
    assert reminders[0].subject == SUBJECT
    assert reminders[0].start == datetime(2026, 11, 15, 10, 0)


def test_load_reminders_rejects_bad_ranges() -> None:
    with pytest.raises(ValueError, match="ends before"):
        c.load_reminders({"reminders": [
            {"subject": "s", "startISO": "2026-11-15T10:00:00",
             "endISO": "2026-11-15T09:00:00"}]})


def test_compose_url_round_trips_subject_and_times() -> None:
    url = c.compose_url("outlook.live.com", c.load_reminders(PACK)[0])
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/calendar/0/deeplink/compose"
    assert query["subject"] == [SUBJECT]
    assert query["startdt"] == ["2026-11-15T10:00:00"]
    assert query["enddt"] == ["2026-11-15T10:30:00"]


def test_summary_matches_live_format() -> None:
    reminder = c.load_reminders(PACK)[0]
    assert c.summary_matches("Sun 15-11-2026 10:00 - 10:30", reminder)
    assert not c.summary_matches("Sun 15-11-2026 16:00 - 16:30", reminder)
    assert not c.summary_matches("Mon 16-11-2026 10:00 - 10:30", reminder)
    assert not c.summary_matches("", reminder)


def test_dry_run_prints_and_never_opens_browser(monkeypatch, tmp_path, capsys) -> None:
    async def boom(*a, **k):
        raise AssertionError("browser opened")

    monkeypatch.setattr(c, "_with_page", boom)
    pack_file = tmp_path / "pack.json"
    pack_file.write_text(json.dumps(PACK))
    asyncio.run(c._main(["--pack-file", str(pack_file), "--dry-run", "--limit", "1"]))
    out = capsys.readouterr().out
    assert "Creating 1 event(s)" in out
    assert f"Would create: {SUBJECT} — 2026-11-15 10:00 -> 10:30" in out
    assert "Step 28" not in out


def _run_with_outcomes(monkeypatch, outcomes):
    async def fake_with_page(callback):
        return await callback(object(), "host")

    async def fake_create(page, host, reminder):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def no_diag(page, index) -> None: ...

    monkeypatch.setattr(c, "_with_page", fake_with_page)
    monkeypatch.setattr(c, "_create_one", fake_create)
    monkeypatch.setattr(c, "_save_failure", no_diag)


def test_single_failure_does_not_abort_batch(monkeypatch) -> None:
    reminders = c.load_reminders(PACK) * 2  # 4 events
    _run_with_outcomes(monkeypatch, [
        "created", RuntimeError("x"), "already exists, skipped", "created"])
    result = asyncio.run(c.create_events(reminders))
    assert result == {"created": 2, "skipped": 1, "failed": 1}


def test_more_than_half_failing_aborts(monkeypatch) -> None:
    reminders = c.load_reminders(PACK) * 2
    _run_with_outcomes(monkeypatch, [RuntimeError("a"), RuntimeError("b"),
                                     RuntimeError("c"), "created"])
    with pytest.raises(RuntimeError, match="3 of 4 events failed"):
        asyncio.run(c.create_events(reminders))


def test_event_label_matches_live_format() -> None:
    reminder = c.load_reminders(PACK)[0]
    live = f"{SUBJECT}, 10:00 to 10:30, Sunday, November 15, 2026, Busy"
    assert c.event_label_matches(live, reminder)
    assert not c.event_label_matches(live.replace("10:00 to", "11:00 to"), reminder)
    assert not c.event_label_matches(live.replace("November 15", "November 16"), reminder)
    assert not c.event_label_matches("Other " + live, reminder)
