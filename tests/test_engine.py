from __future__ import annotations

from datetime import date

import pytest

from app.logic.engine import generate_move_out_pack
from app.logic.engine_config import get_config
from app.logic.engine_helpers import (
    add_months,
    build_tab,
    format_date,
    parse_date,
    rupees,
    whatsapp_phone,
    workday_add,
)


@pytest.fixture
def canonical_item() -> dict[str, str]:
    return {
        "graphMessageId": "12345",
        "society": "Prestige Park",
        "flat": "302",
        "noticeDate": "2026-11-11",
        "moveOutDate": "November 30, 2026",
        "tenantName": "John Doe",
        "tenantEmail": "john@example.com",
        "ownerEmail": "owner.prestige@example.com",
        "ownerPhone": "+919876543210",
        "thm_home_id": "THM-HOME-302",
        "thm_project_id": "THM-PROJ-PP",
    }


def test_generates_source_compatible_pack_and_preserves_pipeline_fields(
    canonical_item: dict[str, str],
) -> None:
    pack = generate_move_out_pack(canonical_item)

    assert len(pack["tickets"]) == 11
    assert pack["tickets"][0] == {
        "ticketBelongsTo": "Home",
        "ticketBelongsToRefId": "THM-HOME-302",
        "category": "Home Cleaning",
        "home": "THM-HOME-302",
        "project": "THM-PROJ-PP",
        "ticketCategory": "Home Cleaning",
        "ticketCategoryType": "Maintenance",
        "status": "open",
        "priority": "low",
        "notes": (
            "[DEEP CLEANING] Move-out job for Prestige Park 302 - "
            "keys handover 30-Nov-2026 (Mon)."
        ),
        "subCategory": "Deep cleaning",
        "jobDate": "2026-12-03",
        "deadlineDate": "2026-12-03",
    }
    assert pack["checklist"] == {
        "tab_name": "Prestige Park 302",
        "header_values": [
            "Prestige Park",
            "302",
            "John Doe",
            "November 30, 2026",
        ],
        "status_rows": ["Pending"] * 40,
        "values": pack["checklistValues"],
        "formulas": pack["checklistFormulas"],
        "rows": pack["checklistRows"],
        "status_col_c": pack["checklistStatusColC"],
        "email_col_h": pack["checklistEmailColH"],
        "auto_status_col_k": pack["checklistAutoStatusColK"],
        "range": pack["checklistRange"],
        "template_name": "TEMPLATE",
        "ticket_row": pack["checklistTicketRow"],
        "workbook_path": pack["checklistWorkbookPath"],
        "email_folder_parent": pack["emailFolderParent"],
    }
    assert len(pack["emailsToSend"]) == 7
    assert pack["emailsToSend"][0]["recipient"] == "owner.prestige@example.com"
    assert pack["emailsToSend"][0]["label"] == "Owner 1st email (notice received)"
    assert len(pack["whatsappMessages"]) == 5
    assert len(pack["reminders"]) == 8
    assert pack["reminders"][-2]["startISO"] == "2026-11-30T10:00:00"
    assert pack["emailsToSend"][0]["graphMessage"]["toRecipients"] == [
        {"emailAddress": {"address": "owner.prestige@example.com"}}
    ]


def test_whatsapp_messages_use_source_template_recipients(
    canonical_item: dict[str, str],
) -> None:
    canonical_item.update(
        {
            "ownerPhone": "9886004645",
            "tenantPhone": "8884764357",
        }
    )

    pack = generate_move_out_pack(canonical_item)

    assert len(pack["whatsapps"]) == 8
    assert [message["to"] for message in pack["whatsapps"]] == [
        "919886004645",
        "919886004645",
        "919886004645",
        "918884764357",
        "918884764357",
        "918884764357",
        "919886004645",
        "919886004645",
    ]
    assert pack["whatsappMessages"] is pack["whatsapps"]


def test_email_test_mode_redirects_sends_without_redirecting_saved_eml(
    canonical_item: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MOVEOUT_TEST_EMAIL", "qa@example.com")

    pack = generate_move_out_pack(canonical_item)

    assert all(email["to"] == "qa@example.com" for email in pack["emailsToSend"])
    assert all(email["cc"] == "" for email in pack["emailsToSend"])
    assert pack["emailFiles"][0]["content"].startswith(
        "To: john@example.com\r\n"
    )
    assert "[email] test-mode: redirect" in capsys.readouterr().out


def test_move_out_date_falls_back_to_notice_plus_22_workdays(
    canonical_item: dict[str, str],
) -> None:
    canonical_item.pop("moveOutDate")

    pack = generate_move_out_pack(canonical_item)

    assert pack["checklistHeaderValues"][1][-1] == 46363
    assert pack["tickets"][3]["jobDate"] == "2026-12-07"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-30", date(2026, 9, 30)),
        ("30th September, 2026", date(2026, 9, 30)),
        ("September 30th, 2026", date(2026, 9, 30)),
        ("13 Sep 2026", date(2026, 9, 13)),
        ("13/09/2026", date(2026, 9, 13)),
    ],
)
def test_parse_date_matches_source_formats(value: str, expected: date) -> None:
    assert parse_date(value) == expected


def test_parse_date_normalizes_javascript_calendar_overflow() -> None:
    assert parse_date("2026-02-31") == date(2026, 3, 3)


def test_date_format_workdays_month_add_and_safe_config_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = get_config()
    assert format_date(date(2026, 11, 30)) == "30-Nov-2026 (Mon)"
    assert workday_add(date(2026, 11, 27), 1, config) == date(2026, 11, 28)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 3, 3)
    assert build_tab("A very long apartment community name", "G-12") == (
        "A very long apartment comm-G-12"
    )
    assert rupees(1_234_567) == "Rs. 12,34,567"
    assert whatsapp_phone("09886004645") == "919886004645"

    monkeypatch.delenv("MOVEOUT_ENGINE_BANK_ACCOUNT_NUMBER", raising=False)
    monkeypatch.setenv("MOVEOUT_ENGINE_SERVICE_DESK_EMAIL", "service@example.test")
    configured = get_config()
    assert configured["bankAcNo"] == "[bank account number not configured]"
    assert configured["serviceDesk"] == "service@example.test"
