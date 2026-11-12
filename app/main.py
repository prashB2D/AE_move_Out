"""Run the local intake pipeline over a JSON file of Outlook-shaped messages."""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from app.adapters.action_sender import (
    execute_pack,
    is_channel_disabled,
    is_dry_run_enabled,
)
from app.logic.dedup import is_duplicate_or_stale
from app.logic.dedup_store import (
    DEFAULT_MESSAGE_DB,
    DEFAULT_PROPERTY_DB,
    SQLiteDedupStore,
    get_dedup_store,
    property_key_for,
)
from app.logic.engine import generate_move_out_pack
from app.logic.idempotency import check_property_dedup
from app.logic.parser import parse_move_out_email
from app.logic.router import thm_lookup as lookup_thm_home
from app.logic.sent_items_gate import (
    is_already_handled,
    read_sent_items_for_gate,
)
from app.logic.setup_reply import is_setup_reply, parse_setup_reply

DedupCheck = Callable[[dict[str, Any]], bool]
PropertyDedupCheck = Callable[[str, str], bool]
PropertyMark = Callable[[str, str], None]
THMLookup = Callable[[str | None, str | None, str | None], dict[str, Any]]
MessageRecorder = Callable[[dict[str, Any], str, dict[str, Any] | None], None]
PackRecorder = Callable[[dict[str, Any], dict[str, Any]], None]
SentItemsLoader = Callable[[], list[dict[str, Any]]]


def _record_message_outcome(
    email: dict[str, Any],
    bucket: str,
    canonical: dict[str, Any] | None = None,
) -> None:
    message_id = email.get("id")
    if not isinstance(message_id, str) or not message_id.strip():
        return
    data = canonical or {}
    society = data.get("society")
    flat = data.get("flat")
    key = (
        property_key_for(society, flat)
        if isinstance(society, str) and isinstance(flat, str)
        else None
    )
    store = get_dedup_store()
    store.record_message(
        message_id,
        bucket=bucket,
        conversation_id=email.get("conversationId"),
        society=society if isinstance(society, str) else None,
        flat=flat if isinstance(flat, str) else None,
        property_key=key,
        thm_home_id=(
            data.get("thm_home_id")
            if isinstance(data.get("thm_home_id"), str)
            else None
        ),
        parse_status=(
            data.get("parseStatus")
            if isinstance(data.get("parseStatus"), str)
            else None
        ),
        received_at=email.get("receivedDateTime"),
        raw_subject=(
            email.get("subject") if isinstance(email.get("subject"), str) else None
        ),
        raw_preview=(
            email.get("bodyPreview")
            if isinstance(email.get("bodyPreview"), str)
            else None
        ),
    )


def _record_pack_plan(
    canonical: dict[str, Any],
    pack: dict[str, Any],
) -> None:
    society = canonical["society"]
    flat = canonical["flat"]
    key = property_key_for(society, flat)
    graph_message_id = canonical.get("graphMessageId")
    store = get_dedup_store()
    summary = {
        "tickets": len(pack["tickets"]),
        "emails": len(pack["emailsToSend"]),
        "whatsapp": len(pack["whatsappMessages"]),
        "reminders": len(pack["reminders"]),
    }
    store.record_property(
        key,
        society,
        flat,
        thm_home_id=canonical.get("thm_home_id"),
        thm_project_id=canonical.get("thm_project_id"),
        graph_message_id=(
            graph_message_id if isinstance(graph_message_id, str) else None
        ),
        pack_summary=summary,
    )

    for ticket in pack["tickets"]:
        disabled = is_channel_disabled("tickets")
        store.record_send(
            key,
            "ticket",
            role=None,
            status="skipped" if disabled else "planned",
            error="disabled by MOVEOUT_DISABLE_TICKETS" if disabled else None,
            payload=ticket,
        )
    for email in pack["emailsToSend"]:
        recipient = email.get("recipient")
        has_recipient = isinstance(recipient, str) and bool(recipient.strip())
        store.record_send(
            key,
            "email",
            recipient=recipient if has_recipient else None,
            role=email.get("role"),
            status="planned" if has_recipient else "skipped",
            error=None if has_recipient else "recipient missing",
            payload=email,
        )
    for message in pack["whatsappMessages"]:
        phone = message.get("phone")
        has_recipient = isinstance(phone, str) and bool(phone.strip())
        store.record_send(
            key,
            "whatsapp",
            recipient=phone if has_recipient else None,
            status=(
                "skipped"
                if is_channel_disabled("whatsapp") or not has_recipient
                else "planned"
            ),
            error=(
                "disabled by MOVEOUT_DISABLE_WHATSAPP"
                if is_channel_disabled("whatsapp")
                else None if has_recipient else "recipient missing"
            ),
            payload=message,
        )
    for reminder in pack["reminders"]:
        disabled = is_channel_disabled("calendar")
        store.record_send(
            key,
            "calendar",
            status="skipped" if disabled else "planned",
            error="disabled by MOVEOUT_DISABLE_CALENDAR" if disabled else None,
            payload=reminder,
        )


