"""Send test emails through the authorized Outlook web session."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.adapters.notifier import LogNotifier, Notifier, build_email_followups
from app.browser.auth_state import restore_auth_cookies
from app.browser.outlook_probe import (
    get_outlook_host,
    get_profile_path,
    is_outlook_mail_url,
    wait_for_user,
)

TEST_EMAIL = "kingprashu123@gmail.com"
DEFAULT_TIMEOUT_MS = 120_000


def _require_test_email(test_email: str | None) -> str:
    if not isinstance(test_email, str) or not test_email.strip():
        raise RuntimeError(
            "--test-email is required for safety; refusing to send."
        )
    if test_email.strip().casefold() != TEST_EMAIL:
        raise RuntimeError(
            f"For safety, --test-email must be {TEST_EMAIL}; refusing to send."
        )
    return TEST_EMAIL


def _extract_pack(data: Any) -> dict[str, Any]:
    pack: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("pack"), dict):
            pack = data["pack"]
        elif isinstance(data.get("auto_processed"), list):
            processed = data["auto_processed"]
            if processed and isinstance(processed[0], dict):
                pack = processed[0].get("pack")
        elif "emailsToSend" in data:
            pack = data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        pack = data[0].get("pack", data[0])

    if not isinstance(pack, dict):
        raise ValueError(
            "Pack file must contain a pack object or auto_processed[0].pack."
        )

    emails = pack.get("emailsToSend", [])
    if not isinstance(emails, list):
        raise ValueError("pack.emailsToSend must be a list.")
    if any(not isinstance(email, dict) for email in emails):
        raise ValueError("Every item in pack.emailsToSend must be an object.")
    return pack


# Candidate selectors, most specific first. Labels use exact / prefix matches
# (never substring) so they can't hit unrelated hidden inputs such as
# "Bottom ..." or "Go to ...", and every match is filtered to visible elements.
NEW_MAIL_SELECTORS = (
    'button[aria-label="New mail"]',
    'button[aria-label="New message"]',
    '[data-testid="newMessage"]',
    'button:has-text("New mail")',
    '[role="button"][aria-label^="New mail" i]',
)
TO_SELECTORS = (
    # Verified against live Outlook web (Nov 2026): contenteditable div, no role.
    'div[contenteditable="true"][aria-label="To"]',
    'div[role="textbox"][aria-label="To"]',
    'input[aria-label="To"]',
    '[aria-label="To"] input',
    '[aria-label="To"] [contenteditable="true"]',
    '[contenteditable="true"][aria-label^="To" i]',
    'input[aria-label^="To" i]',
    'input[placeholder^="To" i]',
)
SUBJECT_SELECTORS = (
    'input[aria-label="Subject"]',  # live: placeholder="Add a subject"
    'input[aria-label="Add a subject"]',
    'input[placeholder="Add a subject"]',
    'input[aria-label^="Subject" i]',
    'input[placeholder*="subject" i]',
    'input[aria-label*="subject" i]',
)
BODY_SELECTORS = (
    'div[role="textbox"][aria-label="Message body"]',
    '[contenteditable="true"][aria-label^="Message body" i]',
    '[contenteditable="true"][aria-label*="message body" i]',
    '[contenteditable="true"][aria-label*="body" i]',
)
SEND_SELECTORS = (
    'button[aria-label="Send"]',
    'button[title^="Send" i]',
    '[role="button"][aria-label="Send"]',
    'button:has-text("Send")',
)

DEFAULT_DUMP_PATH = Path("/tmp/compose_dump.html")
COMPOSE_OPEN_TIMEOUT_MS = 15_000
# Large HTML bodies (~140KB, inline images) take Outlook a while to submit.
SEND_CONFIRM_TIMEOUT_MS = 60_000
POST_PASTE_SETTLE_S = 5.0
POST_SEND_SETTLE_S = 5.0


async def _find_visible(
    page: Any,
    selectors: tuple[str, ...],
    timeout_ms: int,
    what: str,
) -> Any:
    """Return the first visible element matching any selector, polling."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        for selector in selectors:
            locator = page.locator(selector).filter(visible=True)
            if await locator.count() > 0:
                return locator.first
        if loop.time() >= deadline:
            raise RuntimeError(
                f"Could not find the {what} in Outlook compose "
                f"(tried {len(selectors)} selectors). Re-run with "
                "--dump-compose-html to inspect the real DOM."
            )
        await asyncio.sleep(0.25)


