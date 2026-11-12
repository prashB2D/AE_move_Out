"""Branch 4: email staff about move-out notices that need manual review.

Reads ``sent_to_review`` from a pipeline output file, dedups per property
(30 days, like the n8n "Dedup Review by Property" node) and sends one review
email per item to STAFF_EMAIL through Outlook web.

Safety: recipients come only from STAFF_EMAIL below, never from email data.
The tenant/owner mailer keeps its own test-only lock; this module reuses its
compose/send helpers instead of loosening that lock.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from app.adapters.playwright_mailer import (
    BODY_SELECTORS,
    SUBJECT_SELECTORS,
    TO_SELECTORS,
    _find_visible,
    _open_compose,
    _send_current,
    _wait_body_images_loaded,
)
from app.browser.auth_state import restore_auth_cookies
from app.browser.outlook_probe import (
    get_outlook_host,
    get_profile_path,
    is_outlook_mail_url,
    wait_for_user,
)

STAFF_EMAIL = "kingprashu123@gmail.com; prashanthgowdars555@gmail.com"
REVIEW_WINDOW_DAYS = 30
SEEN_PATH = Path.home() / ".moveout" / "review_seen.json"
SUGGESTION_SETTLE_S = 1.5  # Enter before Outlook's suggestions load is ignored
_DAY_MS = 86_400_000


def staff_recipients(value: str = STAFF_EMAIL) -> list[str]:
    recipients = [a.strip() for a in re.split(r"[;,]", value) if a.strip()]
    if not recipients or any("@" not in a for a in recipients):
        raise RuntimeError(f"Invalid STAFF_EMAIL: {value!r}")
    return recipients


# ------------------------------------------------------------------ dedup ---


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def property_key(item: dict[str, Any]) -> str | None:
    """'rev:<society>|<flat>', or None when the property can't be identified."""
    society, flat = _norm(item.get("society")), _norm(item.get("flat"))
    if not society and not flat:
        return None
    return f"rev:{society}|{flat}"


def load_seen(path: Path, now_ms: int) -> dict[str, int]:
    try:
        seen = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    cutoff = now_ms - REVIEW_WINDOW_DAYS * _DAY_MS
    return {k: int(v) for k, v in seen.items() if isinstance(v, (int, float)) and v >= cutoff}


def save_seen(path: Path, seen: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=2), encoding="utf-8")
    tmp.replace(path)


def dedup_reason(item: dict[str, Any], seen: dict[str, int], now_ms: int) -> str | None:
    """Why this item should be skipped, or None to send it."""
    if str(item.get("manualConfirmed")).lower() == "true":
        return None  # staff corrections are never deduped
    key = property_key(item)
    if key is None or key not in seen:
        return None
    return f"reviewed {(now_ms - seen[key]) // _DAY_MS} days ago"


# ------------------------------------------------------------------ email ---


def _label(item: dict[str, Any]) -> str:
    return f"{item.get('society') or '?'} {item.get('flat') or '?'}"


def review_subject(item: dict[str, Any]) -> str:
    return f"[MO-SETUP] Move-out review needed: {_label(item)}"


def _pause_reason(item: dict[str, Any]) -> str:
    if not item.get("moveOutDate") or "date" in str(item.get("reviewReason") or "").lower():
        return "the tenant did not give a firm move-out date"
    return "it could not be auto-matched to a property in THM"


def _ai_line(item: dict[str, Any]) -> str:
    used = item.get("aiUsed")
    if used is None:
        used = str(item.get("parseMethod") or "").lower() not in ("", "regex", "local")
    if str(used).lower() not in ("true", "1"):
        return "AI not used"
    parts = ["flagged as move-out notice"]
    confidence = item.get("aiConfidence")
    if confidence not in (None, ""):
        try:
            parts.append(f"confidence {round(float(confidence) * 100)}%")
        except ValueError:
            pass
    if item.get("aiReason"):
        parts.append(str(item["aiReason"]))
    return " — ".join(parts)


def review_html(item: dict[str, Any]) -> str:
    e = lambda key: html.escape(str(item.get(key) or ""))  # noqa: E731
    fields = [
        ("Property Complex Name", "society"), ("Flat Number", "flat"),
        ("Vacate Date", "moveOutDate"), ("Tenant Name", "tenantName"),
        ("Tenant Email", "tenantEmail"), ("Tenant Phone", "tenantPhone"),
        ("Owner Name", "ownerName"), ("Owner Email", "ownerEmail"),
        ("Owner Phone", "ownerPhone"), ("Property Type (PM or NonPM)", "propertyType"),
    ]
    block = "".join(f"{label}: {e(key)}<br>" for label, key in fields)
    return (
        '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#222">'
        "<p>Hi team,</p>"
        "<p>A move-out notice came in but the automation <b>paused for manual "
        f"review</b> - {_pause_reason(item)}.</p>"
        f"<p><b>Why:</b> {e('reviewReason') or e('parseStatus') or 'not given'}</p>"
        f"<p><b>THM said:</b> {e('thm_status') or 'no status'}</p>"
        f"<p><b>AI classifier:</b> {html.escape(_ai_line(item))}</p>"
        "<p><b>Original email:</b></p>"
        f"<p>From: {e('tenantName')} &lt;{e('tenantEmail')}&gt;<br>"
        f"Subject: {e('rawSubject')}<br>Preview: {e('rawPreview')}</p>"
        "<p><b>To run it:</b> look up the property in THM manually, then either:</p>"
        "<ul><li>Reply to this email with the correct Property Name and Flat Number</li>"
        "<li>Or run the pipeline manually after fixing the source email</li></ul>"
        '<div style="background:#f6f6f6;border:1px solid #ddd;padding:10px;'
        'font-family:Consolas,monospace;font-size:10.5pt;line-height:1.5">'
        "----- MOVE-OUT DETAILS (edit values, keep labels) -----<br>"
        f"{block}----- END MOVE-OUT DETAILS -----</div>"
        '<p style="color:#888;font-size:9pt">Most common fix: <b>Property Complex '
        'Name</b> must be the full name as THM stores it (e.g. "Purva Skywood", not '
        '"PSW"), and <b>Flat Number</b> should match THM\'s unit format.</p></div>'
    )