def run_once(
    emails: Iterable[dict[str, Any]],
    *,
    dedup_check: DedupCheck = is_duplicate_or_stale,
    property_dedup_check: PropertyDedupCheck = check_property_dedup,
    property_mark: PropertyMark | None = None,
    thm_lookup: THMLookup = lookup_thm_home,
    sent_items: list[dict[str, Any]] | None = None,
    sent_items_loader: SentItemsLoader | None = None,
    message_recorder: MessageRecorder = _record_message_outcome,
    pack_recorder: PackRecorder = _record_pack_plan,
) -> dict[str, list[Any]]:
    """Process one supplied inbox batch without performing external side effects."""
    summary: dict[str, list[Any]] = {
        "dropped_duplicate_or_stale": [],
        "setup_replies": [],
        "auto_processed": [],
        "sent_to_review": [],
        "ignored_noise": [],
    }
    cached_sent_items = sent_items

    for email in emails:
        message_id = email.get("id")
        subject = email.get("subject")
        subject_text = subject if isinstance(subject, str) else ""
        if dedup_check(email):
            _log_dedup_decision(email, "dropped_duplicate_or_stale")
            summary["dropped_duplicate_or_stale"].append(message_id)
            message_recorder(email, "dropped_duplicate_or_stale", None)
            continue

        if is_setup_reply(subject_text):
            _log_dedup_decision(email, "setup_replies")
            summary["setup_replies"].append(parse_setup_reply(email))
            message_recorder(email, "setup_replies", None)
            continue

        parsed = parse_move_out_email(email)
        if parsed is None:
            _log_dedup_decision(email, "ignored_noise")
            summary["ignored_noise"].append(message_id)
            message_recorder(email, "ignored_noise", None)
            continue

        thm_data = thm_lookup(
            parsed["society"],
            parsed["flat"],
            parsed["tenantEmail"],
        )
        canonical = {**parsed, **thm_data}
        if (
            canonical.get("thm_home_id")
            and canonical.get("parseStatus") == "success"
            and not canonical.get("needsReview")
        ):
            society = canonical["society"]
            flat = canonical["flat"]
            category = thm_data.get("thm_category")
            if not isinstance(category, str) or category.strip().upper() != "PM":
                print(
                    f"[gate] {society} {flat} is not PM — skipping"
                )
                _log_dedup_decision(email, "ignored_noise")
                summary["ignored_noise"].append(message_id)
                message_recorder(email, "ignored_noise", canonical)
                continue

            if cached_sent_items is None:
                cached_sent_items = (
                    sent_items_loader() if sent_items_loader is not None else []
                )
            handled, reason = is_already_handled(
                cached_sent_items,
                society=thm_data.get("society"),
                flat=thm_data.get("flat"),
                tenant_email=thm_data.get("tenantEmail"),
                owner_email=thm_data.get("ownerEmail"),
                tenant_name=thm_data.get("tenantName"),
                owner_name=thm_data.get("ownerName"),
            )
            if handled:
                print(
                    f"[gate] {thm_data.get('society')} {thm_data.get('flat')} "
                    f"already handled — {reason}"
                )
                summary["dropped_duplicate_or_stale"].append(
                    canonical.get("graphMessageId", message_id)
                )
                message_recorder(email, "dropped_duplicate_or_stale", canonical)
                continue

            if property_dedup_check(society, flat):
                _log_dedup_decision(email, "dropped_property_duplicate")
                summary["dropped_duplicate_or_stale"].append(
                    canonical.get("graphMessageId", message_id)
                )
                message_recorder(
                    email,
                    "dropped_duplicate_or_stale",
                    canonical,
                )
                continue

            pack = generate_move_out_pack(canonical)
            execution_results = execute_pack(pack, dry_run=is_dry_run_enabled())
            if property_mark is not None:
                property_mark(society, flat)
            pack_recorder(canonical, pack)
            message_recorder(email, "auto_processed", canonical)
            _log_dedup_decision(email, "auto_processed")
            summary["auto_processed"].append(
                {
                    "canonical": canonical,
                    "pack": pack,
                    "execution": execution_results,
                }
            )
        else:
            _log_dedup_decision(email, "sent_to_review")
            summary["sent_to_review"].append(canonical)
            message_recorder(email, "sent_to_review", canonical)

    return summary


