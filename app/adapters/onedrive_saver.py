"""Save ``pack["emailFiles"]`` (.eml) into OneDrive via Playwright (no Graph).

Per file: start at "My files", walk the folder path by clicking each folder
(creating missing ones via Create or upload > Folder), then upload through
Create or upload > Files upload. Existing files are skipped (never
overwritten) and every upload is verified in the folder listing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from app.adapters.playwright_mailer import _extract_pack
from app.browser.auth_state import restore_auth_cookies
from app.browser.outlook_probe import get_profile_path, wait_for_user

# Personal OneDrive of the account signed into the shared profile (test
# isolation by account, like the calendar). Override for the company drive.
DRIVE_URL = os.environ.get("MOVEOUT_ONEDRIVE_URL", "https://onedrive.live.com/")
LOGIN_URL = "https://onedrive.live.com/login/"
DUMP_PATH = Path("/tmp/onedrive_dump.html")
MY_FILES_URL = DRIVE_URL.rstrip("/") + "/my"

# Verified against live OneDrive web (Nov 2026).
ADD_NEW = '[data-automationid="AddNew"]'
CRUMB = '[data-automationid="breadcrumb-crumb"]'
CREATE_FOLDER = '[data-automationid="CreateFolderCommand"]'
UPLOAD_FILES = '[data-automationid="uploadFile"]'
UPLOAD_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class DriveFile:
    folders: tuple[str, ...]  # path segments from the drive root
    file_name: str
    content: str
    label: str

    @property
    def folder_path(self) -> str:
        return "/".join(self.folders)


def _segments(path: str) -> tuple[str, ...]:
    """'A//B/C/' -> ('A', 'B', 'C'): tolerate an empty emailFolder setting."""
    parts = tuple(part.strip() for part in path.replace("\\", "/").split("/"))
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"Unsafe folder path: {path!r}")
    return tuple(part for part in parts if part)


def load_files(pack: dict[str, Any]) -> list[DriveFile]:
    files = []
    for raw in pack.get("emailFiles") or []:
        name = str(raw.get("fileName") or "").strip()
        if not name or "/" in name or "\\" in name:
            raise ValueError(f"Invalid fileName: {name!r}")
        folders = _segments(str(raw.get("folderPath") or raw.get("propertyPath") or ""))
        if not folders:
            raise ValueError(f"No target folder for {name!r}")
        files.append(
            DriveFile(folders, name, str(raw.get("content") or ""),
                      str(raw.get("label") or name))
        )
    return files


def _item(page: Any, name: str) -> Any:
    """A file or folder entry in the current listing (title = exact name)."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return page.locator(f'span[role="button"][title="{escaped}"]').filter(visible=True)


async def _current_folder(page: Any) -> str:
    crumbs = page.locator(CRUMB).filter(visible=True)
    if await crumbs.count() == 0:
        return ""
    return (await crumbs.last.inner_text()).strip()


