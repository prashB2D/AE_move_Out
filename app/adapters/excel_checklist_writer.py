"""Write a pack's checklist into the Move-Out Checklists workbook (openpyxl).

Mirrors the n8n Excel nodes: pick the first free ``MO-xx`` slot tab, rename it
to the property tab, write header A2:I3, status C5:C43, email links H5:H43
(formulas), the ticket remark H<checklistTicketRow>, automation status
K5:K43, and append a row to the ``MoveOutDashboard`` table.

Dry-run (default) opens nothing. ``--live`` needs ``--workbook-file`` (a
local copy of the workbook) and typing WRITE EXCEL; a timestamped backup is
written next to it first. Downloading/uploading the SharePoint workbook is
NOT implemented yet (no Graph, and its web UI hasn't been inspected).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.adapters.playwright_mailer import _extract_pack

DASHBOARD_TABLE = "MoveOutDashboard"
CONFIRM_TEXT = "WRITE EXCEL"
FIRST_ROW, LAST_ROW = 5, 43  # checklist body rows (39)
_BAD_TAB_CHARS = re.compile(r"[\[\]:*?/\\]")


@dataclass(frozen=True)
class RangeWrite:
    sheet_cell: str  # top-left cell, e.g. "C5"
    rows: list[list[Any]]
    label: str


def _column(values: Any, name: str) -> list[list[Any]]:
    if not isinstance(values, list) or len(values) != LAST_ROW - FIRST_ROW + 1:
        raise ValueError(f"pack.{name} must have {LAST_ROW - FIRST_ROW + 1} rows.")
    return [[value] for value in values]


def plan_writes(pack: dict[str, Any], ticket_remark: str) -> tuple[str, list[RangeWrite]]:
    tab = str(pack.get("checklistTab") or "").strip()
    if not tab or len(tab) > 31 or _BAD_TAB_CHARS.search(tab):
        raise ValueError(f"Invalid Excel tab name: {tab!r}")
    header = pack.get("checklistHeaderValues")
    if not (isinstance(header, list) and len(header) == 2
            and all(isinstance(r, list) and len(r) == 9 for r in header)):
        raise ValueError("pack.checklistHeaderValues must be 2 rows x 9 cells (A2:I3).")
    ticket_row = int(pack.get("checklistTicketRow") or 0)
    writes = [
        RangeWrite("A2", header, "Header A2:I3"),
        RangeWrite("C5", _column(pack.get("checklistStatusColC"), "checklistStatusColC"),
                   "Status C5:C43"),
        RangeWrite("H5", _column(pack.get("checklistEmailColH"), "checklistEmailColH"),
                   "Email links H5:H43"),
    ]
    if ticket_row and ticket_remark:
        writes.append(RangeWrite(f"H{ticket_row}", [[ticket_remark]],
                                 f"Ticket remark H{ticket_row}"))
    writes.append(RangeWrite("K5", _column(pack.get("checklistAutoStatusColK"),
                                           "checklistAutoStatusColK"), "Auto status K5:K43"))
    return tab, writes


def dry_run_remark(pack: dict[str, Any]) -> str:
    tickets = [t for t in pack.get("tickets") or [] if isinstance(t, dict)]
    if not tickets:
        return ""
    return (f"DRY RUN - {len(tickets)} tickets prepared: "
            + ", ".join(str(t.get("ticketCategory")) for t in tickets))[:900]


def free_slots(sheet_names: list[str]) -> list[str]:
    slots = [n for n in sheet_names if re.fullmatch(r"(?i)mo-\s*\d+", n.strip())]
    return sorted(slots, key=lambda n: int(re.sub(r"\D", "", n)))


def apply_to_workbook(path: Path, pack: dict[str, Any], ticket_remark: str,
                      log: Callable[[str], None] = print) -> str:
    """Write into ``path`` in place. Returns 'written' or 'skipped'."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string, range_boundaries

    tab, writes = plan_writes(pack, ticket_remark)
    workbook = load_workbook(path)
    if tab in workbook.sheetnames:
        log(f"[excel] Tab already exists for {tab}, skipping")
        return "skipped"
    slots = free_slots(workbook.sheetnames)
    if not slots:
        raise RuntimeError("No free MO-xx slot tab; duplicate the TEMPLATE tab "
                           "(e.g. MO-06, MO-07) and re-run.")
    sheet = workbook[slots[0]]
    log(f"[excel] Using slot {slots[0]} -> {tab}")
    sheet.title = tab

    for write in writes:
        col_letter, row = coordinate_from_string(write.sheet_cell)
        col = column_index_from_string(col_letter)
        for r, values in enumerate(write.rows):
            for c, value in enumerate(values):
                sheet.cell(row=row + r, column=col + c, value=value)

    table_sheet = next((ws for ws in workbook.worksheets if DASHBOARD_TABLE in ws.tables), None)
    if table_sheet is None:
        log(f"[excel] WARNING: table {DASHBOARD_TABLE} not found; dashboard row not added.")
    else:
        table = table_sheet.tables[DASHBOARD_TABLE]
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        new_row = max_row + 1
        table_sheet.cell(row=new_row, column=min_col, value=tab)
        table.ref = (f"{get_column_letter(min_col)}{min_row}:"
                     f"{get_column_letter(max_col)}{new_row}")
        if table.autoFilter is not None:
            table.autoFilter.ref = table.ref
        log(f"[excel] Dashboard row added at {table_sheet.title}!{new_row}")

    workbook.save(path)
    return "written"


def main(argv: list[str] | None = None, confirm: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description="Write the move-out checklist to Excel.")
    parser.add_argument("--pack-file", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default: print only")
    mode.add_argument("--live", action="store_true", help="Write (asks to confirm)")
    parser.add_argument("--workbook-file", help="Local .xlsx copy to write (live mode)")
    parser.add_argument("--ticket-remark", default=None,
                        help="Remark for the tickets row (from thm_ticket_creator)")
    args = parser.parse_args(argv)

    pack = _extract_pack(json.loads(Path(args.pack_file).read_text("utf-8")))
    remark = args.ticket_remark if args.ticket_remark is not None else dry_run_remark(pack)
    tab, writes = plan_writes(pack, remark)

    if not args.live:
        print(f"[excel] Dry-run for {tab} (workbook: {pack.get('checklistWorkbookPath')})")
        print("[excel]   Slot: first free MO-xx tab, renamed to the property tab")
        for write in writes:
            filled = sum(1 for row in write.rows for v in row if v not in (None, ""))
            formulas = sum(1 for row in write.rows for v in row
                           if isinstance(v, str) and v.startswith("="))
            extra = f", {formulas} formulas" if formulas else ""
            print(f"[excel]   {write.label}: {filled} non-empty cells{extra}")
        print(f"[excel]   Dashboard row: append [{tab}] to table {DASHBOARD_TABLE}")
        print("[excel] Dry-run complete. No writes made.")
        return 0

    if not args.workbook_file:
        print("[excel] --live currently needs --workbook-file <local copy of "
              f"{pack.get('checklistWorkbookPath')}>. SharePoint download/upload is "
              "not implemented yet.")
        return 2
    path = Path(args.workbook_file)
    if confirm(f'Type "{CONFIRM_TEXT}" to write {tab} into {path}: ').strip() != CONFIRM_TEXT:
        print("[excel] Not confirmed. Nothing written.")
        return 1
    backup = path.with_name(f"{path.stem}.backup-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    shutil.copy2(path, backup)
    print(f"[excel] Backup: {backup}")
    outcome = apply_to_workbook(path, pack, remark)
    print(f"[excel] Done: {outcome} ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
