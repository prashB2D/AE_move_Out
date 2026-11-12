"""Post-send notification hook (WhatsApp later; log-only for now).

After an email is sent, the mailer asks ``build_email_followups`` who should
get a short "we've emailed you" nudge, then hands each one to a ``Notifier``.
Today the only implementation is ``LogNotifier`` (prints, sends nothing). To
integrate WhatsApp later, add a class with the same ``notify`` method that
calls the Meta API and pass it to ``send_pack_emails(notifier=...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_PHONE_KEYS = {"owner": "ownerPhone", "tenant": "tenantPhone"}


@dataclass(frozen=True)
class Followup:
    role: str  # "owner" | "tenant"
    phone: str
    message: str
    email_subject: str


class Notifier(Protocol):
    async def notify(self, followup: Followup) -> None: ...


def _roles(email: dict[str, Any]) -> list[str]:
    role = str(email.get("role") or "").lower()
    return [r for r in ("owner", "tenant") if r in role]


def build_email_followups(
    email: dict[str, Any], pack: dict[str, Any]
) -> list[Followup]:
    """One nudge per recipient role of ``email`` that has a phone in the pack."""
    followups = []
    for role in _roles(email):
        phone = str(pack.get(_PHONE_KEYS[role]) or "").strip()
        if not phone:
            continue
        followups.append(
            Followup(
                role=role,
                phone=phone,
                message=(
                    "Hello, we have sent you an email regarding the move-out "
                    "process. Please check your inbox."
                ),
                email_subject=str(email.get("subject") or ""),
            )
        )
    return followups


def _mask(phone: str) -> str:
    return "*" * max(len(phone) - 4, 0) + phone[-4:]


class LogNotifier:
    """Dry-run notifier: logs what would be sent; contacts nobody."""

    async def notify(self, followup: Followup) -> None:
        print(
            f"[notify:dry-run] WhatsApp to {followup.role} "
            f"{_mask(followup.phone)}: {followup.message}"
        )
