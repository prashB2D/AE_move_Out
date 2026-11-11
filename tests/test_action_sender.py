import pytest

from app.adapters.action_sender import execute_pack
from app.adapters.action_sender import is_dry_run_enabled


def test_execute_pack_previews_all_action_types(capsys) -> None:
    pack = {
        "tickets": [
            {"category": "Inspection", "home": "home-1"},
            {"category": "Key Handover", "home": "home-1"},
        ],
        "emailsToSend": [
            {"recipient": "tenant@example.com", "subject": "Tenant notice"},
            {"recipient": "owner@example.com", "subject": "Owner notice"},
        ],
        "whatsappMessages": [
            {"phone": "+919876543210", "message": "Move-out notice"}
        ],
        "reminders": [
            {"subject": "Move-out day", "startISO": "2026-11-30T10:00:00+05:30"}
        ],
    }

    results = execute_pack(pack)

    output = capsys.readouterr().out
    assert "[THM] Would create ticket: Inspection for home home-1" in output
    assert "[Graph] Would send email to t***@example.com" in output
    assert "[WhatsApp] Would send message to ***3210." in output
    assert "[Calendar] Would create reminder: Move-out day" in output
    assert "tenant@example.com" not in output
    assert results == {
        "mode": "dry_run",
        "tickets_planned": 2,
        "tickets_skipped": 0,
        "emails_planned": 2,
        "whatsapp_planned": 1,
        "whatsapp_skipped": 0,
        "reminders_planned": 1,
        "reminders_skipped": 0,
    }


def test_execute_pack_skips_disabled_channels(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MOVEOUT_DISABLE_TICKETS", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_WHATSAPP", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_CALENDAR", "true")
    pack = {
        "tickets": [{"category": "Inspection", "home": "home-1"}],
        "emailsToSend": [{"recipient": "test@example.com", "subject": "Notice"}],
        "whatsappMessages": [{"phone": "+919876543210", "message": "Notice"}],
        "reminders": [{"subject": "Move-out", "startISO": "2026-11-30"}],
    }

    results = execute_pack(pack)

    output = capsys.readouterr().out
    assert "[tickets] MOVEOUT_DISABLE_TICKETS=true — skipped 1 tickets" in output
    assert "[whatsapp] MOVEOUT_DISABLE_WHATSAPP=true — skipped 1 messages" in output
    assert "[calendar] MOVEOUT_DISABLE_CALENDAR=true — skipped 1 reminders" in output
    assert "[THM] Would create ticket" not in output
    assert "[WhatsApp] Would send" not in output
    assert "[Calendar] Would create" not in output
    assert "[Graph] Would send email to t***@example.com" in output
    assert results == {
        "mode": "dry_run",
        "tickets_planned": 0,
        "tickets_skipped": 1,
        "emails_planned": 1,
        "whatsapp_planned": 0,
        "whatsapp_skipped": 1,
        "reminders_planned": 0,
        "reminders_skipped": 1,
    }


def test_execute_pack_skips_missing_email_recipients(capsys) -> None:
    results = execute_pack(
        {
            "emailsToSend": [{"recipient": None, "subject": "Owner notice"}],
        }
    )

    assert "Skipping email with no recipient" in capsys.readouterr().out
    assert results["emails_planned"] == 0


def test_execute_pack_rejects_live_delivery() -> None:
    with pytest.raises(NotImplementedError, match="only supports dry_run=True"):
        execute_pack({}, dry_run=False)


def test_execute_pack_rejects_malformed_action_lists() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        execute_pack({"tickets": {}})


def test_execute_pack_rejects_live_delivery_even_with_disabled_channels(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MOVEOUT_DISABLE_TICKETS", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_WHATSAPP", "true")
    monkeypatch.setenv("MOVEOUT_DISABLE_CALENDAR", "true")

    with pytest.raises(NotImplementedError, match="only supports dry_run=True"):
        execute_pack({}, dry_run=False)


def test_dry_run_switch_defaults_to_on(monkeypatch) -> None:
    monkeypatch.delenv("MOVEOUT_DRY_RUN", raising=False)

    assert is_dry_run_enabled()


def test_dry_run_switch_accepts_false_but_live_execution_stays_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MOVEOUT_DRY_RUN", "false")

    assert not is_dry_run_enabled()
    with pytest.raises(NotImplementedError, match="only supports dry_run=True"):
        execute_pack({}, dry_run=is_dry_run_enabled())


def test_dry_run_switch_rejects_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_DRY_RUN", "sometimes")

    with pytest.raises(ValueError, match="MOVEOUT_DRY_RUN"):
        is_dry_run_enabled()
