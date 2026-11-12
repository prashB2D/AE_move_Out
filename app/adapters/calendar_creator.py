"""Create Outlook Calendar events for ``pack["reminders"]`` via Playwright.

Events are production-identical: the title is the pack subject verbatim, on
whatever account the shared Chrome profile is signed into (test isolation
comes from the account, not from the code).

The form is opened through Outlook's compose deeplink, which pre-fills
title/start/end (no fragile date pickers). Before Save, the form's title and
date/time summary are checked; after Save, the event is read back from the
day view. A reminder whose event already exists on that day is skipped, so
re-runs don't create duplicates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from app.adapters.playwright_mailer import _extract_pack
from app.browser.auth_state import restore_auth_cookies
from app.browser.outlook_probe import get_outlook_host, get_profile_path

# Verified against live Outlook web (Nov 2026).
TITLE_INPUT = 'input[placeholder="Add title"]'
DUMP_PATH = Path("/tmp/calendar_dump.html")
FAIL_DIR = Path("/tmp")
READBACK_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class Reminder:
    subject: str
    start: datetime  # local, naive (as the engine emits)
    end: datetime


def load_reminders(pack: dict[str, Any]) -> list[Reminder]:
    reminders = []
    for raw in pack.get("reminders") or []:
        subject = str(raw.get("subject") or "").strip()
        if not subject:
            raise ValueError("Reminder without a subject in pack.")
        start = datetime.fromisoformat(str(raw["startISO"]))
        end = datetime.fromisoformat(str(raw["endISO"]))
        if end <= start:
            raise ValueError(f"Reminder ends before it starts: {subject}")
        reminders.append(Reminder(subject, start, end))
    return reminders


def compose_url(host: str, reminder: Reminder) -> str:
    query = urlencode(
        {
            "subject": reminder.subject,
            "startdt": reminder.start.strftime("%Y-%m-%dT%H:%M:%S"),
            "enddt": reminder.end.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    return f"https://{host}/calendar/0/deeplink/compose?{query}"


def event_label_matches(label: str, reminder: Reminder) -> bool:
    """Calendar items are labelled e.g. '<title>, 10:00 to 10:30, Sunday,
    November 15, 2026, Busy'."""
    start = reminder.start
    return (
        label.startswith(reminder.subject + ",")
        and f"{start:%H:%M} to {reminder.end:%H:%M}" in label
        and f"{start:%B} {start.day}, {start.year}" in label
    )


