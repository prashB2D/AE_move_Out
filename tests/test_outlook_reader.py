"""Read visible Outlook inbox rows without opening or changing messages."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.browser.auth_state import restore_auth_cookies, save_auth_state
from app.browser.outlook_probe import (
    MESSAGE_ROW_SELECTOR,
    get_profile_path,
    is_outlook_mail_url,
    wait_for_user,
)

INBOX_URL = "https://outlook.office.com/mail/inbox"

EMPTY_INBOX_PATTERN = re.compile(
    r"(?:your )?inbox is empty"
    r"|no items to show"
    r"|there are no messages"
    r"|no messages"
    r"|you'?re all caught up"
    r"|nothing to show"
    r"|no mail yet",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
AUTHENTICATION_HOSTS = {
    "account.live.com",
    "login.live.com",
    "login.microsoft.com",
    "login.microsoftonline.com",
}

_ROW_SNAPSHOT_SCRIPT = """row => {
    const rowId = row.getAttribute('id') || null;
    const conId = row.getAttribute('data-convid') || null;
    const ariaLabel = row.getAttribute('aria-label') || '';
    const innerText = row.innerText || '';

    let sender = null;
    let senderAddress = null;
    for (const span of row.querySelectorAll('span[title]')) {
        const title = span.getAttribute('title') || '';
        if (title.includes('@')) {
            senderAddress = title;
            sender = (span.innerText || '').trim();
            break;
        }
    }

    let received = null;
    for (const span of row.querySelectorAll('span[title]')) {
        const title = span.getAttribute('title') || '';
        if (/\\d{1,2}[-/.]\\d{1,2}[-/.]\\d{2,4}/.test(title) || /^\\w{3}\\s/.test(title)) {
            received = title;
            break;
        }
    }

    const lines = innerText.split('\\n').map(line => line.trim()).filter(Boolean);
    let subject = lines[1] || null;
    let preview = lines.slice(3).join(' ').trim() || null;

    if (!subject && ariaLabel) {
        const stripped = ariaLabel
            .replace(/^Unread\\s+/, '')
            .replace(/\\s*No conversations selected\\s*$/, '');
        const match = stripped.match(/^(.*?)\\s+\\d{1,2}:\\d{2}\\s+(.*)$/);
        if (match) {
            const beforeTime = match[1];
            subject = sender && beforeTime.startsWith(sender)
                ? beforeTime.slice(sender.length).trim()
                : beforeTime;
            preview = match[2] || null;
        }
    }

    if (!received && ariaLabel) {
        const match = ariaLabel.match(/(\\d{1,2}:\\d{2})/);
        if (match) received = match[1];
    }

    return { rowId, conId, sender, senderAddress, subject, received, preview };
}"""


class OutlookReaderError(RuntimeError):
    """Base error for Outlook reader failures."""


class OutlookAuthenticationError(OutlookReaderError):
    """Raised when Outlook redirects to or displays a sign-in page."""


class OutlookReaderTimeoutError(OutlookReaderError):
    """Raised when the inbox does not finish loading before the timeout."""


def _address_record(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    address_match = EMAIL_PATTERN.search(value)
    address = address_match.group(0) if address_match else None
    name = value
    if address_match:
        name = value[: address_match.start()] + value[address_match.end() :]
        name = name.strip(" \t<>(),;\"'")
    else:
        name = name.strip()
    if not name and not address:
        return None
    email_address: dict[str, str] = {}
    if name:
        email_address["name"] = name
    if address:
        email_address["address"] = address
    return {"emailAddress": email_address}


def _parse_message_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    row_id = snapshot.get("rowId")
    con_id = snapshot.get("conId")
    sender_name = snapshot.get("sender")
    sender_address = snapshot.get("senderAddress")
    sender_record = None
    if sender_name or sender_address:
        email_address: dict[str, str] = {}
        if sender_name:
            email_address["name"] = sender_name
        if sender_address:
            email_address["address"] = sender_address
        sender_record = {"emailAddress": email_address}
    web_link = (
        f"https://outlook.office.com/mail/inbox/id/{con_id}" if con_id else None
    )

    return {
        "id": row_id,
        "internetMessageId": None,
        "subject": snapshot.get("subject"),
        "from": sender_record,
        "receivedDateTime": snapshot.get("received"),
        "toRecipients": [],
        "bodyPreview": snapshot.get("preview"),
        "webLink": web_link,
        "conversationId": con_id,
    }


def _is_authentication_url(url: str) -> bool:
    return urlsplit(url).hostname in AUTHENTICATION_HOSTS


def _validate_outlook_page(url: str) -> None:
    parsed_url = urlsplit(url)
    safe_location = f"{parsed_url.hostname or '<unknown-host>'}{parsed_url.path}"
    if _is_authentication_url(url):
        print(f"[Outlook reader] Current page: {safe_location}")
        raise OutlookAuthenticationError(
            f"Outlook redirected to a sign-in page ({safe_location})."
        )
    if not is_outlook_mail_url(url):
        print(f"[Outlook reader] Current page: {safe_location}")
        raise OutlookReaderError(
            f"The browser is not on an Outlook mailbox page ({safe_location})."
        )


async def _ensure_logged_in(page: Any, timeout_ms: int = 120_000) -> None:
    # Wait a moment for any redirect to complete
    await asyncio.sleep(2.0)

    if is_outlook_mail_url(page.url) and "login" not in page.url.lower():
        return

    print(
        "\n"
        + "=" * 60
        + "\n>>> ACTION REQUIRED: Sign in to Outlook in the Chrome window.\n"
        ">>> 1. Complete your password and MFA steps.\n"
        ">>> 2. Wait until you can SEE the email rows.\n"
        ">>> 3. Then come back HERE and press Enter.\n"
        + "=" * 60
    )

    # Block the script until YOU press Enter
    await asyncio.to_thread(input, ">>> Press Enter here when the Inbox is visible: ")

    # After you press Enter, confirm we're on the inbox
    if not is_outlook_mail_url(page.url) or "login" in page.url.lower():
        raise OutlookAuthenticationError(
            f"Still not on the Outlook inbox (current URL: {page.url}). "
            "Make sure you are actually looking at your email list, then rerun."
        )


async def _wait_for_inbox_ready(page: Any, timeout_ms: int) -> Any:
    _validate_outlook_page(page.url)

    rows = page.locator(MESSAGE_ROW_SELECTOR)
    deadline = time.monotonic() + timeout_ms / 1000

    while True:
        _validate_outlook_page(page.url)
        try:
            await rows.first.wait_for(state="visible", timeout=2_000)
            return rows
        except PlaywrightTimeoutError:
            pass

        try:
            main_text = await page.locator('[role="main"]').first.inner_text(
                timeout=1_000
            )
        except (PlaywrightTimeoutError, PlaywrightError):
            main_text = ""
        if EMPTY_INBOX_PATTERN.search(main_text):
            return rows

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutlookReaderTimeoutError(
                "Timed out waiting for Outlook inbox messages."
            )
        await asyncio.sleep(min(0.2, remaining))


async def read_inbox(
    limit: int = 50,
    timeout_ms: int = 15_000,
    allow_login: bool = True,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if timeout_ms < 1:
        raise ValueError("timeout_ms must be at least 1")

    profile_path: Path = get_profile_path()
    profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                channel="chrome",
            )
            try:
                await restore_auth_cookies(context, profile_path)
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(
                        INBOX_URL,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except PlaywrightTimeoutError as error:
                    if not allow_login:
                        raise OutlookReaderTimeoutError(
                            "Timed out navigating to Outlook."
                        ) from error
                    print(
                        "Outlook navigation timed out. The browser will remain "
                        "open so you can complete sign-in manually."
                    )

                if allow_login:
                    await _ensure_logged_in(page, timeout_ms)
                else:
                    _validate_outlook_page(page.url)

                rows = await _wait_for_inbox_ready(page, timeout_ms)

                messages: list[dict[str, Any]] = []
                count = await rows.count()
                for index in range(min(count, limit)):
                    snapshot = await rows.nth(index).evaluate(_ROW_SNAPSHOT_SCRIPT)
                    messages.append(_parse_message_snapshot(snapshot))

                try:
                    await save_auth_state(context, profile_path)
                except PlaywrightError:
                    pass

                return messages
            finally:
                await context.close()
    except OutlookReaderError:
        raise
    except PlaywrightError as error:
        raise OutlookReaderError(
            "Playwright could not read the Outlook inbox."
        ) from error


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--timeout-ms", type=int, default=15_000)
    parser.add_argument("--no-login", action="store_true")
    args = parser.parse_args()
    messages = await read_inbox(
        limit=args.limit,
        timeout_ms=args.timeout_ms,
        allow_login=not args.no_login,
    )
    print(json.dumps(messages, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(_main())