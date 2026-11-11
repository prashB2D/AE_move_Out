"""Build a local move-out pack without performing external side effects."""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

INDIA_TIME = ZoneInfo("Asia/Kolkata")
TICKET_CATEGORIES = (
    "Inspection",
    "Deep Cleaning",
    "Security Deposit Refund",
    "Owner Notification",
    "Tenant Notification",
    "Move-Out Checklist",
    "Utility Bill Settlement",
    "Society Dues Clearance",
    "Inventory Check",
    "Painting / Repairs",
    "Key Handover",
)
DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
)


def _move_out_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("canonical_item must include a moveOutDate")

    for date_format in DATE_FORMATS:
        try:
            move_out_date = datetime.strptime(value.strip(), date_format).date()
            return datetime.combine(
                move_out_date,
                time(10, 0),
                tzinfo=INDIA_TIME,
            )
        except ValueError:
            continue

    raise ValueError(f"Unsupported moveOutDate format: {value!r}")


def _phone_with_india_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    phone = value.strip()
    digits = "".join(character for character in phone if character.isdigit())
    if phone.startswith("+"):
        return f"+{digits}" if digits else None
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return phone


def _email_recipient(recipient: Any) -> str | None:
    if not isinstance(recipient, str) or not recipient.strip():
        return None
    actual_recipient = recipient.strip()
    test_address = os.environ.get("MOVEOUT_TEST_EMAIL", "").strip()
    if not test_address:
        return actual_recipient
    print(
        f"[email] test-mode: redirect {actual_recipient} -> {test_address}"
    )
    return test_address


def generate_move_out_pack(canonical_item: dict[str, Any]) -> dict[str, Any]:
    """Generate the configured tickets, templates, checklist, and reminders."""
    society = canonical_item.get("society")
    flat = canonical_item.get("flat")
    if not isinstance(society, str) or not society.strip():
        raise ValueError("canonical_item must include a society")
    if not isinstance(flat, str) or not flat.strip():
        raise ValueError("canonical_item must include a flat")

    society = society.strip()
    flat = flat.strip()
    tenant_name = canonical_item.get("tenantName")
    if not isinstance(tenant_name, str) or not tenant_name.strip():
        tenant_name = "Tenant"
    else:
        tenant_name = tenant_name.strip()

    move_out_date = canonical_item.get("moveOutDate")
    move_out_start = _move_out_datetime(move_out_date)
    home_id = canonical_item.get("thm_home_id")
    project_id = canonical_item.get("thm_project_id")
    property_name = f"{society} {flat}"

    tickets = [
        {
            "category": category,
            "home": home_id,
            "project": project_id,
            "status": "Open",
        }
        for category in TICKET_CATEGORIES
    ]

    emails = [
        {
            "role": "tenant",
            "recipient": _email_recipient(canonical_item.get("tenantEmail")),
            "subject": f"Acknowledgment of Move-Out Notice - {property_name}",
            "body": (
                f"Dear {tenant_name}, we received your notice "
                f"for {move_out_date}."
            ),
            "immediate": True,
        },
        {
            "role": "owner",
            "recipient": _email_recipient(canonical_item.get("ownerEmail")),
            "subject": f"Tenant Move-Out Notice - {property_name}",
            "body": (
                f"Tenant {tenant_name} has submitted a notice "
                f"for {property_name}."
            ),
            "immediate": False,
        },
    ]
    association_emails = canonical_item.get("associationEmail")
    if isinstance(association_emails, str):
        for address in association_emails.split(";"):
            recipient = _email_recipient(address)
            if recipient is not None:
                emails.append(
                    {
                        "role": "society",
                        "recipient": recipient,
                        "subject": f"Tenant Move-Out Notice - {property_name}",
                        "body": (
                            f"Tenant {tenant_name} has submitted a move-out "
                            f"notice for {property_name} on {move_out_date}."
                        ),
                        "immediate": False,
                    }
                )

    whatsapp_messages: list[dict[str, Any]] = []
    for role, phone_field in (
        ("owner", "ownerPhone"),
        ("tenant", "tenantPhone"),
    ):
        phone = _phone_with_india_code(canonical_item.get(phone_field))
        if phone is not None:
            whatsapp_messages.append(
                {
                    "role": role,
                    "phone": phone,
                    "message": (
                        f"Move-out notice: {property_name}. "
                        f"Date: {move_out_date}."
                    ),
                    "immediate": True,
                }
            )

    reminder_start = move_out_start - timedelta(days=7)
    reminders = [
        {
            "subject": f"Move-Out Preparation: {property_name}",
            "startISO": reminder_start.isoformat(),
            "endISO": (reminder_start + timedelta(minutes=30)).isoformat(),
        },
        {
            "subject": f"Move-Out Day: {property_name}",
            "startISO": move_out_start.isoformat(),
            "endISO": (move_out_start + timedelta(minutes=30)).isoformat(),
        },
    ]

    return {
        "tickets": tickets,
        "checklist": {
            "tab_name": property_name,
            "header_values": [
                society,
                flat,
                canonical_item.get("tenantName"),
                move_out_date,
            ],
            "status_rows": ["Pending"] * len(TICKET_CATEGORIES),
        },
        "emailsToSend": emails,
        "whatsappMessages": whatsapp_messages,
        "reminders": reminders,
    }
