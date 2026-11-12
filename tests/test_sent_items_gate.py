import asyncio

from app.browser.outlook_reader import OutlookReaderError
from app.logic import sent_items_gate


PROPERTY_TEXT = "Move-Out Notice for Krishna Brookfield Flat T-17"


def _sent_message(
    *,
    subject: str = PROPERTY_TEXT,
    body_preview: str = "",
    recipients: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "subject": subject,
        "bodyPreview": body_preview,
        "toRecipients": recipients or [],
    }


def _recipient(address: str) -> dict[str, object]:
    return {"emailAddress": {"address": address}}


def test_email_match_tenant_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "true")
    handled, reason = sent_items_gate.is_already_handled(
        [_sent_message(recipients=[_recipient("tenant@example.com")])],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert handled is True
    assert reason.startswith("email-match tenant:")


def test_email_match_owner_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "true")
    handled, reason = sent_items_gate.is_already_handled(
        [_sent_message(recipients=[_recipient("owner@example.com")])],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        "owner@example.com",
        "Atharva Singh",
        "Owner Name",
    )

    assert handled is True
    assert reason.startswith("email-match owner:")


def test_name_match_is_fallback_after_email_no_match(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "true")
    handled, reason = sent_items_gate.is_already_handled(
        [
            _sent_message(
                body_preview="Acknowledgment sent to Atharva Singh"
            )
        ],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert handled is True
    assert reason.startswith("name-match tenant:")


def test_no_email_or_name_match_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "true")
    handled, reason = sent_items_gate.is_already_handled(
        [_sent_message(recipients=[_recipient("other@example.com")])],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert handled is False
    assert reason == ""


def test_toggle_off_skips_email_match(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "false")
    handled, reason = sent_items_gate.is_already_handled(
        [_sent_message(recipients=[_recipient("tenant@example.com")])],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Different Name",
        None,
    )

    assert handled is False
    assert reason == ""


def test_toggle_off_still_uses_name_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "false")
    handled, reason = sent_items_gate.is_already_handled(
        [_sent_message(body_preview="Acknowledgment for Atharva Singh")],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert handled is True
    assert reason.startswith("name-match tenant:")


def test_empty_sent_items_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("MOVEOUT_SENT_ITEMS_CHECK", "true")
    handled, reason = sent_items_gate.is_already_handled(
        [],
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert handled is False
    assert reason == ""


def test_sent_items_reader_error_fails_open(monkeypatch, capsys) -> None:
    async def fail_read(**_kwargs):
        raise OutlookReaderError("Outlook is unavailable")

    monkeypatch.setattr(sent_items_gate, "read_sent_items", fail_read)

    rows = asyncio.run(sent_items_gate.read_sent_items_for_gate())
    handled, reason = sent_items_gate.is_already_handled(
        rows,
        "Krishna Brookfield",
        "T-17",
        "tenant@example.com",
        None,
        "Atharva Singh",
        None,
    )

    assert rows == []
    assert handled is False
    assert reason == ""
    assert "passing through" in capsys.readouterr().out