def _log_dedup_decision(email: dict[str, Any], bucket: str) -> None:
    message_id = email.get("id")
    identifier = message_id[:12] if isinstance(message_id, str) else "unknown"
    subject = email.get("subject")
    subject_text = subject if isinstance(subject, str) else ""
    subject_text = subject_text.replace("\r", " ").replace("\n", " ")
    if len(subject_text) > 80:
        subject_text = f"{subject_text[:77]}..."
    print(
        f"[dedup] {identifier} subject={json.dumps(subject_text, ensure_ascii=False)} "
        f"-> {bucket}"
    )


def _run_once_fresh(
    emails: Iterable[dict[str, Any]],
    *,
    thm_lookup: THMLookup = lookup_thm_home,
    sent_items: list[dict[str, Any]] | None = None,
    sent_items_loader: SentItemsLoader | None = None,
) -> dict[str, list[Any]]:
    with tempfile.TemporaryDirectory(prefix="moveout-fresh-") as directory:
        temp_path = Path(directory)
        message_db = temp_path / "messages.sqlite3"
        store = SQLiteDedupStore(
            message_db_path=message_db,
            property_db_path=temp_path / "properties.sqlite3",
        )

        def property_check(society: str, flat: str) -> bool:
            return store.is_property_processed(
                property_key_for(society, flat),
                60,
            )

        return run_once(
            emails,
            dedup_check=lambda email: is_duplicate_or_stale(
                email,
                store_path=message_db,
            ),
            property_dedup_check=property_check,
            property_mark=lambda society, flat: store.record_property(
                property_key_for(society, flat),
                society,
                flat,
            ),
            thm_lookup=thm_lookup,
            sent_items=sent_items,
            sent_items_loader=sent_items_loader,
            message_recorder=lambda *_args: None,
            pack_recorder=lambda *_args: None,
        )


def run_thm_lookup_only(
    emails: Iterable[dict[str, Any]],
    *,
    thm_lookup: THMLookup = lookup_thm_home,
) -> dict[str, list[Any]]:
    """Parse messages and fetch THM property details without downstream actions."""
    summary: dict[str, list[Any]] = {
        "thm_matches": [],
        "needs_review": [],
        "ignored_noise": [],
    }

    for email in emails:
        parsed = parse_move_out_email(email)
        if parsed is None:
            summary["ignored_noise"].append(email.get("id"))
            continue

        thm_data = thm_lookup(
            parsed["society"],
            parsed["flat"],
            parsed["tenantEmail"],
        )
        result = {**parsed, **thm_data}
        if result.get("thm_home_id") and result.get("parseStatus") == "success":
            summary["thm_matches"].append(result)
        else:
            summary["needs_review"].append(result)

    return summary


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


async def _read_live_emails(limit: int) -> list[dict[str, Any]]:
    from app.browser.outlook_reader import read_inbox

    print("--- Reading Live Emails from Outlook ---")
    return await read_inbox(limit=limit, timeout_ms=60_000)


