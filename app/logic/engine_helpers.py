"""Date, text, and formatting helpers ported from the n8n engine."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _normalized_date(year: int, month: int, day: int) -> date:
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return date(year, month, 1) + timedelta(days=day - 1)


def parse_date(value: Any) -> date | None:
    """Match the source parser, including ordinal and day-first formats."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = re.sub(
        r"(\d{1,2})(?:st|nd|rd|th)\b",
        r"\1",
        str(value).strip(),
        flags=re.IGNORECASE,
    )
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return _normalized_date(*(int(part) for part in match.groups()))

    match = re.match(
        r"^(\d{1,2})[-/ .]+([A-Za-z]{3})[A-Za-z]*\.?[-/ .,]+(\d{4})",
        text,
    )
    if match:
        day, month_name, year = match.groups()
        month = next(
            (i for i, name in enumerate(MONTHS, 1) if name.lower() == month_name.lower()),
            0,
        )
        if month:
            return _normalized_date(int(year), month, int(day))

    match = re.match(
        r"^([A-Za-z]{3})[A-Za-z]*\.?[-/ ]+(\d{1,2}),?[-/ ]+(\d{4})",
        text,
    )
    if match:
        month_name, day, year = match.groups()
        month = next(
            (i for i, name in enumerate(MONTHS, 1) if name.lower() == month_name.lower()),
            0,
        )
        if month:
            return _normalized_date(int(year), month, int(day))

    match = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        return _normalized_date(year, month, day)

    try:
        # JavaScript Date(string) handles common English/ISO variants.
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y", "%Y-%m", "%Y/%m/%d", "%Y.%m.%d", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1)
        except ValueError:
            continue
    for fmt in (
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso_day(value: date) -> str:
    return value.isoformat()


def format_date(value: date | None) -> str:
    if value is None:
        return ""
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year} ({DAYS[(value.weekday() + 1) % 7]})"


def add_days(value: date, count: int) -> date:
    return value + timedelta(days=count)


def add_months(value: date, count: int) -> date:
    month_index = value.month - 1 + count
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return _normalized_date(year, month, value.day)


def is_working_day(value: date, config: dict[str, Any]) -> bool:
    if config["sundayOff"] and value.weekday() == 6:
        return False
    if config["saturdayOff"] and value.weekday() == 5:
        return False
    return iso_day(value) not in set(config["holidays"])


def workday_add(value: date, count: int, config: dict[str, Any]) -> date:
    current = value
    added = 0
    while added < count:
        current += timedelta(days=1)
        if is_working_day(current, config):
            added += 1
    return current


def ensure_workday(value: date, config: dict[str, Any]) -> date:
    while not is_working_day(value, config):
        value += timedelta(days=1)
    return value


def yes(value: Any) -> bool:
    return str(value if value is not None else "").strip().upper().startswith("Y")


def number(value: Any) -> float:
    text = re.sub(r"[, ]", "", str(value if value is not None else ""))
    match = re.match(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", text)
    return float(match.group()) if match else 0


def rupees(value: float) -> str:
    rounded = math.floor(value + 0.5)
    sign = "-" if rounded < 0 else ""
    digits = str(abs(rounded))
    if len(digits) > 3:
        last_three = digits[-3:]
        leading = digits[:-3]
        groups: list[str] = []
        while leading:
            groups.append(leading[-2:])
            leading = leading[:-2]
        digits = ",".join(reversed(groups)) + "," + last_three
    return f"Rs. {sign}{digits}"


def whatsapp_phone(value: Any, country_code: str = "91") -> str:
    digits = re.sub(r"\D", "", str(value if value is not None else ""))
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = country_code + digits
    return digits


def clean_tag(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value if value is not None else "")).upper()


def escape_html(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def safe_name(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", str(value))).strip()


def clean_tab(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\\/?*\[\]:']", "", str(value or ""))).strip()


def build_tab(society: str, flat: str) -> str:
    flat = clean_tab(flat)
    society = clean_tab(society)
    max_society = max(0, 31 - len(flat) - 1)
    if len(society) > max_society:
        society = society[:max_society].strip()
    tab = f"{society}-{flat}" if society and flat else society or flat or "Property"
    return tab[:31]


def encode_uri(value: str) -> str:
    return quote(value, safe=";/?:@&=+$,-_.!~*'()#").replace("#", "%23")


def excel_serial(value: date) -> int:
    return (value - date(1899, 12, 30)).days
