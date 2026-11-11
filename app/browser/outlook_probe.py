"""Read-only, interactive feasibility probe for Outlook Web."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.browser.auth_state import save_auth_state

OUTLOOK_HOSTS = {
    "outlook.office.com",
    "outlook.office365.com",
    "outlook.live.com",
    "outlook.cloud.microsoft",
}
DEFAULT_OUTLOOK_HOST = "outlook.live.com"


def get_outlook_host() -> str:
    """Return the configured Outlook host, defaulting to personal Outlook."""
    host = (
        os.environ.get("MOVEOUT_OUTLOOK_HOST", DEFAULT_OUTLOOK_HOST)
        .strip()
        .lower()
    )
    if host not in OUTLOOK_HOSTS:
        raise ValueError(
            "MOVEOUT_OUTLOOK_HOST must be one of: "
            + ", ".join(sorted(OUTLOOK_HOSTS))
        )
    return host


OUTLOOK_HOST = get_outlook_host()
OUTLOOK_URL = f"https://{OUTLOOK_HOST}/mail/"
MESSAGE_ROW_SELECTOR = '[role="option"]'
MESSAGE_BODY_SELECTORS = (
    '[role="document"]',
    '[aria-label="Message body"]',
    '[data-testid="message-body"]',
)

# ---- Automatic sign-in configuration ----
DEFAULT_EMAIL = "prashursking@outlook.com"

# Microsoft sign-in page: "Email, phone, or Skype" input
EMAIL_INPUT_SELECTORS = (
    'input[name="loginfmt"]',          # primary MS selector
    'input#i0116',                     # legacy MS selector
    'input[type="email"]',
    'input[placeholder*="Email" i]',
    'input[placeholder*="phone" i]',
    'input[aria-label*="Email" i]',
)

# Microsoft sign-in page: "Next" button
NEXT_BUTTON_SELECTORS = (
    'input#idSIButton9',               # primary MS "Next" button
    'button#idSIButton9',
    'input[type="submit"][value="Next"]',
    'button:has-text("Next")',
    'input[type="submit"]',
)

# Page markers that confirm we're on the MS email step
EMAIL_STEP_MARKERS = (
    'text="Sign in"',
    'text="Email, phone, or Skype"',
    'input[name="loginfmt"]',
)


def is_outlook_mail_url(url: str) -> bool:
    """Return whether a URL is an Outlook mailbox page on a known host."""
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in OUTLOOK_HOSTS
        and parsed.path.startswith("/mail")
    )


def get_profile_path() -> Path:
    configured_path = os.environ.get("MOVEOUT_BROWSER_PROFILE")
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "browser_data" / "outlook"


async def wait_for_user(prompt: str) -> None:
    await asyncio.to_thread(input, prompt)


async def _wait_for_any(page, selectors: tuple[str, ...], timeout_ms: int = 20_000):
    """Return the first selector whose element becomes visible, or None."""
    per_selector_timeout = max(1_000, timeout_ms // len(selectors))
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=per_selector_timeout)
            return locator
        except Exception:
            continue
    return None


async def _click_first_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click()
                return True
        except Exception:
            continue
    return False


async def auto_fill_email(page, email: str) -> None:
    """Fill the Microsoft 'Email, phone, or Skype' field and click Next."""
    print("Waiting for Microsoft sign-in page...")

    # Wait until the URL looks like a login page or the email field appears
    try:
        await page.wait_for_url(
            lambda url: any(
                marker in url
                for marker in ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")
            ),
            timeout=20_000,
        )
    except Exception:
        # Not fatal — could already be on the form or a different host
        pass

    email_locator = await _wait_for_any(page, EMAIL_INPUT_SELECTORS, timeout_ms=20_000)
    if email_locator is None:
        print("Could not find the email input field. Please enter it manually.")
        return

    print(f"Found email field. Typing: {email}")
    await email_locator.click()
    await email_locator.fill("")            # clear any prefilled value
    await email_locator.type(email, delay=30)  # human-like typing
    # Dismiss any autocomplete dropdown / suggestion bubble
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)

    # Ensure the Next button is enabled before clicking
    next_button = await _wait_for_any(page, NEXT_BUTTON_SELECTORS, timeout_ms=10_000)
    if next_button is None:
        print("Could not find the Next button. Please click it manually.")
        return

    try:
        if not await next_button.is_enabled():
            await asyncio.sleep(0.5)
    except Exception:
        pass

    print("Clicking Next...")
    try:
        await next_button.click(timeout=10_000)
    except Exception:
        # Fallback: press Enter inside the input (works on MS form)
        try:
            await email_locator.press("Enter")
        except Exception:
            print("Could not click Next. Please click it manually.")
            return

    # Give the password page a moment to load
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await asyncio.sleep(1.0)
    print("Email submitted. Complete password / MFA in the browser.")


async def run_probe() -> None:
    from playwright.async_api import async_playwright

    profile_path = get_profile_path()
    profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not profile_path.is_dir():
        raise RuntimeError(f"Browser profile path is not a directory: {profile_path}")

    print(f"Using persistent browser profile: {profile_path}")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(OUTLOOK_URL, wait_until="domcontentloaded")

            # If a persistent session already lands us in the mailbox, skip auto-fill.
            if is_outlook_mail_url(page.url):
                print("Already signed in — skipping email auto-fill.")
            else:
                await auto_fill_email(page, DEFAULT_EMAIL)

            print(
                "A browser window is open. Complete the password / MFA steps manually if prompted."
            )
            await wait_for_user("When your Outlook inbox is visible, press Enter here: ")

            if not is_outlook_mail_url(page.url):
                raise RuntimeError(
                    "Outlook mailbox was not detected after sign-in. "
                    "Confirm the browser is on an Outlook /mail page and try again."
                )

            rows = page.locator(MESSAGE_ROW_SELECTOR)
            row_count = await rows.count()
            if row_count == 0:
                raise RuntimeError(
                    "No Outlook message rows were found. The inbox may still be loading, "
                    "or the current Outlook layout may need a selector update."
                )

            auth_path = await save_auth_state(context, profile_path)
            print(f"Authenticated session cookies saved securely to {auth_path.name}.")

            first_row_label = await rows.first.get_attribute("aria-label")
            print(f"Mailbox page detected: yes")
            print(f"Message rows detected: {row_count}")
            print(f"First row has an accessible label: {'yes' if first_row_label else 'no'}")
            print("No message body was read or printed.")

            await wait_for_user(
                "Optionally open a representative email in the browser, then press "
                "Enter to test whether its body container is visible (or press Enter "
                "without opening one to skip): "
            )
            body_visible = False
            for selector in MESSAGE_BODY_SELECTORS:
                locator = page.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    body_visible = True
                    break
            print(f"Visible message body container detected: {'yes' if body_visible else 'no'}")
            print("The probe is read-only; it does not send, modify, or delete anything.")
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(run_probe())


if __name__ == "__main__":
    main()