async def _wait_until(check: Any, timeout_s: float, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not await check():
        if loop.time() >= deadline:
            raise RuntimeError(f"Timed out waiting for {what}.")
        await asyncio.sleep(0.5)


async def _wait_listing(page: Any, folder: str) -> None:
    async def ready() -> bool:
        return await _current_folder(page) == folder
    await _wait_until(ready, 20.0, f"folder '{folder}' to open")
    await asyncio.sleep(1.5)  # let the listing render


async def _create_folder(page: Any, name: str) -> None:
    await page.locator(ADD_NEW).first.click(timeout=10_000)
    await page.locator(CREATE_FOLDER).first.click(timeout=5_000)
    dialog = page.get_by_role("dialog")
    await dialog.get_by_role("textbox", name="Name").fill(name)
    await dialog.get_by_role("button", name="Create", exact=True).click(timeout=5_000)

    async def appeared() -> bool:
        return await _item(page, name).count() > 0
    await _wait_until(appeared, 20.0, f"new folder '{name}' to appear")


async def _open_path(page: Any, folders: tuple[str, ...]) -> None:
    """From My files, open (creating if needed) each folder in turn."""
    await page.goto(MY_FILES_URL, wait_until="domcontentloaded")
    await _wait_listing(page, "My files")
    for name in folders:
        if await _item(page, name).count() == 0:
            print(f"[onedrive] Creating folder: {name}")
            await _create_folder(page, name)
        await _item(page, name).first.click(timeout=10_000)
        await _wait_listing(page, name)


async def _upload(page: Any, file: DriveFile, workdir: Path) -> None:
    local = workdir / file.file_name
    local.write_text(file.content, encoding="utf-8")
    await page.locator(ADD_NEW).first.click(timeout=10_000)
    async with page.expect_file_chooser(timeout=10_000) as chooser_info:
        await page.locator(UPLOAD_FILES).first.click(timeout=5_000)
    chooser = await chooser_info.value
    await chooser.set_files(str(local))

    async def listed() -> bool:
        return await _item(page, file.file_name).count() > 0
    await _wait_until(listed, UPLOAD_TIMEOUT_S, f"'{file.file_name}' to appear")


async def _save_one(page: Any, file: DriveFile, workdir: Path) -> str:
    await _open_path(page, file.folders)
    if await _item(page, file.file_name).count() > 0:
        return "already exists, skipped"
    print(f"[onedrive] Uploading: {file.file_name} -> {file.folder_path}/")
    await _upload(page, file, workdir)
    return "uploaded"


async def _save_failure(page: Any, index: int) -> None:
    shot = Path(f"/tmp/onedrive_fail_{index}.png")
    try:
        await page.screenshot(path=str(shot))
        shot.with_suffix(".html").write_text(
            await page.evaluate("() => document.body.outerHTML"), encoding="utf-8")
        print(f"[onedrive] Diagnostics: {shot} (+ .html)")
    except Exception as error:
        print(f"[onedrive] Could not save diagnostics: {error}")


async def _with_page(callback: Any) -> Any:
    profile = get_profile_path()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, channel="chrome")
        except Exception as error:
            raise RuntimeError(
                f"Could not open the Chrome profile ({error}). If it is locked, "
                "run: pkill -f browser_data/outlook") from error
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(MY_FILES_URL, wait_until="domcontentloaded")
            # OneDrive bounces through login.live.com to refresh the session.
            for _ in range(60):
                if "onedrive.live.com/my" in page.url:
                    break
                await asyncio.sleep(0.5)
            if "onedrive.live.com/my" not in page.url:
                raise RuntimeError(
                    f"Not signed in to OneDrive (landed on {page.url}). Run: "
                    "python -m app.adapters.onedrive_saver --login")
            return await callback(page)
        finally:
            await context.close()


async def save_files(files: list[DriveFile]) -> dict[str, int]:
    async def run(page: Any) -> dict[str, int]:
        saved = skipped = failed = 0
        with tempfile.TemporaryDirectory(prefix="onedrive_upload_") as tmp:
            for index, file in enumerate(files, 1):
                try:
                    outcome = await _save_one(page, file, Path(tmp))
                except Exception as error:
                    failed += 1
                    print(f"[onedrive] ERROR: {file.file_name}: {error}")
                    await _save_failure(page, index)
                    if failed > len(files) / 2:
                        raise RuntimeError(
                            f"{failed} of {len(files)} files failed; aborting.") from error
                    continue
                if outcome == "uploaded":
                    saved += 1
                    print(f"[onedrive] Uploaded: {file.file_name}")
                else:
                    skipped += 1
                    print(f"[onedrive] Already exists, skipped: {file.file_name}")
        return {"saved": saved, "skipped": skipped, "failed": failed}

    return await _with_page(run)


async def dump_drive() -> None:
    async def run(page: Any) -> None:
        await page.screenshot(path=str(DUMP_PATH.with_suffix(".png")))
        DUMP_PATH.write_text(await page.evaluate("() => document.body.outerHTML"),
                             encoding="utf-8")
        print(f"[onedrive] Dumped {DUMP_PATH} and {DUMP_PATH.with_suffix('.png')}")
    await _with_page(run)


async def login() -> None:
    """Open OneDrive sign-in in the shared profile and wait for the user."""
    profile = get_profile_path()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, channel="chrome"
        )
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await wait_for_user(
                "Sign in to OneDrive in the browser (tick 'Stay signed in'), "
                "wait until your files are listed, then press Enter: "
            )
            print(f"[onedrive] Browser is on: {page.url}")
        finally:
            await context.close()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


async def _main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Save pack .eml files to OneDrive.")
    parser.add_argument("--pack-file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=_positive, default=None)
    parser.add_argument("--dump-drive-html", action="store_true",
                        help="Dump My files HTML + screenshot; upload nothing")
    parser.add_argument("--login", action="store_true",
                        help="One-time OneDrive sign-in in the shared profile")
    args = parser.parse_args(argv)

    if args.login:
        await login()
        return
    if args.dump_drive_html:
        await dump_drive()
        return
    if not args.pack_file:
        parser.error("--pack-file is required")

    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    files = load_files(pack)[: args.limit]
    print(f"[onedrive] Saving {len(files)} file(s) to OneDrive ({DRIVE_URL})")
    if args.dry_run:
        for file in files:
            print(f"[onedrive] Would save: {file.file_name} -> {file.folder_path}/ "
                  f"({len(file.content):,} chars)")
        return

    result = await save_files(files)
    print(f"\n[onedrive] Done: {result['saved']} saved, {result['skipped']} "
          f"already existed, {result['failed']} failed")
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
