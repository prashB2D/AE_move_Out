"""Run the whole move-out flow end to end, one step after another.

    python -m app.run_flow                     # full flow from the live inbox
    python -m app.run_flow --skip send         # everything except sending email
    python -m app.run_flow --from-output /tmp/pipeline_output.json   # reuse step 1

Steps (each is the same CLI you can run on its own):
  1 pipeline  read inbox -> parse -> THM -> gates -> engine   (app.main)
  2 review    staff review email for items needing a human     (review_notifier)
  3 send      immediate emails (owner notice + tenant ack)     (playwright_mailer)
  4 drafts    scheduled + manual Outlook drafts                (outlook_drafter)
  5 calendar  reminder events                                  (calendar_creator)
  6 onedrive  save .eml files                                  (onedrive_saver)
  7 tickets   THM tickets  - DRY RUN ONLY                      (thm_ticket_creator)
  8 excel     checklist    - DRY RUN ONLY                      (excel_checklist_writer)

Tickets and Excel are always dry-run here; nothing is created in THM or
written to the workbook. A failed step is reported and the flow continues.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PY = sys.executable
OUT = Path("/tmp")
STEPS = ("pipeline", "review", "send", "drafts", "calendar", "onedrive", "tickets", "excel")
BROWSER_STEPS = {"pipeline", "review", "send", "drafts", "calendar", "onedrive"}


def _banner(number: int, name: str, text: str) -> None:
    print(f"\n{'=' * 70}\n STEP {number}/8  {name.upper()}  -  {text}\n{'=' * 70}", flush=True)


def _free_browser() -> None:
    """Each step opens the shared Chrome profile; make sure none is left open."""
    subprocess.run(["pkill", "-f", "browser_data/outlook"], capture_output=True)
    time.sleep(1.0)


def _run(args: list[str], *, browser: bool) -> int:
    if browser:
        _free_browser()
    print(f"$ {' '.join(args[1:])}", flush=True)
    return subprocess.call(args)


def extract_summary(text: str) -> dict[str, Any]:
    """The pipeline prints log lines, then the JSON summary starting at a bare '{'."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == "{"]
    if not starts:
        raise ValueError("No JSON summary found in pipeline output.")
    return json.loads("\n".join(lines[starts[-1]:]))


def run_pipeline(limit: int) -> tuple[int, str]:
    _free_browser()
    args = [PY, "-u", "-m", "app.main", "--input-live", "--limit", str(limit), "--fresh"]
    print(f"$ {' '.join(args[2:])}", flush=True)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, bufsize=1)
    captured = []
    assert process.stdout is not None
    in_json = False
    for line in process.stdout:
        captured.append(line)
        in_json = in_json or line.rstrip("\n") == "{"
        if not in_json:  # show the logs live, keep the big JSON off screen
            print(line, end="", flush=True)
    return process.wait(), "".join(captured)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the full move-out flow.")
    parser.add_argument("--limit", type=int, default=20, help="Inbox emails to read")
    parser.add_argument("--from-output", help="Reuse a saved pipeline output (skip step 1)")
    parser.add_argument("--skip", default="", help=f"Comma list of steps to skip: {','.join(STEPS)}")
    parser.add_argument("--test-email", default=None,
                        help="Redirect address for sends/drafts (default: MOVEOUT_TEST_EMAIL or the mailer's test inbox)")
    args = parser.parse_args(argv)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - set(STEPS)
    if unknown:
        parser.error(f"unknown step(s): {', '.join(sorted(unknown))}")
    from app.adapters.playwright_mailer import TEST_EMAIL
    test_email = args.test_email or os.environ.get("MOVEOUT_TEST_EMAIL") or TEST_EMAIL
    if "@" not in test_email:
        test_email = TEST_EMAIL
    results: dict[str, str] = {}

    # ---- 1. pipeline -------------------------------------------------------
    output_file = OUT / "pipeline_output.json"
    _banner(1, "pipeline", "read inbox, parse, THM lookup, gates, engine")
    if args.from_output:
        summary = extract_summary(Path(args.from_output).read_text("utf-8"))
        results["pipeline"] = f"reused {args.from_output}"
    elif "pipeline" in skip:
        print("skipped")
        summary = extract_summary(output_file.read_text("utf-8"))
        results["pipeline"] = f"skipped (reused {output_file})"
    else:
        code, text = run_pipeline(args.limit)
        output_file.write_text(text, encoding="utf-8")
        if code != 0:
            print(f"\n[flow] Pipeline failed (exit {code}); see output above.")
            return code
        summary = extract_summary(text)
        results["pipeline"] = "ok"
        output_file.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    buckets = {k: len(summary.get(k) or []) for k in
               ("auto_processed", "sent_to_review", "setup_replies",
                "dropped_duplicate_or_stale", "ignored_noise")}
    print(f"\n[flow] Buckets: {buckets}")
    packs = [item["pack"] for item in summary.get("auto_processed") or [] if item.get("pack")]

    # ---- 2. review ---------------------------------------------------------
    _banner(2, "review", "email staff about notices that need a human")
    if "review" in skip:
        results["review"] = "skipped"
    elif not buckets["sent_to_review"]:
        print("[flow] Nothing in sent_to_review.")
        results["review"] = "nothing to review"
    else:
        code = _run([PY, "-m", "app.adapters.review_notifier",
                     "--pipeline-output", str(output_file)], browser=True)
        results["review"] = "ok" if code == 0 else f"FAILED (exit {code})"

    if not packs:
        print("\n[flow] No property was auto-processed, so there is nothing to send, "
              "draft or save. (A notice already handled, a duplicate, or one that "
              "went to review ends here.)")
        for step in STEPS[2:]:
            results.setdefault(step, "no pack")
        _report(results)
        return 0

    # ---- 3..8 per property pack -----------------------------------------------
    for index, pack in enumerate(packs, 1):
        pack_file = OUT / ("pack.json" if len(packs) == 1 else f"pack_{index}.json")
        pack_file.write_text(json.dumps(pack, indent=2, default=str), encoding="utf-8")
        label = pack.get("propertyRef") or pack.get("checklistTab") or f"pack {index}"
        print(f"\n[flow] Property {index}/{len(packs)}: {label}  ->  {pack_file}")
        suffix = "" if len(packs) == 1 else f" [{label}]"

        plan = [
            (3, "send", "send the immediate emails",
             [PY, "-m", "app.adapters.playwright_mailer", "--pack-file", str(pack_file),
              "--test-email", test_email], True),
            (4, "drafts", "create scheduled + manual Outlook drafts",
             [PY, "-m", "app.adapters.outlook_drafter", "--pack-file", str(pack_file),
              "--test-email", test_email], True),
            (5, "calendar", "create reminder events",
             [PY, "-m", "app.adapters.calendar_creator", "--pack-file", str(pack_file)], True),
            (6, "onedrive", "save the .eml files to OneDrive",
             [PY, "-m", "app.adapters.onedrive_saver", "--pack-file", str(pack_file)], True),
            (7, "tickets", "THM tickets (DRY RUN - nothing created)",
             [PY, "-m", "app.adapters.thm_ticket_creator", "--pack-file", str(pack_file),
              "--dry-run"], False),
            (8, "excel", "checklist workbook (DRY RUN - nothing written)",
             [PY, "-m", "app.adapters.excel_checklist_writer", "--pack-file", str(pack_file),
              "--dry-run"], False),
        ]
        for number, name, text, command, browser in plan:
            _banner(number, name, text + suffix)
            if name in skip:
                print("skipped")
                results[name + suffix] = "skipped"
                continue
            code = _run(command, browser=browser)
            results[name + suffix] = "ok" if code == 0 else f"FAILED (exit {code})"

    _free_browser()
    _report(results)
    return 0 if all(not v.startswith("FAILED") for v in results.values()) else 1


def _report(results: dict[str, str]) -> None:
    print(f"\n{'=' * 70}\n FLOW SUMMARY\n{'=' * 70}")
    for step, outcome in results.items():
        print(f"  {step:<28} {outcome}")
    print("  (tickets and excel are dry-run: nothing created in THM or the workbook)")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    raise SystemExit(main())
