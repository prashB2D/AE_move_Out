"""Write the checklist tab through Excel for the web, using the shared
``browser_data/outlook`` Chrome profile (one account for every channel).

Status:
* ``--dry-run`` (default): prints the write plan; no browser.
* ``--dump-excel-ui``: the login/access test - opens the workbook, reports
  URL / access / sheet tabs, saves /tmp/excel_dump.html + .png, changes nothing.
* ``--live``: not implemented until the dump shows Excel web's real DOM
  (tab strip, Name Box, rename, table row). No selectors are guessed.

The workbook URL comes from MOVEOUT_EXCEL_WORKBOOK_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from app.adapters.excel_checklist_writer import (
    DASHBOARD_TABLE,
    dry_run_remark,
    free_slots,
    plan_writes,
)
from app.adapters.playwright_mailer import _extract_pack

DUMP_PATH = Path("/tmp/excel_dump.html")
SETTLE_TIMEOUT_S = 60.0
_LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")
_DENIED = re.compile(
    r"access denied|you don.t have (permission|access)|request access|"
    r"need permission|you need access|sign in to (your|continue)", re.I)


class ExcelAccessError(RuntimeError):
    pass


def workbook_url() -> str:
    url = os.environ.get("MOVEOUT_EXCEL_WORKBOOK_URL", "").strip()
    if not url.startswith("https://"):
        raise RuntimeError(
            "Set MOVEOUT_EXCEL_WORKBOOK_URL in .env to the full SharePoint link of "
            "Move-Out Checklists.xlsx.")
    return url


def access_problem(url: str, page_text: str) -> str | None:
    """Why the workbook isn't usable, or None if it looks reachable."""
    if any(host in url for host in _LOGIN_HOSTS):
        return f"redirected to sign-in ({url.split('?')[0]})"
    match = _DENIED.search(page_text or "")
    if match:
        return f'page says "{match.group(0)}"'
    return None


_TAB_NAMES_JS = """() => [...document.querySelectorAll('[role="tab"]')]
    .map(t => (t.getAttribute('aria-label') || t.innerText || '').trim())
    .filter(Boolean)"""


async def _sheet_tabs(page: Any) -> list[str]:
    """Sheet tab names from the page and any frame (Excel web runs in an iframe)."""
    names: list[str] = []
    for frame in page.frames:
        try:
            names += await frame.evaluate(_TAB_NAMES_JS)
        except Exception:
            continue
    return list(dict.fromkeys(names))


async def dump_excel_ui() -> dict[str, Any]:
    from playwright.async_api import async_playwright

    from app.browser.auth_state import restore_auth_cookies
    from app.browser.outlook_probe import get_profile_path

    url = workbook_url()
    profile = get_profile_path()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, channel="chrome")
        try:
            await restore_auth_cookies(context, profile)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + SETTLE_TIMEOUT_S
            tabs: list[str] = []
            text = ""
            while loop.time() < deadline:
                await asyncio.sleep(3.0)
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                if access_problem(page.url, text):
                    break
                tabs = await _sheet_tabs(page)
                if any(free_slots([t]) or t.upper() == "TEMPLATE" for t in tabs):
                    break

            await page.screenshot(path=str(DUMP_PATH.with_suffix(".png")))
            parts = [f"<!-- main: {page.url} -->", await page.content()]
            for frame in page.frames[1:]:
                try:
                    parts.append(f"<!-- frame: {frame.url} -->\n" + await frame.content())
                except Exception:
                    continue
            DUMP_PATH.write_text("\n".join(parts), encoding="utf-8")
            return {"url": page.url, "tabs": tabs, "problem": access_problem(page.url, text),
                    "frames": [f.url[:100] for f in page.frames]}
        finally:
            await context.close()


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the checklist via Excel web.")
    parser.add_argument("--pack-file", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default: print plan only")
    mode.add_argument("--dump-excel-ui", action="store_true",
                      help="Open the workbook, dump HTML + screenshot, change nothing")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Accepted for symmetry")
    args = parser.parse_args(argv)

    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    tab, writes = plan_writes(pack, dry_run_remark(pack))

    if args.dump_excel_ui:
        from dotenv import load_dotenv
        load_dotenv()
        result = await dump_excel_ui()
        print(f"[excel-web] Current URL: {result['url']}")
        print(f"[excel-web] Tab strip detected: {'yes' if result['tabs'] else 'no'}")
        print(f"[excel-web] Tabs found: {result['tabs']}")
        print(f"[excel-web] Free MO-xx slots: {free_slots(result['tabs'])}")
        print(f"[excel-web] Workbook accessible: "
              f"{'no - ' + result['problem'] if result['problem'] else 'yes' if result['tabs'] else 'unclear (no tabs found)'}")
        print(f"[excel-web] Dumped {DUMP_PATH} and {DUMP_PATH.with_suffix('.png')}")
        if result["problem"]:
            raise ExcelAccessError(
                "Workbook requires sign-in or access. Confirm the Playwright account "
                f"has Editor access to the workbook. ({result['problem']})")
        return 0

    if args.live:
        print("[excel-web] --live is not implemented yet: run --dump-excel-ui with "
              "workbook access first so the Excel web selectors can be verified.")
        return 2

    print(f"[excel-web] Dry-run for {tab}")
    print("[excel-web]   Open MOVEOUT_EXCEL_WORKBOOK_URL in the shared profile")
    print("[excel-web]   Slot: first MO-xx tab -> rename to " + tab)
    for write in writes:
        print(f"[excel-web]   {write.label}: {sum(len(r) for r in write.rows)} cells via Name Box + paste")
    print(f"[excel-web]   Dashboard: add row [{tab}] to table {DASHBOARD_TABLE}")
    print("[excel-web] Dry-run complete. No browser opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