def _print_channel_status() -> None:
    test_email = os.environ.get("MOVEOUT_TEST_EMAIL", "").strip()
    dry_run = is_dry_run_enabled()
    if dry_run:
        email_status = (
            f"DRY RUN (test recipient: {test_email})"
            if test_email
            else "DRY RUN (no live email sender configured)"
        )
    else:
        email_status = "LIVE REQUEST BLOCKED (live adapters are not configured)"
    channel_lines = (
        ("THM tickets", "tickets"),
        ("WhatsApp", "whatsapp"),
        ("Calendar", "calendar"),
    )
    print("=" * 60)
    print("MOVEOUT TEST RUN — channel status")
    print("=" * 60)
    print(f"  Email       : {email_status}")
    for label, channel in channel_lines:
        status = (
            "DISABLED"
            if is_channel_disabled(channel)
            else "DRY RUN PREVIEW (disable flag is false)"
        )
        print(f"  {label:<12}: {status}")
    print("=" * 60)


def _database_status(name: str, default_path: Path, fresh: bool) -> str:
    if fresh:
        return "ephemeral (--fresh)"
    configured = os.environ.get(name)
    if configured and configured.startswith(("postgresql://", "postgres://")):
        return "PostgreSQL (configured)"
    path = Path(configured).expanduser() if configured else default_path
    return str(path.resolve())


def _print_run_configuration(*, fresh: bool) -> None:
    test_email = os.environ.get("MOVEOUT_TEST_EMAIL", "").strip()
    tickets_disabled = is_channel_disabled("tickets")
    whatsapp_disabled = is_channel_disabled("whatsapp")
    calendar_disabled = is_channel_disabled("calendar")
    dry_run = is_dry_run_enabled()
    thm_configured = bool(
        os.environ.get("THM_PHONE", "").strip()
        and os.environ.get("THM_PASSWORD", "").strip()
    )
    print("=" * 60)
    print("MOVEOUT RUN CONFIGURATION")
    print("=" * 60)
    print(
        "  DEDUP DB            : "
        f"{_database_status('MOVEOUT_DEDUP_DB', DEFAULT_MESSAGE_DB, fresh)}"
    )
    print(
        "  PROPERTY DEDUP DB   : "
        f"{_database_status('MOVEOUT_PROPERTY_DEDUP_DB', DEFAULT_PROPERTY_DB, fresh)}"
    )
    print(
        "  TEST EMAIL          : "
        f"{test_email or '(unset — real recipients)'}"
    )
    print(f"  DISABLE TICKETS     : {str(tickets_disabled).lower()}")
    print(f"  DISABLE WHATSAPP    : {str(whatsapp_disabled).lower()}")
    print(f"  DISABLE CALENDAR    : {str(calendar_disabled).lower()}")
    print(f"  DRY RUN             : {str(dry_run).lower()}")
    print(f"  THM CREDENTIALS     : {'set' if thm_configured else 'unset'}")
    print("=" * 60)
    if fresh:
        print(
            "[dedup] --fresh: using ephemeral store "
            "(ignoring MOVEOUT_DEDUP_DB and MOVEOUT_PROPERTY_DEDUP_DB)"
        )
    if os.environ.get("MOVEOUT_THM_TEST_MATCH", "").strip():
        print(
            "[WARNING] MOVEOUT_THM_TEST_MATCH is set — THM will return fixture "
            "data for matching sender emails. Do not use in production."
        )


def _print_read_only_rows(emails: list[dict[str, Any]]) -> None:
    for index, email in enumerate(emails, start=1):
        sender = email.get("from")
        sender_info = (
            sender.get("emailAddress") if isinstance(sender, dict) else None
        )
        sender_name = (
            sender_info.get("name") if isinstance(sender_info, dict) else None
        )
        if not isinstance(sender_name, str):
            sender_name = (
                sender_info.get("address")
                if isinstance(sender_info, dict)
                and isinstance(sender_info.get("address"), str)
                else ""
            )

        def display(value: Any, limit: int) -> str:
            text = value if isinstance(value, str) else ""
            text = text.replace("\r", " ").replace("\n", " ")
            if len(text) > limit:
                text = f"{text[: limit - 3]}..."
            return text

        row_id = display(email.get("id"), 80)
        conversation_id = display(email.get("conversationId"), 80)
        subject = display(email.get("subject"), 120)
        received = display(email.get("receivedDateTime"), 80)
        preview = display(email.get("bodyPreview"), 60)
        print(
            f"[read-only] row {index}: id={row_id} "
            f"conId={conversation_id} "
            f"sender={json.dumps(display(sender_name, 100), ensure_ascii=False)} "
            f"subject={json.dumps(subject, ensure_ascii=False)} "
            f"received={json.dumps(received, ensure_ascii=False)} "
            f"preview={json.dumps(preview, ensure_ascii=False)}"
        )


