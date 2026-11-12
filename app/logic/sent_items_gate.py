"""Match move-out properties against messages visible in Outlook Sent Items."""
from __future__ import annotations

import os
import re
from typing import Any

from app.browser.outlook_reader import OutlookReaderError, read_sent_items


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _text_of(message: dict[str, Any]) -> str:
    return " ".join(
        value
        for value in (message.get("subject"), message.get("bodyPreview"))
        if isinstance(value, str)
    )


def _to_addresses(message: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    for recipient in message.get("toRecipients") or []:
        if not isinstance(recipient, dict):
            continue
        email_address = recipient.get("emailAddress")
        if not isinstance(email_address, dict):
            continue
        address = email_address.get("address")
        if isinstance(address, str) and address.strip():
            addresses.append(address.strip().lower())
    return addresses


def email_check_enabled() -> bool:
    value = os.environ.get("MOVEOUT_SENT_ITEMS_CHECK", "true").strip().lower()
    return value in ("1", "true", "yes", "on")


async def read_sent_items_for_gate() -> list[dict[str, Any]]:
    try:
        messages = await read_sent_items(limit=200, timeout_ms=30_000)
    except OutlookReaderError as error:
        print(f"[gate] read_sent_items failed: {error} — passing through")
        return []

    return messages


def is_already_handled(
    messages: list[dict[str, Any]],
    society: str | None,
    flat: str | None,
    tenant_email: str | None,
    owner_email: str | None,
    tenant_name: str | None,
    owner_name: str | None,
) -> tuple[bool, str]:
    """Return whether a Sent Items row matches this property."""
    print(
        f"[gate] checking {len(messages)} sent items "
        f"(email_check={str(email_check_enabled()).lower()})"
    )
    society_n = _norm(society)
    flat_n = _norm(flat)
    if not society_n or not flat_n:
        return False, ""

    tenant_email_n = (tenant_email or "").strip().lower()
    owner_email_n = (owner_email or "").strip().lower()
    tenant_name_n = _norm(tenant_name)
    owner_name_n = _norm(owner_name)
    email_enabled = email_check_enabled()

    for message in messages:
        subject = message.get("subject")
        subject_n = _norm(subject if isinstance(subject, str) else None)
        if not any(
            hint in subject_n
            for hint in ("moveoutnotice", "acknowledgment")
        ):
            continue

        text_n = _norm(_text_of(message))
        if society_n not in text_n or flat_n not in text_n:
            continue

        if email_enabled:
            recipients = _to_addresses(message)
            if tenant_email_n and tenant_email_n in recipients:
                return True, f"email-match tenant: {subject}"
            if owner_email_n and owner_email_n in recipients:
                return True, f"email-match owner: {subject}"

        if tenant_name_n and tenant_name_n in text_n:
            return True, f"name-match tenant: {subject}"
        if owner_name_n and owner_name_n in text_n:
            return True, f"name-match owner: {subject}"

    return False, ""
