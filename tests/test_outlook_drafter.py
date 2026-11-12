import asyncio
from datetime import date, datetime, timedelta

import pytest

from app.adapters import outlook_drafter as d
from app.adapters.playwright_mailer import TEST_EMAIL

EML = (
    "To: tenant@example.com\n"
    "Cc: owner@example.com\n"
    "Subject: Rough Estimate | Krishna Brookfield #Flat T-17\n"
    "X-Unsent: 1\n"
    "MIME-Version: 1.0\n"
    'Content-Type: text/html; charset="utf-8"\n'
    "Content-Transfer-Encoding: 8bit\n\n"
    "<html><body><p>Dear Tenant,</p></body></html>\n"
)

PACK = {
    "emailsToSend": [],
    "emailsToSchedule": [
        {
            "label": "Owner 2nd email",
            "subject": "Association access",
            "html": "<p>x</p>",
            "to": "owner@example.com",
            "cc": "tenant@example.com",
            "immediate": False,
            "sendAtISO": "2026-11-13T04:30:00Z",
        },
        {"label": "Stray immediate", "subject": "s", "immediate": True},
    ],
    "emailFiles": [{"label": "Tenant 2nd email", "content": EML}],
}


def test_parse_eml_extracts_headers_and_html() -> None:
    parsed = d.parse_eml(EML)
    assert parsed["to"] == "tenant@example.com"
    assert parsed["cc"] == "owner@example.com"
    assert parsed["subject"].startswith("Rough Estimate | Krishna")
    assert "<p>Dear Tenant,</p>" in parsed["html"]


def test_parse_send_at_converts_utc_to_local() -> None:
    local = d.parse_send_at("2026-11-13T04:30:00Z")
    expected = (
        datetime(2026, 11, 13, 4, 30).replace(tzinfo=__import__("datetime").timezone.utc)
        .astimezone().replace(tzinfo=None)
    )
    assert local == expected
    assert d.parse_send_at("") is None


def test_items_skip_immediate_and_keep_original_recipients() -> None:
    scheduled = d.scheduled_items(PACK)
    assert [i.label for i in scheduled] == ["Owner 2nd email"]
    assert scheduled[0].original_cc == "tenant@example.com"
    assert scheduled[0].send_at is not None

    manual = d.manual_items(PACK)
    assert manual[0].kind == "manual" and manual[0].original_to == "tenant@example.com"


@pytest.mark.parametrize(
    "sample, expected",
    [
        ("13-11-2026", "14-11-2026"),
        ("2026/11/13", "2026/11/14"),
        ("11/13/2026", "11/14/2026"),
        ("13.11.2026", "14.11.2026"),
    ],
)
def test_format_like_matches_picker_order(sample, expected) -> None:
    assert d._format_like(sample, date(2026, 11, 14)) == expected


def test_resolve_test_email_requires_flag_or_env(monkeypatch) -> None:
    monkeypatch.delenv("MOVEOUT_TEST_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="test-email"):
        d.resolve_test_email(None)
    monkeypatch.setenv("MOVEOUT_TEST_EMAIL", TEST_EMAIL)
    assert d.resolve_test_email(None) == TEST_EMAIL
    with pytest.raises(RuntimeError):
        d.resolve_test_email("someone@example.com")


def test_cli_refuses_without_test_email(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MOVEOUT_TEST_EMAIL", raising=False)
    pack_file = tmp_path / "pack.json"
    pack_file.write_text("{}")
    with pytest.raises(RuntimeError, match="test-email"):
        asyncio.run(d._main(["--pack-file", str(pack_file)]))


class _Page:
    async def goto(self, *a, **k) -> None: ...


def _patch_flow(monkeypatch, schedule_error=None):
    calls = {"saved": 0}

    async def noop(*a, **k) -> None: ...

    async def schedule(page, when) -> None:
        if schedule_error:
            raise schedule_error

    async def save(page) -> bool:
        calls["saved"] += 1
        return True

    monkeypatch.setattr(d, "_open_compose", noop)
    monkeypatch.setattr(d, "_fill_draft", noop)
    monkeypatch.setattr(d, "_schedule_send", schedule)
    monkeypatch.setattr(d, "_save_manual", save)
    return calls


def _future_item() -> d.DraftItem:
    return d.DraftItem("scheduled", "L", "S", "<p/>", "o@x", "",
                       datetime.now() + timedelta(days=1))


class _Keyboard:
    async def press(self, key) -> None: ...


class _PageKb(_Page):
    keyboard = _Keyboard()


def test_scheduled_item_is_scheduled_not_saved(monkeypatch) -> None:
    calls = _patch_flow(monkeypatch)
    out = asyncio.run(d._create_one(_PageKb(), _future_item(), TEST_EMAIL, "u"))
    assert out.startswith("scheduled for") and calls["saved"] == 0


def test_missing_schedule_option_falls_back_to_plain_draft(monkeypatch) -> None:
    calls = _patch_flow(monkeypatch, d.ScheduleUnavailable("no menu"))
    out = asyncio.run(d._create_one(_PageKb(), _future_item(), TEST_EMAIL, "u"))
    assert out == "manual" and calls["saved"] == 1


def test_other_schedule_failure_discards_and_fails(monkeypatch) -> None:
    calls = _patch_flow(monkeypatch, RuntimeError("overlay"))
    events = []

    async def diag(page) -> None:
        events.append("diag")

    async def discard(page) -> None:
        events.append("discard")

    monkeypatch.setattr(d, "_save_diagnostics", diag)
    monkeypatch.setattr(d, "_discard_compose", discard)
    with pytest.raises(RuntimeError, match="draft discarded"):
        asyncio.run(d._create_one(_PageKb(), _future_item(), TEST_EMAIL, "u"))
    assert events == ["diag", "discard"] and calls["saved"] == 0


def test_unconfirmed_schedule_never_falls_back(monkeypatch) -> None:
    calls = _patch_flow(monkeypatch, d.ScheduleUnconfirmed("slow"))
    with pytest.raises(d.ScheduleUnconfirmed):
        asyncio.run(d._create_one(_PageKb(), _future_item(), TEST_EMAIL, "u"))
    assert calls["saved"] == 0


def test_past_send_time_becomes_plain_draft(monkeypatch) -> None:
    calls = _patch_flow(monkeypatch)
    item = d.DraftItem("scheduled", "L", "S", "<p/>", "o@x", "",
                       datetime.now() - timedelta(hours=1))
    assert asyncio.run(d._create_one(_PageKb(), item, TEST_EMAIL, "u")) == "manual"
    assert calls["saved"] == 1