async def _wait_for_compose_open(
    page: Any, timeout_ms: int = COMPOSE_OPEN_TIMEOUT_MS
) -> Any:
    """Wait until the compose pane is rendered; return its To field."""
    return await _find_visible(page, TO_SELECTORS, timeout_ms, "To field")


async def _click_new_mail(page: Any) -> None:
    """Click Outlook's New mail button (once)."""
    button = await _find_visible(
        page, NEW_MAIL_SELECTORS, 10_000, "New mail button"
    )
    await button.click(timeout=5_000)


async def _open_compose(page: Any) -> Any:
    """Open the composer and return the To field once it is visible."""
    await _click_new_mail(page)
    return await _wait_for_compose_open(page)


async def _fill_compose(
    page: Any,
    to: str,
    cc: str,
    subject: str,
    html_body: str,
) -> None:
    """Fill the compose pane, refusing any non-test CC recipient."""
    if cc.strip():
        raise RuntimeError("CC recipients are disabled for test-only sending.")

    # To: Outlook's recipient well is a contenteditable picker, so type real
    # keystrokes (fill() doesn't trigger the picker) and press Enter to commit.
    to_field = await _find_visible(page, TO_SELECTORS, 5_000, "To field")
    await to_field.click(timeout=5_000)
    await page.keyboard.type(to, delay=30)
    await page.keyboard.press("Enter")
    # Outlook keeps Send aria-disabled until a recipient is committed. If Enter
    # didn't commit (suggestion list not shown yet), Tab resolves the text.
    if not await _wait_send_enabled(page, 5_000):
        await page.keyboard.press("Tab")
        if not await _wait_send_enabled(page, 5_000):
            raise RuntimeError(
                "Recipient was not committed in the To field (Send stayed "
                "disabled); refusing to continue."
            )

    # fill()/focus() don't need a pointer hit, so the recipient suggestion
    # dropdown overlapping these fields can't intercept them.
    subject_field = await _find_visible(
        page, SUBJECT_SELECTORS, 5_000, "Subject field"
    )
    await subject_field.fill(subject, timeout=5_000)

    body_field = await _find_visible(
        page, BODY_SELECTORS, 5_000, "message body"
    )
    await body_field.focus()
    await page.evaluate(
        """async (html) => {
            const blob = new Blob([html], {type: 'text/html'});
            const item = new ClipboardItem({'text/html': blob});
            await navigator.clipboard.write([item]);
        }""",
        html_body,
    )
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    await page.keyboard.press(f"{modifier}+V")
    await _wait_body_images_loaded(body_field)


