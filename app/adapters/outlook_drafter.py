"""Branch 3: create Outlook drafts for every non-immediate email in a pack.

* ``emailsToSchedule`` -> compose + Outlook "Schedule send" at ``sendAtISO``
  (Outlook itself sends it later). If scheduling fails, the email is kept as
  a plain draft instead.
* ``emailFiles`` (.eml)  -> compose + save as a plain draft (staff send later).

Safety: a test address is mandatory (``--test-email`` or MOVEOUT_TEST_EMAIL);
every To/CC is replaced by it. Nothing is ever sent immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import Parser
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from app.adapters.playwright_mailer import (
    BODY_SELECTORS,
    SUBJECT_SELECTORS,
    TO_SELECTORS,
    _extract_pack,
    _find_visible,
    _open_compose,
    _require_test_email,
    _wait_body_images_loaded,
)
from app.browser.auth_state import restore_auth_cookies
from app.browser.outlook_probe import (
    get_outlook_host,
    get_profile_path,
    is_outlook_mail_url,
    wait_for_user,
)

# Verified against live Outlook web (Nov 2026).
MORE_SEND_OPTIONS = 'button[aria-label="More send options"]'
DISCARD_BUTTON = 'button[aria-label="Discard"]'
DRAFT_SAVED_RE = re.compile(r"draft saved", re.I)
SUGGESTION_SETTLE_S = 1.5  # Enter before suggestions load does not commit


class ScheduleUnavailable(RuntimeError):
    """The account has no Schedule send option -> keep as a plain draft."""


class ScheduleUnconfirmed(RuntimeError):
    """Schedule was confirmed in the dialog but Outlook didn't close compose.

    Never fall back to a plain draft here: it may already be scheduled.
    """


@dataclass(frozen=True)
class DraftItem:
    kind: str  # "scheduled" | "manual"
    label: str
    subject: str
    html: str
    original_to: str
    original_cc: str
    send_at: datetime | None = None  # local time, scheduled only


# ---------------------------------------------------------------- inputs ---


def resolve_test_email(cli_value: str | None) -> str:
    """--test-email wins, else MOVEOUT_TEST_EMAIL; refuse to run without one."""
    value = cli_value or os.environ.get("MOVEOUT_TEST_EMAIL")
    return _require_test_email(value)


def parse_send_at(send_at_iso: str) -> datetime | None:
    """UTC ``...Z`` timestamp -> naive local datetime (what Outlook's picker uses)."""
    if not send_at_iso:
        return None
    parsed = datetime.fromisoformat(send_at_iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().replace(tzinfo=None)


def parse_eml(content: str) -> dict[str, str]:
    message = Parser(policy=policy.default).parsestr(content)
    part = message.get_body(preferencelist=("html", "plain"))
    body = part.get_content() if part is not None else ""
    if part is not None and part.get_content_type() == "text/plain":
        body = "<pre>" + body + "</pre>"
    return {
        "to": str(message.get("To") or ""),
        "cc": str(message.get("Cc") or ""),
        "subject": str(message.get("Subject") or ""),
        "html": body,
    }


def scheduled_items(pack: dict[str, Any]) -> list[DraftItem]:
    items = []
    for email in pack.get("emailsToSchedule") or []:
        if email.get("immediate") is True:
            continue  # belt and braces: immediate mail is branch 2's job
        items.append(
            DraftItem(
                kind="scheduled",
                label=str(email.get("label") or email.get("subject") or ""),
                subject=str(email.get("subject") or ""),
                html=str(email.get("html") or email.get("body") or ""),
                original_to=str(email.get("to") or ""),
                original_cc=str(email.get("cc") or ""),
                send_at=parse_send_at(str(email.get("sendAtISO") or "")),
            )
        )
    return items


def manual_items(pack: dict[str, Any]) -> list[DraftItem]:
    items = []
    for file in pack.get("emailFiles") or []:
        parsed = parse_eml(str(file.get("content") or ""))
        items.append(
            DraftItem(
                kind="manual",
                label=str(file.get("label") or parsed["subject"]),
                subject=parsed["subject"],
                html=parsed["html"],
                original_to=parsed["to"],
                original_cc=parsed["cc"],
            )
        )
    return items


# ------------------------------------------------------------- browser ops ---


async def _fill_draft(page: Any, to: str, subject: str, html: str) -> None:
    to_field = await _find_visible(page, TO_SELECTORS, 5_000, "To field")
    await to_field.click(timeout=5_000)
    await page.keyboard.type(to, delay=30)
    await asyncio.sleep(SUGGESTION_SETTLE_S)
    await page.keyboard.press("Enter")
    pill = page.locator(f'span[aria-label="{to}"]').filter(visible=True)
    for _ in range(20):
        if await pill.count() > 0:
            break
        await asyncio.sleep(0.25)
    else:
        raise RuntimeError(f"Recipient {to} was not committed in the To field.")

    subject_field = await _find_visible(
        page, SUBJECT_SELECTORS, 5_000, "Subject field"
    )
    await subject_field.fill(subject, timeout=5_000)

    body = await _find_visible(page, BODY_SELECTORS, 5_000, "message body")
    await body.focus()
    await page.evaluate(
        """async (html) => {
            const blob = new Blob([html], {type: 'text/html'});
            await navigator.clipboard.write([new ClipboardItem({'text/html': blob})]);
        }""",
        html,
    )
    await page.keyboard.press(("Meta" if sys.platform == "darwin" else "Control") + "+V")
    await _wait_body_images_loaded(body)


async def _wait_draft_saved(page: Any, timeout_s: float = 20.0) -> bool:
    indicator = page.get_by_text(DRAFT_SAVED_RE).filter(visible=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await indicator.count() > 0:
            return True
        await asyncio.sleep(0.5)
    return False


async def _save_manual(page: Any) -> bool:
    """Outlook autosaves; nudge with Ctrl+S and wait for 'Draft saved'."""
    if await _wait_draft_saved(page, 8.0):
        return True
    await page.keyboard.press("Control+S")
    return await _wait_draft_saved(page, 15.0)


def _format_like(sample: str, day: date) -> str:
    """Format ``day`` in whatever order the picker's prefilled value uses."""
    sep = next((c for c in sample if not c.isdigit()), "-")
    parts = sample.split(sep)
    if len(parts) == 3 and len(parts[0]) == 4:
        order = ("%Y", "%m", "%d")
    elif len(parts) == 3 and len(parts[1]) == 2 and int(parts[1] or 0) > 12:
        order = ("%m", "%d", "%Y")  # middle part can't be a month -> US order
    else:
        order = ("%d", "%m", "%Y")
    return day.strftime(sep.join(order))


async def _schedule_send(page: Any, when: datetime) -> None:
    """Open Schedule send > Custom time, set date/time, verify, confirm."""
    more = page.locator(MORE_SEND_OPTIONS).filter(visible=True).first
    await more.click(timeout=10_000)
    menu_item = page.get_by_role("menuitem", name="Schedule send")
    try:
        await menu_item.first.wait_for(state="visible", timeout=5_000)
    except Exception as error:
        raise ScheduleUnavailable("No 'Schedule send' menu item.") from error
    await menu_item.first.click(timeout=5_000)
    await page.get_by_text("Custom time").first.click(timeout=5_000)

    dialog = page.get_by_role("dialog").filter(
        has=page.get_by_role("combobox")
    ).last
    boxes = dialog.get_by_role("combobox")
    date_box, time_box = boxes.nth(0), boxes.nth(1)
    want_date = _format_like(await date_box.input_value(), when.date())
    want_time = when.strftime("%H:%M")

    for box, value in ((date_box, want_date), (time_box, want_time)):
        await box.fill(value)
        # Tab (not Enter/Escape) commits the value and closes the combobox's
        # option list; Escape could close the whole schedule dialog.
        await box.press("Tab")
        await asyncio.sleep(0.5)
    await _wait_no_open_listbox(page)

    got = (await date_box.input_value(), await time_box.input_value())
    if got != (want_date, want_time):
        await dialog.get_by_role("button", name="Cancel").click()
        raise RuntimeError(
            f"Schedule picker shows {got}, expected {(want_date, want_time)}."
        )
    await dialog.get_by_role("button", name="Send", exact=True).click(timeout=5_000)
    try:
        await page.locator(BODY_SELECTORS[0]).first.wait_for(
            state="hidden", timeout=30_000
        )
    except Exception as error:
        raise ScheduleUnconfirmed(
            "Schedule confirmed but compose did not close; check Drafts "
            "before retrying (not re-drafted, to avoid duplicates)."
        ) from error


async def _wait_no_open_listbox(page: Any, timeout_s: float = 5.0) -> None:
    """Combobox option lists overlay the dialog's Send button; wait them out."""
    # Only popup layers: the inbox message list is also role=listbox/option.
    options = page.locator(
        '[data-portal-node="true"] [role="listbox"], '
        '[data-portal-node="true"] [role="option"]'
    ).filter(visible=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while await options.count() > 0:
        if loop.time() >= deadline:
            raise RuntimeError("A dropdown list is still covering the Send button.")
        await asyncio.sleep(0.25)


FAIL_SHOT = Path("/tmp/drafter_schedule_fail.png")


async def _save_diagnostics(page: Any) -> None:
    try:
        await page.screenshot(path=str(FAIL_SHOT), full_page=True)
        html = await page.evaluate("() => document.body.outerHTML")
        FAIL_SHOT.with_suffix(".html").write_text(html, encoding="utf-8")
        print(f"[drafter] Diagnostics: {FAIL_SHOT} and {FAIL_SHOT.with_suffix('.html')}")
    except Exception as error:
        print(f"[drafter] Could not save diagnostics: {error}")


async def _discard_compose(page: Any) -> None:
    """Delete the autosaved draft so failed attempts don't pile up in Drafts."""
    for _ in range(3):  # close any schedule dialog layers first
        cancel = page.get_by_role("dialog").get_by_role("button", name="Cancel")
        if await cancel.count() == 0:
            break
        await cancel.last.click(timeout=3_000)
        await asyncio.sleep(0.5)
    await page.locator(DISCARD_BUTTON).filter(visible=True).first.click(timeout=5_000)
    await page.get_by_role("dialog").get_by_role("button", name="OK").first.click(
        timeout=5_000
    )
    await asyncio.sleep(1.0)


async def _create_one(page: Any, item: DraftItem, to: str, mail_url: str) -> str:
    """Create one draft; returns how it ended up ('scheduled ...' / 'manual')."""
    await page.goto(mail_url, wait_until="domcontentloaded")
    await _open_compose(page)
    await _fill_draft(page, to, item.subject, item.html)

    if item.kind == "scheduled":
        when = item.send_at
        if when is None or when <= datetime.now() + timedelta(minutes=5):
            print(f"[drafter] WARNING: no future send time for '{item.label}'; "
                  "keeping it as an unscheduled draft.")
        else:
            try:
                await _schedule_send(page, when)
                return f"scheduled for {when:%Y-%m-%d %H:%M}"
            except ScheduleUnavailable as error:
                print(f"[drafter] WARNING: {error} Keeping '{item.label}' as "
                      "a plain draft.")
                await page.keyboard.press("Escape")
            except ScheduleUnconfirmed:
                raise
            except Exception as error:
                await _save_diagnostics(page)
                try:
                    await _discard_compose(page)
                    note = "draft discarded"
                except Exception as discard_error:
                    note = f"could not discard draft ({discard_error}); check Drafts"
                raise RuntimeError(f"Schedule send failed: {error} [{note}]") from error

    if not await _save_manual(page):
        raise RuntimeError("Outlook never showed 'Draft saved'; check Drafts.")
    return "manual"


async def create_drafts(
    items: list[DraftItem], test_email: str
) -> dict[str, int]:
    to = _require_test_email(test_email)
    created = failed = 0
    if not items:
        print("[drafter] Nothing to draft.")
        return {"created": 0, "failed": 0}

    profile = get_profile_path()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = get_outlook_host()
    mail_url = f"https://{host}/mail/"
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, channel="chrome"
            )
        except Exception as error:
            raise RuntimeError(
                f"Could not open the Outlook Chrome profile ({error}). If it "
                "is locked, run: pkill -f browser_data/outlook"
            ) from error
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(mail_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            if not is_outlook_mail_url(page.url):
                await wait_for_user("Sign in to Outlook, then press Enter: ")
                if not is_outlook_mail_url(page.url):
                    raise RuntimeError("Outlook sign-in was not completed.")
            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=f"https://{host}"
            )

            for item in items:
                original = item.original_to + (
                    f" cc {item.original_cc}" if item.original_cc else ""
                )
                print(f"[drafter] {item.label}: original recipients "
                      f"[{original}] -> redirected to {to}")
                try:
                    outcome = await _create_one(page, item, to, mail_url)
                    created += 1
                    print(f"[drafter] Created draft: {item.label} -> {to} ({outcome})")
                except Exception as error:
                    failed += 1
                    print(f"[drafter] ERROR drafting '{item.label}': {error}")
        finally:
            await context.close()
    return {"created": created, "failed": failed}


async def create_scheduled_drafts(
    pack: dict[str, Any], test_email: str, limit: int | None = None
) -> dict[str, int]:
    return await create_drafts(scheduled_items(pack)[:limit], test_email)


async def create_manual_drafts(
    pack: dict[str, Any], test_email: str, limit: int | None = None
) -> dict[str, int]:
    return await create_drafts(manual_items(pack)[:limit], test_email)


# -------------------------------------------------------------------- CLI ---


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


async def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create Outlook drafts for non-immediate pack emails."
    )
    parser.add_argument("--pack-file", required=True)
    parser.add_argument("--test-email", help="or set MOVEOUT_TEST_EMAIL")
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--scheduled-only", action="store_true")
    only.add_argument("--manual-only", action="store_true")
    parser.add_argument("--limit", type=_positive, default=None,
                        help="Cap the number of drafts created (total)")
    args = parser.parse_args(argv)

    test_email = resolve_test_email(args.test_email)
    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    items: list[DraftItem] = []
    if not args.manual_only:
        items += scheduled_items(pack)
    if not args.scheduled_only:
        items += manual_items(pack)
    items = items[: args.limit]

    kinds = [item.kind for item in items]
    print(f"[drafter] Creating {len(items)} draft(s): "
          f"{kinds.count('scheduled')} scheduled, {kinds.count('manual')} manual")
    result = await create_drafts(items, test_email)
    print(f"\n[drafter] Done: {result['created']} created, {result['failed']} failed")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
