"""Parity checks for the source-derived n8n engine output contract."""

from app.logic.engine import generate_move_out_pack
from app.logic.engine_templates import EMAIL_META, SIGNATURE_HTML, TEMPLATES

SOURCE_ROOT_KEYS = {
    "ref",
    "propertyRef",
    "folderBase",
    "ownerPhone",
    "tenantPhone",
    "checklistWorkbookPath",
    "checklistTab",
    "checklistTemplateName",
    "checklistHeaderRange",
    "checklistHeaderValues",
    "checklistTicketRow",
    "checklistStatusColC",
    "checklistEmailColH",
    "checklistAutoStatusColK",
    "checklistValues",
    "checklistFormulas",
    "checklistRange",
    "checklistTableAddress",
    "checklistRows",
    "emailsToSend",
    "emailsToSendNow",
    "emailsToSchedule",
    "emailFiles",
    "emailFolderParent",
    "reminders",
    "whatsapps",
    "tickets",
    "ticketsNote",
    "waTestMode",
    "summary",
}


def _sample_pack() -> dict:
    return generate_move_out_pack(
        {
            "society": "Sobha Daisy",
            "flat": "3033",
            "thm_home_id": "TEST-HOME-3033",
            "thm_project_id": "TEST-PROJECT-SOBHA",
            "propertyType": "PM",
            "tenantName": "Test Tenant",
            "tenantEmail": "tenant@example.test",
            "tenantPhone": "9876543210",
            "ownerName": "Test Owner",
            "ownerEmail": "owner@example.test",
            "ownerPhone": "9886004645",
            "noticeDate": "2026-11-11",
            "moveOutDate": "2026-12-15",
            "noticePeriodMonths": 1,
            "securityDeposit": 100000,
            "monthlyRent": 50000,
            "firstEstimate": 1000,
            "finalEstimate": 1000,
            "maintenanceInclusive": "N",
            "seepage": "N",
            "newTenantConfirmed": "N",
            "newTenantMoveIn": "",
            "associationEmail": "association@example.test",
            "associationName": "Test Association",
            "associationPhone": "9876500000",
        }
    )


def test_source_template_counts_and_major_output_structures() -> None:
    pack = _sample_pack()

    assert SOURCE_ROOT_KEYS <= pack.keys()
    assert len(TEMPLATES) == 13
    assert len(EMAIL_META) == 13
    assert len(pack["emailsToSend"]) == 7
    assert len(pack["emailsToSendNow"]) == 2
    assert len(pack["emailsToSchedule"]) == 5
    assert len(pack["emailFiles"]) == 6
    assert len(pack["reminders"]) == 8
    assert len(pack["whatsapps"]) == 8
    assert len(pack["tickets"]) == 11
    assert len(pack["checklistRows"]) == 49
    assert len(pack["checklistValues"]) == 50
    assert len(pack["checklistFormulas"]) == 50
    assert all(len(row) == 8 for row in pack["checklistValues"])
    assert len(pack["checklistStatusColC"]) == 39
    assert len(pack["checklistEmailColH"]) == 39
    assert len(pack["checklistAutoStatusColK"]) == 39
    checklist = pack["checklist"]
    assert len(checklist["values"]) == 50
    assert len(checklist["formulas"]) == 50
    assert len(checklist["rows"]) == 49
    assert len(checklist["status_rows"]) == 40
    assert len(checklist["status_col_c"]) == 39
    assert len(checklist["email_col_h"]) == 39
    assert len(checklist["auto_status_col_k"]) == 39
    assert checklist["range"] == pack["checklistRange"]
    assert checklist["template_name"] == pack["checklistTemplateName"]
    assert checklist["ticket_row"] == pack["checklistTicketRow"]
    assert checklist["workbook_path"] == pack["checklistWorkbookPath"]
    assert checklist["email_folder_parent"] == pack["emailFolderParent"]
    assert checklist["values"] is pack["checklistValues"]
    assert checklist["formulas"] is pack["checklistFormulas"]
    assert checklist["rows"] is pack["checklistRows"]

    assert pack["emailsToSend"][0]["label"] == "Owner 1st email (notice received)"
    assert pack["emailsToSend"][0]["immediate"] is True
    assert pack["emailsToSend"][1]["immediate"] is False
    assert pack["emailsToSend"][1]["graphMessage"]["body"]["contentType"] == "HTML"
    assert pack["emailsToSend"][1]["graphMessage"]["categories"] == [
        "MoveOut:Send:2026-11-12"
    ]
    for email in pack["emailsToSend"]:
        assert {
            "to",
            "cc",
            "subject",
            "html",
            "label",
            "step",
            "sendOn",
            "immediate",
            "sendAtISO",
            "sendDateISO",
            "sendDatePretty",
            "graphMessage",
        } <= email.keys()
        assert {
            "subject",
            "body",
            "toRecipients",
            "ccRecipients",
        } <= email["graphMessage"].keys()
    assert {
        "folderPath",
        "folderParent",
        "propertyFolder",
        "propertyPath",
        "fileName",
        "content",
        "label",
        "step",
        "sendOn",
    } <= pack["emailFiles"][0].keys()
    assert {
        "ticketBelongsTo",
        "ticketBelongsToRefId",
        "home",
        "project",
        "ticketCategory",
        "ticketCategoryType",
        "status",
        "priority",
        "notes",
    } <= pack["tickets"][0].keys()
    assert {
        "to",
        "templateName",
        "languageCode",
        "variables",
        "recipientRole",
    } <= pack["whatsapps"][0].keys()
    assert pack["emailFiles"][0]["content"].startswith(
        "To: tenant@example.test\r\n"
    )
    assert pack["tickets"][0]["ticketCategory"] == "Home Cleaning"
    assert pack["tickets"][0]["jobDate"] == "2026-12-18"
    assert pack["whatsapps"][0]["to"] == "919886004645"
    assert pack["checklistTab"] == "Sobha Daisy-3033"
    assert pack["checklistHeaderRange"] == "A2:I3"
    assert pack["checklistTicketRow"] == 17
    assert pack["reminders"][0]["startISO"] == "2026-11-14T10:00:00"


def test_source_html_signature_and_embedded_logos_are_preserved() -> None:
    assert SIGNATURE_HTML.count("data:image/") == 7
    assert len(SIGNATURE_HTML) > 130_000
    first_html = _sample_pack()["emailsToSend"][0]["html"]
    assert "data:image/png;base64," in first_html
    assert first_html.endswith("</table>")


def test_source_runtime_counts_document_audit_mismatches() -> None:
    pack = _sample_pack()

    assert len(TEMPLATES) == 13
    assert len(pack["emailsToSend"]) == 7
    assert len(pack["emailFiles"]) == 6
    assert len(pack["checklistRows"]) == 49
    assert len(pack["checklistValues"]) == 50
