import asyncio
import json

import pytest

from app.adapters import excel_writer_playwright as w
from tests.test_excel_checklist_writer import PACK


def _pack_file(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(PACK))
    return str(path)


@pytest.mark.parametrize("url, text, expected", [
    ("https://login.microsoftonline.com/common/oauth2?x=1", "", "redirected to sign-in"),
    ("https://login.live.com/login.srf", "", "redirected to sign-in"),
    ("https://x.sharepoint.com/Doc.aspx", "Access Denied", "access denied"),
    ("https://x.sharepoint.com/Doc.aspx", "You don't have permission", "permission"),
    ("https://x.sharepoint.com/Doc.aspx", "You need access. Request access", "access"),
    ("https://x.sharepoint.com/Doc.aspx", "Move-Out Checklists  TEMPLATE  MO-01", None),
])
def test_access_problem(url, text, expected) -> None:
    result = w.access_problem(url, text)
    assert (result is None) if expected is None else (expected.lower() in result.lower())


def test_workbook_url_required(monkeypatch) -> None:
    monkeypatch.delenv("MOVEOUT_EXCEL_WORKBOOK_URL", raising=False)
    with pytest.raises(RuntimeError, match="MOVEOUT_EXCEL_WORKBOOK_URL"):
        w.workbook_url()
    monkeypatch.setenv("MOVEOUT_EXCEL_WORKBOOK_URL", "https://x.sharepoint.com/:x:/r/a")
    assert w.workbook_url() == "https://x.sharepoint.com/:x:/r/a"


def test_dry_run_default_opens_no_browser(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(w, "dump_excel_ui", lambda: pytest.fail("browser opened"))
    assert asyncio.run(w._main(["--pack-file", _pack_file(tmp_path)])) == 0
    out = capsys.readouterr().out
    assert "Slot: first MO-xx tab -> rename to Krishna Brookfield-T-17" in out
    assert "Status C5:C43: 39 cells" in out and "No browser opened." in out


def test_live_refuses_until_selectors_verified(tmp_path, capsys) -> None:
    assert asyncio.run(w._main(["--pack-file", _pack_file(tmp_path), "--live"])) == 2
    assert "not implemented yet" in capsys.readouterr().out


def test_dump_reports_access_denied(tmp_path, monkeypatch, capsys) -> None:
    async def denied():
        return {"url": "https://login.microsoftonline.com/x", "tabs": [],
                "problem": "redirected to sign-in (https://login.microsoftonline.com/x)",
                "frames": []}

    monkeypatch.setattr(w, "dump_excel_ui", denied)
    with pytest.raises(w.ExcelAccessError, match="Editor access"):
        asyncio.run(w._main(["--pack-file", _pack_file(tmp_path), "--dump-excel-ui"]))
    assert "Workbook accessible: no - redirected to sign-in" in capsys.readouterr().out
