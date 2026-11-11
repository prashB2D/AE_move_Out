"""Safely preview downstream actions for a generated move-out pack."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def _items(pack: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = pack.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"pack[{key!r}] must be a list.")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Every item in pack[{key!r}] must be an object.")
    return value


def _masked_address(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def _masked_phone(value: str) -> str:
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def is_channel_disabled(channel: str) -> bool:
    """Return whether the named outbound channel has its kill switch enabled."""
    flag = f"MOVEOUT_DISABLE_{channel.upper()}"
    return os.environ.get(flag, "").strip().casefold() == "true"


def is_dry_run_enabled() -> bool:
    """Read the dry-run switch; live execution remains unsupported below."""
    value = os.environ.get("MOVEOUT_DRY_RUN", "true").strip().casefold()
    if value not in {"true", "false"}:
        raise ValueError("MOVEOUT_DRY_RUN must be set to 'true' or 'false'.")
    return value == "true"


def execute_pack(
    pack: dict[str, Any],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Print a redacted action preview; live delivery is intentionally disabled."""
    if not dry_run:
        raise NotImplementedError(
            "Live downstream adapters are not configured; execute_pack only "
            "supports dry_run=True."
        )

    tickets = _items(pack, "tickets")
    emails = _items(pack, "emailsToSend")
    messages = _items(pack, "whatsappMessages")
    reminders = _items(pack, "reminders")
    tickets_disabled = is_channel_disabled("tickets")
    whatsapp_disabled = is_channel_disabled("whatsapp")
    calendar_disabled = is_channel_disabled("calendar")

    print("\n--- Sending Pack (Dry Run: True) ---")
    if tickets_disabled:
        print(
            f"[tickets] MOVEOUT_DISABLE_TICKETS=true — "
            f"skipped {len(tickets)} tickets"
        )
    else:
        for ticket in tickets:
            category = ticket.get("category")
            if not isinstance(category, str) or not category.strip():
                raise ValueError("Every ticket must have a non-empty category.")
            print(
                f"[THM] Would create ticket: {category} "
                f"for home {ticket.get('home') or '<missing>'}"
            )

    emails_planned = 0
    for email in emails:
        recipient = email.get("recipient")
        if not isinstance(recipient, str) or not recipient.strip():
            print("[Graph] Skipping email with no recipient.")
            continue
        subject = email.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("Every email to send must have a non-empty subject.")
        print(
            f"[Graph] Would send email to {_masked_address(recipient.strip())} "
            f"(Subject: {subject})"
        )
        emails_planned += 1

    messages_planned = 0
    if whatsapp_disabled:
        print(
            f"[whatsapp] MOVEOUT_DISABLE_WHATSAPP=true — "
            f"skipped {len(messages)} messages"
        )
    else:
        for message in messages:
            phone = message.get("phone")
            text = message.get("message")
            if not isinstance(phone, str) or not phone.strip():
                print("[WhatsApp] Skipping message with no phone number.")
                continue
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Every WhatsApp message must have non-empty text.")
            print(
                f"[WhatsApp] Would send message to "
                f"{_masked_phone(phone.strip())}."
            )
            messages_planned += 1

    if calendar_disabled:
        print(
            f"[calendar] MOVEOUT_DISABLE_CALENDAR=true — "
            f"skipped {len(reminders)} reminders"
        )
    else:
        for reminder in reminders:
            subject = reminder.get("subject")
            start = reminder.get("startISO")
            if not isinstance(subject, str) or not isinstance(start, str):
                raise ValueError("Every reminder must have a subject and startISO.")
            print(f"[Calendar] Would create reminder: {subject} at {start}")

    print("--- Finished Sending Pack ---\n")
    return {
        "mode": "dry_run",
        "tickets_planned": 0 if tickets_disabled else len(tickets),
        "tickets_skipped": len(tickets) if tickets_disabled else 0,
        "emails_planned": emails_planned,
        "whatsapp_planned": messages_planned,
        "whatsapp_skipped": len(messages) if whatsapp_disabled else 0,
        "reminders_planned": 0 if calendar_disabled else len(reminders),
        "reminders_skipped": len(reminders) if calendar_disabled else 0,
    }
