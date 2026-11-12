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
    OUTLOOK_HOST,
    get_profile_path,
    is_outlook_mail_url,
    wait_for_user,
)

FOLDER_URLS = {
    "inbox": f"https://{OUTLOOK_HOST}/mail/",
    "sentitems": f"https://{OUTLOOK_HOST}/mail/sentitems",
}
INBOX_URL = FOLDER_URLS["inbox"]

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
MAX_MID_READ_AUTH_RECOVERIES = 2
_ROW_SNAPSHOT_SCRIPT = r"""(row, folder) => {
    const rowId = row.getAttribute('id') || null;
    const conId = row.getAttribute('data-convid') || null;
    const ariaLabel = row.getAttribute('aria-label') || '';
    const innerText = row.innerText || '';

    let sender = null;
    let senderAddress = null;
    const isSentItems = folder === 'sentitems';
    const visible = element => element.getClientRects().length > 0;
    const senderElement = isSentItems ? null : [
        ...new Set([
            ...row.querySelectorAll('[class*="sender" i] span[title*="@"]'),
            ...row.querySelectorAll('[class*="from" i] span[title*="@"]'),
            ...row.querySelectorAll('[role="gridcell"] span[title*="@"]'),
            ...row.querySelectorAll('span[title*="@"]'),
        ]),
    ].find(visible);

    const emailPattern = /[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g;
    if (senderElement) {
        const title = senderElement.getAttribute('title') || '';
        const addressMatch = title.match(emailPattern);
        const address = addressMatch ? addressMatch[0] : null;
        if (address && !/^outlook_[A-Z0-9]+@outlook\.com$/i.test(address)) {
            senderAddress = address;
        }
        sender = (senderElement.innerText || '').trim();
        if (!sender) sender = title.replace(emailPattern, '').trim() || null;
    }

    const toRecipients = [];
    const seenRecipients = new Set();
    const recipientCandidates = isSentItems
        ? [
            ...row.querySelectorAll('[role="gridcell"] span'),
            ...row.querySelectorAll('[class*="recipient" i] span'),
            ...row.querySelectorAll('span[title*="@"]'),
        ]
        : [
            ...row.querySelectorAll('[class*="recipient" i] span[title*="@"]'),
            ...row.querySelectorAll('[class*="to" i] span[title*="@"]'),
            ...row.querySelectorAll('[aria-label*="recipient" i] span[title*="@"]'),
        ];
    for (const element of new Set(recipientCandidates)) {
        if (!visible(element)) continue;
        const text = (element.innerText || '').trim();
        const searchableText = [
            element.getAttribute('title') || '',
            element.getAttribute('aria-label') || '',
            text,
        ].join(' ');
        const addresses = searchableText.match(emailPattern) || [];
        for (const address of addresses) {
            const key = address.toLowerCase();
            if (seenRecipients.has(key)) continue;
            seenRecipients.add(key);
            toRecipients.push({
                name: text.replace(emailPattern, '').trim() || address,
                address,
            });
        }
    }
    if (isSentItems) {
        const sentLabel = ariaLabel.replace(/^Unread\s+/, '').trim();
        const leadingAddress = sentLabel.match(/^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/);
        if (leadingAddress) {
            const address = leadingAddress[0];
            const key = address.toLowerCase();
            if (!seenRecipients.has(key)) {
                seenRecipients.add(key);
                toRecipients.push({ name: address, address });
            }
        }
    }

    let received = null;
    for (const span of row.querySelectorAll('span[title]')) {
        const title = span.getAttribute('title') || '';
        if (/\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}/.test(title) || /^\w{3}\s/.test(title)) {
            received = title;
            break;
        }
    }

    const lines = innerText.split('\n').map(line => line.trim()).filter(Boolean);
    let subject = null;
    let preview = null;

    if (ariaLabel) {
        let a = ariaLabel
            .replace(/^Unread\s+/, '')
            .replace(/\s*No conversations selected\s*$/, '')
            .trim();

        if (isSentItems) {
            for (const recipient of toRecipients) {
                const prefix = recipient.address || recipient.name;
                if (prefix && a.toLowerCase().startsWith(prefix.toLowerCase())) {
                    a = a.slice(prefix.length).trim();
                }
            }
            a = a.replace(/^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\s+/, '');
        } else if (sender && a.startsWith(sender)) {
            a = a.slice(sender.length).trim();
        }

        const m = a.match(/^(.*?)\s+(\d{1,2}:\d{2})\s+(.*)$/);
        if (m) {
            subject = m[1].trim() || null;
            preview = m[3].trim() || null;
            if (!received) received = m[2];
        }
    }

    if (!subject) {
        const excludedNames = new Set([
            sender,
            ...toRecipients.map(recipient => recipient.name),
        ].filter(Boolean).map(name => name.toLowerCase()));
        subject = lines.find(line =>
            !excludedNames.has(line.toLowerCase())
            && !/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/.test(line)
            && !/^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?:\s|$)/.test(line)
            && !/^\w{3}\s+\d{1,2}[-/.]/.test(line)
        ) || null;
    }

    return {
        rowId,
        conId,
        sender,
        senderAddress,
        toRecipients,
        subject,
        received,
        preview,
    };
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


def _parse_message_snapshot(
    snapshot: dict[str, Any],
    folder: str = "inbox",
) -> dict[str, Any]:
    row_id = snapshot.get("rowId")
    con_id = snapshot.get("conId")
    sender_name = snapshot.get("sender")
    raw_sender_address = snapshot.get("senderAddress")
    address_match = (
        EMAIL_PATTERN.search(raw_sender_address)
        if isinstance(raw_sender_address, str)
        else None
    )
    sender_address = address_match.group(0) if address_match else None
    if sender_address and re.fullmatch(
        r"outlook_[A-Z0-9]+@outlook\.com", sender_address, re.IGNORECASE
    ):
        sender_address = None
    sender_record = None
    if sender_name or sender_address:
        email_address: dict[str, str] = {}
        if sender_name:
            email_address["name"] = sender_name
        if sender_address:
            email_address["address"] = sender_address
        sender_record = {"emailAddress": email_address}
    to_records = []
    raw_recipients = snapshot.get("toRecipients")
    if isinstance(raw_recipients, list):
        for recipient in raw_recipients:
            if not isinstance(recipient, dict):
                continue
            address = recipient.get("address")
            if not isinstance(address, str) or not address.strip():
                continue
            name = recipient.get("name")
            email_address = {"address": address.strip()}
            if isinstance(name, str) and name.strip():
                email_address["name"] = name.strip()
            to_records.append({"emailAddress": email_address})
    web_link = (
        f"https://{OUTLOOK_HOST}/mail/{folder}/id/{con_id}" if con_id else None
    )

    return {
        "id": row_id,
        "internetMessageId": None,
        "subject": snapshot.get("subject"),
        "from": sender_record,
        "receivedDateTime": snapshot.get("received"),
        "toRecipients": to_records,
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
            f"Outlook redirected to a sign-in page ({safe_location}). "
            "Complete any remaining Microsoft sign-in or MFA steps in Chrome, "
            "then rerun the reader."
        )
    if not is_outlook_mail_url(url):
        print(f"[Outlook reader] Current page: {safe_location}")
        raise OutlookReaderError(
            f"The browser is not on an Outlook mailbox page ({safe_location})."
        )


async def _ensure_logged_in(
    page: Any,
    timeout_ms: int,
    *,
    prompt: str,
    folder_url: str,
) -> None:
    print(prompt)
    await asyncio.to_thread(
        input,
        ">>> Press Enter when the Inbox rows are visible: ",
    )
    try:
        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=min(timeout_ms, 5_000),
        )
    except PlaywrightTimeoutError:
        pass

    deadline = time.monotonic() + timeout_ms / 1000
    while not is_outlook_mail_url(page.url):
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.25)

    _validate_outlook_page(page.url)
    if urlsplit(page.url).path.rstrip("/") != urlsplit(folder_url).path.rstrip("/"):
        try:
            await page.goto(
                folder_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            raise OutlookReaderTimeoutError(
                "Timed out opening the requested Outlook folder after sign-in."
            ) from error


async def _wait_for_inbox_ready(
    page: Any,
    timeout_ms: int,
    *,
    allow_login: bool = True,
    folder_url: str = INBOX_URL,
) -> Any:
    _validate_outlook_page(page.url)

    rows = page.locator(MESSAGE_ROW_SELECTOR)
    deadline = time.monotonic() + timeout_ms / 1000
    auth_recoveries = 0

    while True:
        if _is_authentication_url(page.url):
            if not allow_login or auth_recoveries >= MAX_MID_READ_AUTH_RECOVERIES:
                _validate_outlook_page(page.url)
            auth_recoveries += 1
            await _ensure_logged_in(
                page,
                timeout_ms,
                folder_url=folder_url,
                prompt=(
                    "[outlook] Microsoft redirected this session to sign-in. "
                    "Complete any remaining sign-in/MFA steps in Chrome, wait "
                    "until Outlook returns to the requested folder, then press "
                    "Enter here."
                ),
            )
            deadline = time.monotonic() + timeout_ms / 1000
            continue

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
    folder: str = "inbox",
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if timeout_ms < 1:
        raise ValueError("timeout_ms must be at least 1")
    if folder not in FOLDER_URLS:
        raise ValueError(
            f"Unsupported Outlook folder {folder!r}; "
            f"choose from {', '.join(FOLDER_URLS)}."
        )
    folder_url = FOLDER_URLS[folder]

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
                        folder_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except PlaywrightTimeoutError as error:
                    if not allow_login:
                        raise OutlookReaderTimeoutError(
                            "Timed out navigating to Outlook."
                        ) from error

                await asyncio.sleep(2.0)
                if not is_outlook_mail_url(page.url) or "login" in page.url.lower():
                    if not allow_login:
                        _validate_outlook_page(page.url)
                    await _ensure_logged_in(
                        page,
                        timeout_ms,
                        folder_url=folder_url,
                        prompt=(
                            "\n"
                            + "=" * 60
                            + "\n>>> ACTION REQUIRED: Sign in to Outlook in "
                            "the Chrome window.\n"
                            + ">>> 1. Type your email, password, and MFA.\n"
                            + ">>> 2. Wait until you can SEE the email rows.\n"
                            + ">>> 3. Come back HERE and press Enter.\n"
                            + "=" * 60
                        ),
                    )

                rows = await _wait_for_inbox_ready(
                    page,
                    timeout_ms,
                    allow_login=allow_login,
                    folder_url=folder_url,
                )

                messages: list[dict[str, Any]] = []
                count = await rows.count()
                for index in range(min(count, limit)):
                    snapshot = await rows.nth(index).evaluate(
                        _ROW_SNAPSHOT_SCRIPT,
                        folder,
                    )
                    messages.append(_parse_message_snapshot(snapshot, folder))

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


async def read_sent_items(
    limit: int = 100,
    timeout_ms: int = 15_000,
    allow_login: bool = True,
) -> list[dict[str, Any]]:
    """Read visible Sent Items rows using the authorized Outlook browser session."""
    return await read_inbox(
        limit=limit,
        timeout_ms=timeout_ms,
        allow_login=allow_login,
        folder="sentitems",
    )


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