def summary_matches(summary: str, reminder: Reminder) -> bool:
    """Form shows e.g. 'Sun 15-11-2026 10:00 - 10:30'."""
    times = f"{reminder.start:%H:%M} - {reminder.end:%H:%M}"
    dates = {reminder.start.strftime(f) for f in ("%d-%m-%Y", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y")}
    return times in summary and any(d in summary for d in dates)


def _line(reminder: Reminder) -> str:
    return (
        f"{reminder.subject} — {reminder.start:%Y-%m-%d %H:%M} -> "
        f"{reminder.end:%H:%M}"
    )


# ------------------------------------------------------------- browser ops ---


_EVENT_LABELS_JS = """(subject) => [...document.querySelectorAll('[aria-label]')]
    .map(e => e.getAttribute('aria-label'))
    .filter(label => label && label.includes(subject))"""


async def _goto_day(page: Any, host: str, day: date) -> None:
    """Show ``day`` in Day view. Outlook ignores dates in the URL, so click the
    mini month calendar, or step with the next/previous-day buttons."""
    await page.goto(f"https://{host}/calendar/view/day", wait_until="domcontentloaded")
    next_day = page.locator('button[aria-label^="Go to next day"]').filter(visible=True)
    await next_day.first.wait_for(timeout=20_000)
    await asyncio.sleep(1.5)
    cell = page.locator(
        f'button[aria-label^="{day.day}, {day:%B}, {day.year}"]'
    ).filter(visible=True)
    if await cell.count() > 0:
        await cell.first.click()
    else:
        delta = (day - date.today()).days
        button = next_day if delta > 0 else page.locator(
            'button[aria-label^="Go to previous day"]').filter(visible=True)
        for _ in range(abs(delta)):
            await button.first.click()
            await asyncio.sleep(0.3)
    await asyncio.sleep(2.5)  # let the day's events render


async def _events_on_day(page: Any, host: str, reminder: Reminder) -> list[str]:
    """Labels of calendar items that match the reminder (title, time, date)."""
    await _goto_day(page, host, reminder.start.date())
    labels = await page.evaluate(_EVENT_LABELS_JS, reminder.subject)
    return [label for label in labels if event_label_matches(label, reminder)]


async def _wait_readback(page: Any, host: str, reminder: Reminder) -> list[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READBACK_TIMEOUT_S
    while True:
        labels = await _events_on_day(page, host, reminder)
        if labels or loop.time() >= deadline:
            return labels
        await asyncio.sleep(2.0)


async def _open_form(page: Any, host: str, reminder: Reminder) -> Any:
    await page.goto(compose_url(host, reminder), wait_until="domcontentloaded")
    title = page.locator(TITLE_INPUT).filter(visible=True).first
    await title.wait_for(timeout=30_000)
    await asyncio.sleep(1.5)
    return title


async def _form_summary(page: Any, reminder: Reminder) -> str:
    """Text of the collapsed date/time row in the form."""
    times = f"{reminder.start:%H:%M} - {reminder.end:%H:%M}"
    row = page.get_by_role("button").filter(has_text=times).filter(visible=True)
    if await row.count() == 0:
        return ""
    return (await row.first.inner_text()).strip()


async def _create_one(page: Any, host: str, reminder: Reminder) -> str:
    if await _events_on_day(page, host, reminder):
        return "already exists, skipped"

    title = await _open_form(page, host, reminder)
    if await title.input_value() != reminder.subject:
        await title.fill(reminder.subject)
    if await title.input_value() != reminder.subject:
        raise RuntimeError("Title field does not match the pack subject.")
    summary = await _form_summary(page, reminder)
    if not summary_matches(summary, reminder):
        raise RuntimeError(
            f"Form date/time {summary!r} does not match {_line(reminder)}; not saved."
        )

    await page.get_by_role("button", name="Save", exact=True).filter(
        visible=True
    ).first.click(timeout=10_000)
    try:
        await title.wait_for(state="hidden", timeout=15_000)
    except Exception:
        pass  # the read-back below is the real check

    labels = await _wait_readback(page, host, reminder)
    if not labels:
        print(f"[calendar] WARNING: saved, but no event with this title and "
              f"time found on {reminder.start:%d-%b-%Y}; check the calendar.")
        return "created (unverified)"
    return "created" if len(labels) == 1 else f"created ({len(labels)} with this title that day)"


async def _save_failure(page: Any, index: int) -> None:
    shot = FAIL_DIR / f"calendar_fail_{index}.png"
    try:
        await page.screenshot(path=str(shot))
        shot.with_suffix(".html").write_text(
            await page.evaluate("() => document.body.outerHTML"), encoding="utf-8"
        )
        print(f"[calendar] Diagnostics: {shot} (+ .html)")
    except Exception as error:
        print(f"[calendar] Could not save diagnostics: {error}")


async def _with_page(callback: Any) -> Any:
    profile = get_profile_path()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = get_outlook_host()
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, channel="chrome"
            )
        except Exception as error:
            raise RuntimeError(
                f"Could not open the Outlook Chrome profile ({error}). If it is "
                "locked, run: pkill -f browser_data/outlook"
            ) from error
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://{host}/calendar/", wait_until="domcontentloaded")
            await asyncio.sleep(3.0)
            if "/calendar" not in page.url:
                raise RuntimeError(
                    f"Not signed in to Outlook Calendar (landed on {page.url}). "
                    "Run the Outlook reader once to sign in."
                )
            return await callback(page, host)
        finally:
            await context.close()


async def create_events(reminders: list[Reminder]) -> dict[str, int]:
    async def run(page: Any, host: str) -> dict[str, int]:
        created = skipped = failed = 0
        for index, reminder in enumerate(reminders, 1):
            print(f"[calendar] Creating: {_line(reminder)}")
            try:
                outcome = await _create_one(page, host, reminder)
            except Exception as error:
                failed += 1
                print(f"[calendar] ERROR: {reminder.subject}: {error}")
                await _save_failure(page, index)
                if failed > len(reminders) / 2:
                    raise RuntimeError(
                        f"{failed} of {len(reminders)} events failed; aborting "
                        "(selectors, sign-in or page problem)."
                    ) from error
                continue
            if outcome.startswith("already exists"):
                skipped += 1
            else:
                created += 1
            print(f"[calendar] {outcome.capitalize()}: {reminder.subject}")
        return {"created": created, "skipped": skipped, "failed": failed}

    return await _with_page(run)


async def dump_form(reminder: Reminder) -> None:
    async def run(page: Any, host: str) -> None:
        await _open_form(page, host, reminder)
        await page.screenshot(path=str(DUMP_PATH.with_suffix(".png")))
        DUMP_PATH.write_text(
            await page.evaluate("() => document.body.outerHTML"), encoding="utf-8"
        )
        print(f"[calendar] Form dumped to {DUMP_PATH} and "
              f"{DUMP_PATH.with_suffix('.png')}; nothing was saved.")

    await _with_page(run)


# -------------------------------------------------------------------- CLI ---


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


async def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create Outlook Calendar events for pack reminders."
    )
    parser.add_argument("--pack-file", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be created; no browser")
    parser.add_argument("--limit", type=_positive, default=None)
    parser.add_argument("--dump-calendar-html", action="store_true",
                        help="Open the pre-filled form for the first reminder, "
                             "dump HTML + screenshot, exit without saving")
    args = parser.parse_args(argv)

    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    reminders = load_reminders(pack)[: args.limit]
    print(f"[calendar] Creating {len(reminders)} event(s) from pack")

    if args.dry_run:
        for reminder in reminders:
            print(f"[calendar] Would create: {_line(reminder)}")
        return
    if args.dump_calendar_html:
        if reminders:
            await dump_form(reminders[0])
        return

    result = await create_events(reminders)
    print(f"\n[calendar] Done: {result['created']} created, "
          f"{result['skipped']} already existed, {result['failed']} failed")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
