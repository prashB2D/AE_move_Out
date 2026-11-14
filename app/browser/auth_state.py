"""Securely persist and restore Outlook browser cookies."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext

AUTH_STATE_FILENAME = "auth.json"


def get_auth_state_path(profile_path: Path) -> Path:
    return profile_path / AUTH_STATE_FILENAME


async def save_auth_state(context: BrowserContext, profile_path: Path) -> Path:
    """Save Playwright cookies atomically with owner-only file permissions."""
    profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_path = get_auth_state_path(profile_path)
    state = await context.storage_state()
    payload = json.dumps({"cookies": state.get("cookies", [])}, ensure_ascii=False)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=profile_path,
            prefix=".auth-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            os.chmod(temp_path, 0o600)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, auth_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    os.chmod(auth_path, 0o600)
    return auth_path


def load_auth_cookies(profile_path: Path) -> list[dict[str, Any]]:
    """Load saved cookies, raising an explicit error for malformed state."""
    auth_path = get_auth_state_path(profile_path)
    if not auth_path.exists():
        return []

    with auth_path.open(encoding="utf-8") as auth_file:
        state = json.load(auth_file)
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list) or not all(isinstance(cookie, dict) for cookie in cookies):
        raise ValueError(f"Invalid Outlook cookie state format: {auth_path}")
    return cookies


_SESSION_URLS = ("https://login.live.com", "https://outlook.live.com")


async def restore_auth_cookies(context: BrowserContext, profile_path: Path) -> bool:
    """Inject saved cookies only when the profile has no Microsoft session.

    The persistent Chrome profile keeps (and refreshes) its own cookies.
    Re-injecting an older auth.json on every launch overwrote those fresher
    cookies and made outlook.live.com answer HTTP 500, so auth.json is only a
    fallback for an empty profile.
    """
    if await context.cookies(list(_SESSION_URLS)):
        return False
    cookies = load_auth_cookies(profile_path)
    if not cookies:
        return False
    await context.add_cookies(cookies)
    return True
