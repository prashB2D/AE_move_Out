"""Detect and parse staff replies to ``[MO-SETUP]`` review emails.

A reply carries the editable "MOVE-OUT DETAILS" block from the review email
(app/adapters/review_notifier.py). The first block in the reply is the one
staff edited; anything after it (the quoted original) is ignored.
"""

from __future__ import annotations

import html
import re
from typing import Any

_BLOCK_START = re.compile(r"-+\s*move-?out\s+details", re.I)
_BLOCK_END = re.compile(r"-+\s*end\s+move-?out\s+details", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PLACEHOLDER = re.compile(r"^(\[?confirm\]?|n/?a|-+|tbd|none|\?)?$", re.I)

# label pattern -> canonical key (order matters: "Tenant Name" before "Name")
_FIELDS = (
    (r"property\s*complex\s*name", "society"),
    (r"flat\s*(?:number|no\.?|#)?", "flat"),
    (r"vacate\s*date", "moveOutDate"),
    (r"tenant\s*name", "tenantName"),
    (r"tenant\s*email", "tenantEmail"),
    (r"tenant\s*phone", "tenantPhone"),
    (r"owner\s*name", "ownerName"),
    (r"owner\s*email", "ownerEmail"),
    (r"owner\s*phone", "ownerPhone"),
    (r"property\s*type[^:\n]*", "propertyType"),
)


def is_setup_reply(subject: str | None) -> bool:
    """Any email about a review (original or reply) - never a new notice."""
    return "[mo-setup]" in (subject or "").lower()


def is_review_reply(subject: str | None) -> bool:
    """A staff reply that may carry corrected details.

    Outlook's message list shows the conversation topic, which drops "Re:",
    so any inbox email with [MO-SETUP] in the subject counts. (The original
    review email is sent to staff and never lands in this inbox.)
    """
    return is_setup_reply(subject)


def _to_text(body: str) -> str:
    if "<" in body and ">" in body:
        body = re.sub(r"<\s*br\s*/?>", "\n", body, flags=re.I)
        body = re.sub(r"</\s*(p|div|li|tr|h[1-6]|pre)\s*>", "\n", body, flags=re.I)
        body = re.sub(r"<[^>]*>", " ", body)
    body = html.unescape(body).replace("\xa0", " ")
    # Plain-text quoting ("> Flat Number: ...") must not hide the labels.
    return "\n".join(line.lstrip("> \t") for line in body.splitlines())


def extract_details_block(body: str) -> str | None:
    text = _to_text(body or "")
    start = _BLOCK_START.search(text)
    if start is None:
        return None
    rest = text[start.end():]
    end = _BLOCK_END.search(rest)
    return rest[: end.start()] if end else rest


_LABELS = re.compile(
    "|".join(f"(?P<{key}>{label})[ \t]*:" for label, key in _FIELDS), re.I
)


def _values(block: str) -> dict[str, str]:
    """Value = text after 'Label:' up to the next label or line end, so a
    block flattened onto one line (list preview) still parses."""
    matches = list(_LABELS.finditer(block))
    values = {key: "" for _, key in _FIELDS}
    for index, match in enumerate(matches):
        key = match.lastgroup
        if values[key]:
            continue  # first occurrence wins
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        raw = block[match.end():stop].split("\n", 1)[0]
        value = re.sub(r"\s+", " ", raw).strip().strip("[]<>").strip()
        values[key] = "" if _PLACEHOLDER.match(value) else value
    return values


def parse_details_block(block: str) -> dict[str, Any]:
    values = _values(block)
    for key in ("tenantEmail", "ownerEmail"):
        found = _EMAIL.search(values[key])
        values[key] = found.group(0) if found else ""
    values["flat"] = re.sub(r"^(flat\s*#?|no\.?|number)\s*", "", values["flat"], flags=re.I)
    kind = values["propertyType"]
    values["propertyType"] = (
        "NonPM" if re.search(r"non\s*-?\s*pm", kind, re.I)
        else "PM" if re.search(r"\bpm\b", kind, re.I) else ""
    )
    return {key: (value or None) for key, value in values.items()}


def _sender(email: dict[str, Any]) -> tuple[str | None, str | None]:
    sender = email.get("from")
    address = sender.get("emailAddress") if isinstance(sender, dict) else None
    if not isinstance(address, dict):
        return None, None
    name, mail = address.get("name"), address.get("address")
    return (name if isinstance(name, str) else None,
            mail if isinstance(mail, str) else None)


def _reply_body(email: dict[str, Any]) -> str:
    body = email.get("body")
    if isinstance(body, dict):
        body = body.get("content")
    for candidate in (body, email.get("bodyText"), email.get("bodyPreview")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def parse_setup_reply(email: dict[str, Any]) -> dict[str, Any]:
    """Canonical data from a staff reply; ``manualConfirmed`` is always True.

    ``parseStatus`` is "success" only when the block names a society and flat;
    otherwise the reply stays in the setup_replies bucket for a human.
    """
    staff_name, staff_email = _sender(email)
    base: dict[str, Any] = {
        "graphMessageId": email.get("id"),
        "manualConfirmed": True,
        "staffReplyFrom": staff_email,
        "staffReplyName": staff_name,
        "society": None,
        "flat": None,
        "moveOutDate": None,
        "tenantName": None,
        "tenantEmail": None,
    }
    block = extract_details_block(_reply_body(email)) if is_review_reply(
        email.get("subject")) else None
    if block is None:
        return {**base, "parseStatus": "manual_reply_received",
                "reviewReason": "No MOVE-OUT DETAILS block found in the reply."}

    details = parse_details_block(block)
    result = {**base, **details}
    if not (details["society"] and details["flat"]):
        result["parseStatus"] = "manual_reply_incomplete"
        result["reviewReason"] = (
            "Reply received but Property Complex Name / Flat Number were blank."
        )
        return result
    result["parseStatus"] = "success"
    result["parseMethod"] = "manual_reply"
    result["needsReview"] = False
    return result