# ---------------------------------------------------------------- sending ---


async def _fill_and_send(page: Any, recipients: list[str], subject: str, body_html: str) -> None:
    await _open_compose(page)
    to_field = await _find_visible(page, TO_SELECTORS, 5_000, "To field")
    await to_field.click(timeout=5_000)
    for address in recipients:
        await page.keyboard.type(address, delay=30)
        await asyncio.sleep(SUGGESTION_SETTLE_S)
        await page.keyboard.press("Enter")
        pill = page.locator(f'span[aria-label="{address}"]').filter(visible=True)
        for _ in range(20):
            if await pill.count() > 0:
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError(f"Recipient {address} was not committed.")

    subject_field = await _find_visible(page, SUBJECT_SELECTORS, 5_000, "Subject field")
    await subject_field.fill(subject, timeout=5_000)
    body = await _find_visible(page, BODY_SELECTORS, 5_000, "message body")
    await body.focus()
    await page.evaluate(
        """async (h) => navigator.clipboard.write(
            [new ClipboardItem({'text/html': new Blob([h], {type: 'text/html'})})])""",
        body_html,
    )
    await page.keyboard.press(("Meta" if sys.platform == "darwin" else "Control") + "+V")
    await _wait_body_images_loaded(body)
    await _send_current(page)


async def _save_failure(page: Any, index: int) -> None:
    shot = Path(f"/tmp/review_fail_{index}.png")
    try:
        await page.screenshot(path=str(shot))
        print(f"[review] Diagnostics: {shot}")
    except Exception as error:
        print(f"[review] Could not save diagnostics: {error}")


async def send_reviews(
    items: list[dict[str, Any]], seen_path: Path = SEEN_PATH
) -> dict[str, int]:
    recipients = staff_recipients()
    now_ms = int(time.time() * 1000)
    seen = load_seen(seen_path, now_ms)
    to_send = []
    deduped = 0
    for item in items:
        reason = dedup_reason(item, seen, now_ms)
        if reason:
            deduped += 1
            print(f"[review] Deduped: {_label(item)} ({reason})")
        else:
            to_send.append(item)
    if not to_send:
        return {"sent": 0, "deduped": deduped, "failed": 0}

    sent = failed = 0
    profile = get_profile_path()
    host = get_outlook_host()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, channel="chrome"
        )
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            mail_url = f"https://{host}/mail/"
            await page.goto(mail_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
            if not is_outlook_mail_url(page.url):
                await wait_for_user("Sign in to Outlook, then press Enter: ")
                if not is_outlook_mail_url(page.url):
                    raise RuntimeError("Outlook sign-in was not completed.")
            await context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=f"https://{host}"
            )
            for index, item in enumerate(to_send, 1):
                key = property_key(item)
                # A second item for a property sent earlier in this run.
                if key and key in seen and dedup_reason(item, seen, now_ms):
                    deduped += 1
                    print(f"[review] Deduped: {_label(item)} (sent earlier this run)")
                    continue
                subject = review_subject(item)
                print(f"[review] Processing: {_label(item)} (reason: {_pause_reason(item)})")
                print(f"[review] Sending review to: {', '.join(recipients)}")
                try:
                    await page.goto(mail_url, wait_until="domcontentloaded")
                    await _fill_and_send(page, recipients, subject, review_html(item))
                except Exception as error:
                    failed += 1
                    print(f"[review] ERROR sending '{subject}': {error}")
                    await _save_failure(page, index)
                    continue
                sent += 1
                print(f"[review] Sent: {subject}")
                if key:
                    seen[key] = int(time.time() * 1000)
                    save_seen(seen_path, seen)
        finally:
            await context.close()
    return {"sent": sent, "deduped": deduped, "failed": failed}


# -------------------------------------------------------------------- CLI ---


def read_review_items(path: Path) -> list[dict[str, Any]]:
    content = path.read_text("utf-8")
    data = json.loads(content[content.find("{"):])
    items = data.get("sent_to_review") or []
    return [item for item in items if isinstance(item, dict)]


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


async def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Email staff about review items.")
    parser.add_argument("--pipeline-output", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=_positive, default=None)
    args = parser.parse_args(argv)

    print(f"[review] Reading {args.pipeline_output}")
    items = read_review_items(Path(args.pipeline_output))[: args.limit]
    print(f"[review] Found {len(items)} email(s) in sent_to_review")

    if args.dry_run:
        now_ms = int(time.time() * 1000)
        seen = load_seen(SEEN_PATH, now_ms)
        for item in items:
            reason = dedup_reason(item, seen, now_ms)
            status = f"would dedup ({reason})" if reason else "would send"
            print(f"[review] {status}: {review_subject(item)} -> "
                  f"{', '.join(staff_recipients())} | {_pause_reason(item)}")
        return

    result = await send_reviews(items)
    print(f"[review] Done: {result['sent']} sent, {result['deduped']} deduped, "
          f"{result['failed']} failed")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
