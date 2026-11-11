import pytest

from app.logic.engine import generate_move_out_pack


@pytest.fixture
def canonical_item() -> dict[str, str]:
    return {
        "graphMessageId": "12345",
        "society": "Prestige Park",
        "flat": "302",
        "moveOutDate": "November 30, 2026",
        "tenantName": "John Doe",
        "tenantEmail": "john@example.com",
        "ownerEmail": "owner.prestige@example.com",
        "ownerPhone": "+919876543210",
        "thm_home_id": "THM-HOME-302",
        "thm_project_id": "THM-PROJ-PP",
        "parseStatus": "success",
    }


def test_generates_move_out_pack(canonical_item: dict[str, str]) -> None:
    pack = generate_move_out_pack(canonical_item)

    assert len(pack["tickets"]) == 11
    assert pack["tickets"][0] == {
        "category": "Inspection",
        "home": "THM-HOME-302",
        "project": "THM-PROJ-PP",
        "status": "Open",
    }
    assert pack["tickets"][-1]["category"] == "Key Handover"
    assert pack["checklist"] == {
        "tab_name": "Prestige Park 302",
        "header_values": [
            "Prestige Park",
            "302",
            "John Doe",
            "November 30, 2026",
        ],
        "status_rows": ["Pending"] * 11,
    }
    assert len(pack["emailsToSend"]) == 2
    assert pack["emailsToSend"][0]["recipient"] == "john@example.com"
    assert pack["emailsToSend"][1]["recipient"] == "owner.prestige@example.com"
    assert len(pack["whatsappMessages"]) == 1
    assert pack["whatsappMessages"][0]["phone"] == "+919876543210"
    assert pack["reminders"] == [
        {
            "subject": "Move-Out Preparation: Prestige Park 302",
            "startISO": "2026-11-23T10:00:00+05:30",
            "endISO": "2026-11-23T10:30:00+05:30",
        },
        {
            "subject": "Move-Out Day: Prestige Park 302",
            "startISO": "2026-11-30T10:00:00+05:30",
            "endISO": "2026-11-30T10:30:00+05:30",
        }
    ]


def test_does_not_invent_whatsapp_recipient(canonical_item: dict[str, str]) -> None:
    canonical_item.pop("ownerPhone")

    pack = generate_move_out_pack(canonical_item)

    assert pack["whatsappMessages"] == []


def test_pack_uses_thm_contacts_and_redirects_email_only(
    canonical_item: dict[str, str],
    monkeypatch,
    capsys,
) -> None:
    canonical_item.update(
        {
            "ownerEmail": "owner@example.com",
            "ownerPhone": "9886004645",
            "tenantPhone": "8884764357",
            "associationEmail": (
                "gm@example.com; em@example.com; aso@example.com"
            ),
            "thm_home_id": "home-real",
            "thm_project_id": "project-real",
        }
    )
    monkeypatch.setenv("MOVEOUT_TEST_EMAIL", "qa@example.com")

    pack = generate_move_out_pack(canonical_item)

    assert [email["role"] for email in pack["emailsToSend"]] == [
        "tenant",
        "owner",
        "society",
        "society",
        "society",
    ]
    assert {email["recipient"] for email in pack["emailsToSend"]} == {
        "qa@example.com"
    }
    assert pack["emailsToSend"][0]["body"] == (
        "Dear John Doe, we received your notice for November 30, 2026."
    )
    assert [message["phone"] for message in pack["whatsappMessages"]] == [
        "+919886004645",
        "+918884764357",
    ]
    assert all(ticket["home"] == "home-real" for ticket in pack["tickets"])
    assert all(
        ticket["project"] == "project-real" for ticket in pack["tickets"]
    )
    output = capsys.readouterr().out
    assert "[email] test-mode: redirect owner@example.com -> qa@example.com" in output
    assert "[email] test-mode: redirect 8884764357" not in output


def test_requires_valid_move_out_date(canonical_item: dict[str, str]) -> None:
    canonical_item["moveOutDate"] = "not a date"

    with pytest.raises(ValueError, match="Unsupported moveOutDate format"):
        generate_move_out_pack(canonical_item)