def _print_thm_lookup_status() -> None:
    ai_status = (
        "Gemini enabled (regex fallback)"
        if os.environ.get("GEMINI_API_KEY", "").strip()
        else "regex only (GEMINI_API_KEY not configured)"
    )
    print("=" * 60)
    print("MOVEOUT THM LOOKUP-ONLY RUN")
    print("=" * 60)
    print("  Inbox       : READ ONLY")
    print(f"  Parser      : {ai_status}")
    print("  THM         : READ ONLY lookup")
    print("  Tickets     : NOT RUN")
    print("  Email       : NOT RUN")
    print("  WhatsApp    : NOT RUN")
    print("  Calendar    : NOT RUN")
    print("  Dedup writes: NOT RUN")
    print("=" * 60)


async def _run_cli(
    input_file: Path | None,
    *,
    input_live: bool,
    limit: int,
    thm_lookup_only: bool = False,
    fresh: bool = False,
    read_only: bool = False,
) -> dict[str, list[Any]]:
    fresh_dedup = fresh and not read_only and not thm_lookup_only
    _print_run_configuration(fresh=fresh_dedup)
    if thm_lookup_only:
        _print_thm_lookup_status()
    elif not read_only:
        _print_channel_status()
    if input_live:
        emails = await _read_live_emails(limit)
    else:
        if input_file is None:
            raise ValueError("input_file is required unless input_live is enabled.")
        print(f"--- Reading Emails from {input_file} ---")
        with input_file.open(encoding="utf-8") as source:
            emails = json.load(source)
        if not isinstance(emails, list) or any(
            not isinstance(email, dict) for email in emails
        ):
            raise ValueError("Input JSON must be an array of email objects.")

    print(f"Read {len(emails)} emails.")
    if read_only:
        _print_read_only_rows(emails)
        return {"read_only": emails}

    if thm_lookup_only:
        summary = run_thm_lookup_only(emails)
    elif fresh_dedup:
        summary = await asyncio.to_thread(
            _run_once_fresh,
            emails,
            thm_lookup=lookup_thm_home,
            sent_items_loader=lambda: asyncio.run(read_sent_items_for_gate()),
        )
    else:
        summary = await asyncio.to_thread(
            run_once,
            emails,
            sent_items_loader=lambda: asyncio.run(read_sent_items_for_gate()),
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return summary


async def _main() -> None:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    input_source = argument_parser.add_mutually_exclusive_group(required=False)
    input_source.add_argument(
        "--input-json",
        type=Path,
        help="Path to a JSON array containing Outlook-shaped email objects.",
    )
    input_source.add_argument(
        "--input-live",
        action="store_true",
        help="Read visible inbox messages from Outlook using the browser.",
    )
    argument_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        help="Maximum number of visible inbox messages to read (default: 20).",
    )
    argument_parser.add_argument(
        "--thm-lookup-only",
        action="store_true",
        help=(
            "Parse messages and fetch THM property details without dedup "
            "writes, pack generation, or downstream actions."
        ),
    )
    argument_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Use ephemeral dedup databases and ignore configured persistent stores.",
    )
    argument_parser.add_argument(
        "--read-only",
        action="store_true",
        help="Read and print visible Outlook rows, then exit without parsing.",
    )
    args = argument_parser.parse_args()

    if args.read_only and args.input_json is not None:
        argument_parser.error("--read-only cannot be combined with --input-json.")
    if args.read_only and args.thm_lookup_only:
        argument_parser.error(
            "--read-only cannot be combined with --thm-lookup-only."
        )
    if args.read_only:
        args.input_live = True
    elif args.input_json is None and not args.input_live:
        argument_parser.error("specify --input-json or --input-live.")

    await _run_cli(
        args.input_json,
        input_live=args.input_live,
        limit=args.limit,
        thm_lookup_only=args.thm_lookup_only,
        fresh=args.fresh,
        read_only=args.read_only,
    )


if __name__ == "__main__":
    asyncio.run(_main())
