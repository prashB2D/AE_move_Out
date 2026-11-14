import json

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table

from app.adapters import excel_checklist_writer as x

PACK = {
    "emailsToSend": [],
    "checklistWorkbookPath": "Move-Out Automation/Move-Out Checklists.xlsx",
    "checklistTab": "Krishna Brookfield-T-17",
    "checklistHeaderValues": [["Flat# :", "T-17", "", "AE Manager:", "", "", "", "Notice Date:", 46338],
                              ["Society:", "Krishna Brookfield", "", "", "", "", "", "", ""]],
    "checklistStatusColC": [f"status {i}" for i in range(39)],
    "checklistEmailColH": ['=HYPERLINK("https://x/a.eml","email")'] + [""] * 38,
    "checklistAutoStatusColK": ["Handled by automation workflow"] * 39,
    "checklistTicketRow": 17,
    "tickets": [{"ticketCategory": "Painting"}, {"ticketCategory": "Plumbing"}],
}


def _workbook(tmp_path, sheets=("TEMPLATE", "Dashboard", "MO-10", "MO-2", "MO-03")):
    wb = Workbook()
    wb.remove(wb.active)
    for name in sheets:
        ws = wb.create_sheet(name)
        if name == "Dashboard":
            ws.append(["Property", "B", "C", "D", "E", "F", "G", "H", "I"])
            ws.append(["Old Property-1"] + [None] * 8)
            ws.add_table(Table(displayName="MoveOutDashboard", ref="A1:I2"))
    path = tmp_path / "checklists.xlsx"
    wb.save(path)
    return path


def test_free_slots_sorted_numerically() -> None:
    assert x.free_slots(["TEMPLATE", "MO-10", "MO-2", "mo-03", "MO-x", "Krishna-1"]) == [
        "MO-2", "mo-03", "MO-10"]


def test_writes_lowest_slot_ranges_formulas_and_dashboard_row(tmp_path) -> None:
    path = _workbook(tmp_path)
    assert x.apply_to_workbook(path, PACK, "Created: Painting (t1)", log=lambda _m: None) == "written"
    wb = load_workbook(path)
    assert "MO-2" not in wb.sheetnames and "MO-03" in wb.sheetnames
    ws = wb["Krishna Brookfield-T-17"]
    assert ws["A2"].value == "Flat# :" and ws["I2"].value == 46338 and ws["B3"].value == "Krishna Brookfield"
    assert ws["C5"].value == "status 0" and ws["C43"].value == "status 38"
    assert ws["H5"].value == '=HYPERLINK("https://x/a.eml","email")'
    assert ws["H17"].value == "Created: Painting (t1)"  # ticket remark overrides row 17
    assert ws["K43"].value == "Handled by automation workflow"
    dash = wb["Dashboard"]
    assert dash["A3"].value == "Krishna Brookfield-T-17"
    assert dash.tables["MoveOutDashboard"].ref == "A1:I3"


def test_existing_tab_is_skipped_without_changes(tmp_path) -> None:
    path = _workbook(tmp_path, sheets=("Krishna Brookfield-T-17", "MO-01"))
    before = path.read_bytes()
    assert x.apply_to_workbook(path, PACK, "", log=lambda _m: None) == "skipped"
    assert path.read_bytes() == before


def test_no_free_slot_is_an_error(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="No free MO-xx slot"):
        x.apply_to_workbook(_workbook(tmp_path, sheets=("TEMPLATE",)), PACK, "", log=lambda _m: None)


@pytest.mark.parametrize("change, message", [
    ({"checklistTab": "Bad/Name"}, "Invalid Excel tab"),
    ({"checklistTab": "x" * 32}, "Invalid Excel tab"),
    ({"checklistStatusColC": ["a"] * 38}, "39 rows"),
    ({"checklistHeaderValues": [["a"] * 9]}, "2 rows x 9"),
])
def test_bad_pack_shapes_rejected(change, message) -> None:
    with pytest.raises(ValueError, match=message):
        x.plan_writes({**PACK, **change}, "")


def _pack_file(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(PACK))
    return str(path)


def test_dry_run_default_opens_no_workbook(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(x, "apply_to_workbook", lambda *a, **k: pytest.fail("wrote"))
    assert x.main(["--pack-file", _pack_file(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Email links H5:H43: 1 non-empty cells, 1 formulas" in out
    assert "Ticket remark H17" in out and "No writes made." in out


def test_live_needs_workbook_and_exact_confirmation(tmp_path, capsys) -> None:
    pack = _pack_file(tmp_path)
    assert x.main(["--pack-file", pack, "--live"]) == 2
    path = _workbook(tmp_path)
    before = path.read_bytes()
    assert x.main(["--pack-file", pack, "--live", "--workbook-file", str(path)],
                  confirm=lambda _p: "yes") == 1
    assert path.read_bytes() == before


def test_live_confirmed_backs_up_then_writes(tmp_path) -> None:
    path = _workbook(tmp_path)
    assert x.main(["--pack-file", _pack_file(tmp_path), "--live", "--workbook-file", str(path)],
                  confirm=lambda _p: "WRITE EXCEL") == 0
    assert len(list(tmp_path.glob("checklists.backup-*.xlsx"))) == 1
    assert "Krishna Brookfield-T-17" in load_workbook(path).sheetnames
