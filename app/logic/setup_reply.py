"""Detect and minimally represent manual-review setup replies."""

from __future__ import annotations

from typing import Any


def is_setup_reply(subject: str | None) -> bool:
    return "[mo-setup]" in (subject or "").lower()


def parse_setup_reply(email: dict[str, Any]) -> dict[str, Any]:
    """Mark a setup reply as received without interpreting its details block."""
    sender = email.get("from")
    email_address = sender.get("emailAddress") if isinstance(sender, dict) else None
    if not isinstance(email_address, dict):
        email_address = {}

    tenant_name = email_address.get("name")
    tenant_email = email_address.get("address")
    return {
        "graphMessageId": email.get("id"),
        "manualConfirmed": True,
        "parseStatus": "manual_reply_received",
        "society": None,
        "flat": None,
        "moveOutDate": None,
        "tenantName": tenant_name if isinstance(tenant_name, str) else None,
        "tenantEmail": tenant_email if isinstance(tenant_email, str) else None,
    }
