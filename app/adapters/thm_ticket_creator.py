"""Create the pack's THM tickets (port of the n8n "THM Create Tickets" node).

Dry-run is the default and makes no network call at all (no login). Live
mode needs ``--live`` plus typing CREATE TICKETS at a prompt.

Live flow, as in n8n:
  login -> project categories (+ sub-categories) -> the home's existing OPEN
  tickets -> per category, create only the shortfall (desired - open), so a
  re-run creates nothing and the two "Home Cleaning" tickets both get made.
Category / sub-category names are resolved to THM ObjectIds before POST.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx

from app.adapters.playwright_mailer import _extract_pack

THM_BASE_URL = os.environ.get("THM_BASE_URL", "https://dashboard.thehousemonk.com")
TIMEOUT_S = 20
SUBCATEGORY_FIELD = "ticketSubCategory"  # n8n's best guess; verify on first live ticket
CONFIRM_TEXT = "CREATE TICKETS"
LIST_LIMIT = 500
FILTER_IGNORED_AT = 1000  # no single home has this many tickets; org-wide is ~15k
_CLOSED = re.compile(r"clos|complet|cancel|resolv|done|reject", re.I)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload[k] for k in ("rows", "data", "tickets", "subCategories")
                     if isinstance(payload.get(k), list)), [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("_id") or value.get("id") or "")
    return str(value or "")


def describe(tickets: list[dict[str, Any]]) -> list[str]:
    return [
        f"{t.get('ticketCategory')}"
        + (f" ({t['subCategory']})" if t.get("subCategory") else "")
        + f" - jobDate {t.get('jobDate') or '?'}"
        for t in tickets
    ]


def ticket_remark(results: list[dict[str, Any]]) -> str:
    """One-line remark for the checklist 'Create tickets' row (n8n format)."""
    made = [r["ticketCategory"] + (f" ({r['id']})" if r.get("id") else "")
            for r in results if r["ok"] and not r.get("skipped")]
    skipped = [r["ticketCategory"] for r in results if r.get("skipped")]
    failed = [f"{r['ticketCategory']} [{r.get('error') or 'failed'}]"
              for r in results if not r["ok"]]
    parts = []
    if made:
        parts.append("Created: " + ", ".join(made))
    if skipped:
        parts.append("Already open (skipped): " + ", ".join(skipped))
    if failed:
        parts.append("Failed: " + "; ".join(failed))
    return " | ".join(parts)[:900]


class THMClient:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http
        self.token = ""

    def login(self, phone: str, password: str, country_code: str) -> None:
        response = self.http.post("/api/login", json={
            "countryCode": country_code, "phoneNumber": phone, "password": password})
        response.raise_for_status()
        token = (response.json() or {}).get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("THM login returned no token.")
        self.token = token

    def get(self, path: str) -> Any:
        response = self.http.get(path, headers={"Authorization": self.token})
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        response = self.http.post(path, json=body, headers={"Authorization": self.token})
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        return response.json()


def _categories(thm: THMClient, project: str) -> tuple[dict[str, str], dict[str, str]]:
    categories: dict[str, str] = {}
    subcategories: dict[str, str] = {}

    def add_subs(items: Any) -> None:
        for sub in items if isinstance(items, list) else []:
            if isinstance(sub, dict) and sub.get("name") and _id(sub):
                subcategories.setdefault(_norm(sub["name"]), _id(sub))

    for cat in _rows(thm.get(f"/api/category?project={project}")):
        if cat.get("name") and _id(cat) and cat.get("module") in (None, "tickets"):
            categories[_norm(cat["name"])] = _id(cat)
        for key in ("subCategories", "subcategories", "subCategory", "children"):
            add_subs(cat.get(key))
    if not subcategories:
        for path in (f"/api/sub-category?project={project}", f"/api/subcategory?project={project}"):
            try:
                rows = _rows(thm.get(path))
            except Exception:
                continue
            if rows:
                add_subs(rows)
                break
    return categories, subcategories


def _count(payload: Any) -> int | None:
    value = payload.get("count") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


def _open_counts(thm: THMClient, home: str, categories: dict[str, str]) -> Counter:
    """Open tickets per category id on the home.

    Verified live: THM ignores ``?home=`` (returns all ~15k org tickets) but
    honours ``ticketBelongsToRefId``; and listed tickets carry the category
    as a NAME ("Home cleaning"), not an id. Fails closed: if the filter
    looks ignored or the list is incomplete, raise instead of guessing.
    """
    payload = thm.get(f"/api/ticket?ticketBelongsToRefId={home}&limit={LIST_LIMIT}")
    rows = _rows(payload)
    count = _count(payload)
    if count is not None and count >= FILTER_IGNORED_AT:
        raise RuntimeError("THM ignored the per-home ticket filter; refusing to create "
                           "tickets without a reliable duplicate check.")
    if count is not None and count > len(rows):
        raise RuntimeError(f"Home has {count} tickets but only {len(rows)} were listed; "
                           "refusing to create tickets without a complete duplicate check.")
    ids = set(categories.values())
    counts: Counter = Counter()
    for ticket in rows:
        status = ticket.get("status") or ticket.get("ticketStatus") or ""
        if isinstance(status, dict):
            status = status.get("name", "")
        if _CLOSED.search(str(status)):
            continue
        raw = _id(ticket.get("ticketCategory")) or _id(ticket.get("category"))
        category = raw if raw in ids else categories.get(_norm(raw), "")
        if category:
            counts[category] += 1
    return counts


def create_tickets(
    tickets: list[dict[str, Any]], thm: THMClient, log: Callable[[str], None] = print
) -> list[dict[str, Any]]:
    """Live creation. Never raises for a single ticket; returns per-ticket results."""
    project = str(tickets[0].get("project") or "")
    home = str(tickets[0].get("home") or "")
    if not project or not home:
        raise RuntimeError("Tickets have no THM project/home id; run THM lookup first.")
    categories, subcategories = _categories(thm, project)
    log(f"[tickets] THM categories for project: {len(categories)}, "
        f"sub-categories: {len(subcategories)}")

    open_counts = _open_counts(thm, home, categories)
    log(f"[tickets] Open tickets already on home: {sum(open_counts.values())}")
    desired = Counter(categories[_norm(t.get("ticketCategory"))] for t in tickets
                      if _norm(t.get("ticketCategory")) in categories)
    needed = {cat: max(0, n - open_counts[cat]) for cat, n in desired.items()}

    results = []
    for ticket in tickets:
        name = str(ticket.get("ticketCategory") or "")
        category_id = categories.get(_norm(name))
        if not category_id:
            results.append({"ticketCategory": name, "ok": False,
                            "error": f'category not found in project: "{name}"'})
            continue
        if needed[category_id] <= 0:
            results.append({"ticketCategory": name, "ok": True, "skipped": True})
            continue
        payload = {**ticket, "ticketCategory": category_id}
        sub_name = payload.pop("subCategory", None)
        if sub_name:
            sub_id = subcategories.get(_norm(sub_name))
            if sub_id:
                payload[SUBCATEGORY_FIELD] = sub_id
            else:
                log(f'[tickets] WARNING: sub-category "{sub_name}" not found; sent without it.')
        try:
            response = thm.post("/api/ticket", payload)
        except Exception as error:
            results.append({"ticketCategory": name, "ok": False, "error": str(error)[:300]})
            continue
        created = response.get("ticket") if isinstance(response, dict) else None
        results.append({"ticketCategory": name, "ok": True, "id": _id(created) or None})
        needed[category_id] -= 1
    return results


def _client_from_env() -> THMClient:
    phone = os.environ.get("THM_PHONE", "").strip()
    password = os.environ.get("THM_PASSWORD", "").strip()
    if not phone or not password:
        raise RuntimeError("Set THM_PHONE and THM_PASSWORD in .env.")
    thm = THMClient(httpx.Client(base_url=THM_BASE_URL, timeout=TIMEOUT_S))
    thm.login(phone, password, os.environ.get("THM_COUNTRY_CODE", "+91").strip())
    return thm


def main(argv: list[str] | None = None, confirm: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description="Create THM tickets from a pack.")
    parser.add_argument("--pack-file", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default: print only")
    mode.add_argument("--live", action="store_true", help="POST to THM (asks to confirm)")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    tickets = [t for t in pack.get("tickets") or [] if isinstance(t, dict)][: args.limit]
    home = tickets[0].get("home") if tickets else "?"

    if not args.live:
        print(f"[tickets] Dry-run: {len(tickets)} tickets would be created for home {home}")
        for line in describe(tickets):
            print(f"[tickets]   {line}")
        print("[tickets] Dry-run complete. No tickets created.")
        return 0

    if not tickets:
        print("[tickets] No tickets in pack.")
        return 0
    if confirm(f'Type "{CONFIRM_TEXT}" to create {len(tickets)} THM ticket(s): ').strip() != CONFIRM_TEXT:
        print("[tickets] Not confirmed. Nothing created.")
        return 1
    from dotenv import load_dotenv
    load_dotenv()
    results = create_tickets(tickets, _client_from_env())
    for result in results:
        state = ("skipped (already open)" if result.get("skipped")
                 else f"created {result.get('id') or ''}" if result["ok"]
                 else f"FAILED: {result.get('error')}")
        print(f"[tickets] {result['ticketCategory']}: {state}")
    print(f"[tickets] Remark: {ticket_remark(results)}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