async def _wait_send_enabled(page: Any, timeout_ms: int) -> bool:
    """Poll until Outlook's Send button is not aria-disabled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        for selector in SEND_SELECTORS:
            locator = page.locator(selector).filter(visible=True)
            if await locator.count() > 0:
                state = await locator.first.get_attribute("aria-disabled")
                if state != "true":
                    return True
                break
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _wait_body_images_loaded(body_field: Any, timeout_ms: int = 30_000) -> None:
    """Wait until pasted content (incl. base64 images) has rendered."""
    try:
        await body_field.evaluate(
            """(el, ms) => new Promise((resolve) => {
                const start = Date.now();
                const check = () => {
                    const imgs = [...el.querySelectorAll('img')];
                    const done = el.innerText.trim().length > 0
                        && imgs.every((img) => img.complete);
                    if (done || Date.now() - start > ms) resolve(done);
                    else setTimeout(check, 250);
                };
                check();
            })""",
            timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass
    await asyncio.sleep(POST_PASTE_SETTLE_S)


async def _send_current(page: Any) -> None:
    """Click Send once and wait for Outlook to close the compose pane."""
    body_field = await _find_visible(
        page, BODY_SELECTORS, 5_000, "message body"
    )
    if not await _wait_send_enabled(page, 30_000):
        raise RuntimeError("Send button stayed disabled; nothing was sent.")
    send_button = await _find_visible(
        page, SEND_SELECTORS, 5_000, "Send button"
    )
    await send_button.click(timeout=10_000)
    # Never retry after the click: a second click could send a duplicate.
    try:
        await body_field.wait_for(
            state="hidden", timeout=SEND_CONFIRM_TIMEOUT_MS
        )
    except PlaywrightTimeoutError as error:
        shot = Path("/tmp/mailer_send_unconfirmed.png")
        try:
            await page.screenshot(path=str(shot))
        except Exception:
            shot = None
        raise RuntimeError(
            "Send was clicked but the compose pane did not close within "
            f"{SEND_CONFIRM_TIMEOUT_MS // 1000}s. It MAY still have sent - check "
            "Sent Items before retrying (not retried, to avoid duplicates)."
            + (f" Screenshot: {shot}" if shot else "")
        ) from error
    # Let Outlook finish submitting before the browser is closed.
    await asyncio.sleep(POST_SEND_SETTLE_S)


_FIELD_SUMMARY_JS = """() => {
    const sel = 'input, textarea, button, [contenteditable="true"], '
        + '[role="textbox"], [role="combobox"], [role="dialog"]';
    const out = [];
    for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        out.push({
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role'),
            ariaLabel: el.getAttribute('aria-label'),
            placeholder: el.getAttribute('placeholder'),
            title: el.getAttribute('title'),
            id: el.id || null,
            testid: el.getAttribute('data-testid'),
            contenteditable: el.getAttribute('contenteditable'),
            text: (el.innerText || '').trim().slice(0, 40),
        });
    }
    return out;
}"""

_COMPOSE_HTML_JS = """() => {
    const anchor = document.querySelector(
        '[aria-label="To"], [aria-label="Message body"], '
        + '[aria-label="Add a subject"], [role="dialog"]');
    if (!anchor) return document.body.outerHTML;
    // Climb until the container holds both a recipient area and a body.
    let el = anchor;
    while (el.parentElement && el !== document.body) {
        el = el.parentElement;
        if (el.querySelector('[contenteditable="true"]')
            && el.querySelector('button[aria-label="Send"], button[title^="Send"]')) {
            return el.outerHTML;
        }
    }
    return document.body.outerHTML;
}"""


async def _dump_compose(page: Any, dump_path: Path) -> None:
    """Write compose HTML + a visible-field summary for selector debugging."""
    html = await page.evaluate(_COMPOSE_HTML_JS)
    fields = await page.evaluate(_FIELD_SUMMARY_JS)
    dump_path.write_text(html, encoding="utf-8")
    fields_path = dump_path.with_suffix(".fields.json")
    fields_path.write_text(json.dumps(fields, indent=2), encoding="utf-8")
    print(f"[mailer] Compose HTML dumped to {dump_path} ({len(html)} chars)")
    print(f"[mailer] Visible field summary written to {fields_path}")
    for field in fields:
        if field["tag"] == "button" and not field["ariaLabel"]:
            continue
        print(
            "  {tag:<8} role={role} aria-label={ariaLabel!r} "
            "placeholder={placeholder!r} editable={contenteditable}".format(
                **field
            )
        )


async def send_one(
    to: str,
    cc: str,
    subject: str,
    html_body: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    dump_path: Path | None = None,
) -> None:
    """Send one test email through Outlook web.

    With ``dump_path`` set, open compose, dump its DOM, and return WITHOUT
    typing or sending anything.
    """
    test_recipient = _require_test_email(to)
    if cc.strip():
        raise RuntimeError("CC recipients are disabled for test-only sending.")

    profile_path = get_profile_path()
    profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    outlook_host = get_outlook_host()
    outlook_url = f"https://{outlook_host}/mail/"

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            channel="chrome",
        )
        try:
            await restore_auth_cookies(context, profile_path)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                outlook_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            await asyncio.sleep(2.0)

            if not is_outlook_mail_url(page.url):
                await wait_for_user("Sign in to Outlook, then press Enter: ")
                if not is_outlook_mail_url(page.url):
                    raise RuntimeError(
                        "Outlook sign-in was not completed; refusing to send."
                    )

            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"],
                origin=f"https://{outlook_host}",
            )

            if dump_path is not None:
                await _click_new_mail(page)
                try:
                    await _wait_for_compose_open(page)
                except RuntimeError as error:
                    print(f"[mailer] {error} Dumping page anyway.")
                    await asyncio.sleep(3.0)
                await _dump_compose(page, dump_path)
                return

            await _open_compose(page)
            await _fill_compose(
                page,
                to=test_recipient,
                cc="",
                subject=subject,
                html_body=html_body,
            )

            print(f"[mailer] Sending: {subject} -> {test_recipient}")
            await _send_current(page)
            print(f"[mailer] Sent: {subject}")
        finally:
            await context.close()


def _select_emails(
    pack: dict[str, Any], include_scheduled: bool
) -> tuple[list[dict[str, Any]], int]:
    """Return (emails to send, number skipped as scheduled)."""
    emails = pack["emailsToSend"]
    if include_scheduled:
        return list(emails), 0
    selected = [email for email in emails if email.get("immediate") is True]
    return selected, len(emails) - len(selected)


async def send_pack_emails(
    pack: dict[str, Any],
    test_email: str | None = None,
    limit: int | None = None,
    *,
    include_scheduled: bool = False,
    notifier: Notifier | None = None,
) -> dict[str, int]:
    """Send the pack's immediate emails to the fixed test address only.

    Emails with ``immediate`` not exactly True are scheduled for later and are
    skipped unless ``include_scheduled``. After each successful send, the
    notifier (log-only by default) gets one follow-up per recipient role.
    """
    test_recipient = _require_test_email(test_email)
    checked_pack = _extract_pack(pack)
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer.")
    notifier = notifier or LogNotifier()

    emails, skipped = _select_emails(checked_pack, include_scheduled)
    total = len(checked_pack["emailsToSend"])
    if limit is not None:
        emails = emails[:limit]
    print(
        f"[mailer] {total} email(s) in pack: sending {len(emails)}, "
        f"skipping {skipped} scheduled"
    )
    sent = 0
    failed = 0

    for email in emails:
        subject = email.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            print("[mailer] Skipping email (no subject)")
            failed += 1
            continue

        html_body = email.get("html") or email.get("body") or ""
        if not isinstance(html_body, str):
            print(f"[mailer] Skipping email (invalid body): {subject}")
            failed += 1
            continue

        try:
            await send_one(
                to=test_recipient,
                cc="",
                subject=subject,
                html_body=html_body,
            )
            sent += 1
        except Exception as error:
            print(f"[mailer] ERROR sending '{subject}': {error}")
            failed += 1
            continue

        for followup in build_email_followups(email, checked_pack):
            try:
                await notifier.notify(followup)
            except Exception as error:  # a nudge must never fail the email
                print(f"[notify] ERROR for {followup.role}: {error}")

    return {"sent": sent, "failed": failed}


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Send pack emails through Outlook to the fixed test inbox."
    )
    parser.add_argument(
        "--pack-file",
        required=True,
        help="Path to JSON containing a pack with emailsToSend",
    )
    parser.add_argument(
        "--test-email",
        required=True,
        help=f"Required safety override; must be {TEST_EMAIL}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N emails in the pack",
    )
    parser.add_argument(
        "--include-scheduled",
        action="store_true",
        help="Also send emails with immediate=false (default: immediate only)",
    )
    parser.add_argument(
        "--dump-compose-html",
        nargs="?",
        const=str(DEFAULT_DUMP_PATH),
        default=None,
        metavar="PATH",
        help=(
            "Open compose, dump its HTML (default "
            f"{DEFAULT_DUMP_PATH}) and exit WITHOUT sending"
        ),
    )
    args = parser.parse_args()
    test_email = _require_test_email(args.test_email)

    if args.dump_compose_html:
        await send_one(
            to=test_email,
            cc="",
            subject="",
            html_body="",
            dump_path=Path(args.dump_compose_html),
        )
        print("[mailer] Dump complete; nothing was sent.")
        return

    data = json.loads(Path(args.pack_file).read_text(encoding="utf-8"))
    pack = _extract_pack(data)
    result = await send_pack_emails(
        pack,
        test_email=test_email,
        limit=args.limit,
        include_scheduled=args.include_scheduled,
    )
    print(f"\n[mailer] Done: {result['sent']} sent, {result['failed']} failed")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
