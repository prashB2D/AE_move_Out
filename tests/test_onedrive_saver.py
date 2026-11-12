import asyncio
import json

import pytest

from app.adapters import onedrive_saver as o


def _pack(folder_path: str, name: str = "04 Tenant 2nd email (rough estimate).eml"):
    return {"emailsToSend": [], "emailFiles": [
        {"folderPath": folder_path, "fileName": name, "content": "To: x\n\n<p/>",
         "label": "Tenant 2nd email"}]}


def test_double_slash_from_empty_email_folder_is_collapsed() -> None:
    files = o.load_files(_pack("Move-Out Automation//Krishna Brookfield T-17/Tenant"))
    assert files[0].folders == ("Move-Out Automation", "Krishna Brookfield T-17", "Tenant")


def test_full_path_kept_in_order() -> None:
    path = "Move-Out Automation/Owner Tenant Email Templates/Krishna Brookfield T-17/Owner"
    assert o.load_files(_pack(path))[0].folder_path == path


@pytest.mark.parametrize("path", ["A/../B", "./A"])
def test_unsafe_paths_rejected(path) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        o.load_files(_pack(path))


@pytest.mark.parametrize("name", ["", "a/b.eml"])
def test_bad_file_names_rejected(name) -> None:
    with pytest.raises(ValueError, match="fileName"):
        o.load_files(_pack("A/B", name))


def test_dry_run_lists_files_without_browser(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(o, "async_playwright", None)  # any browser use would crash
    pack_file = tmp_path / "pack.json"
    pack_file.write_text(json.dumps(_pack("A//B/Tenant")))
    asyncio.run(o._main(["--pack-file", str(pack_file), "--dry-run"]))
    out = capsys.readouterr().out
    assert "Would save: 04 Tenant 2nd email (rough estimate).eml -> A/B/Tenant/" in out


def _fake_run(monkeypatch, outcomes):
    async def fake_with_page(callback):
        return await callback(object())

    async def fake_save_one(page, file, workdir):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def no_diag(page, index) -> None: ...

    monkeypatch.setattr(o, "_with_page", fake_with_page)
    monkeypatch.setattr(o, "_save_one", fake_save_one)
    monkeypatch.setattr(o, "_save_failure", no_diag)


def _files(n):
    return o.load_files(_pack("A/B")) * n


def test_one_failure_continues_and_existing_files_are_skipped(monkeypatch) -> None:
    _fake_run(monkeypatch, ["uploaded", RuntimeError("x"), "already exists, skipped", "uploaded"])
    assert asyncio.run(o.save_files(_files(4))) == {"saved": 2, "skipped": 1, "failed": 1}


def test_majority_failure_aborts(monkeypatch) -> None:
    _fake_run(monkeypatch, [RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), "uploaded"])
    with pytest.raises(RuntimeError, match="3 of 4 files failed"):
        asyncio.run(o.save_files(_files(4)))
