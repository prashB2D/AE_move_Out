"""Python implementation of the supplied n8n move-out engine."""

from __future__ import annotations

import os
import re
import sys
import math
from datetime import date, timedelta
from typing import Any

from app.logic.engine_config import get_config
from app.logic.engine_helpers import (
    add_days,
    add_months,
    build_tab,
    clean_tag,
    encode_uri,
    excel_serial,
    format_date,
    is_working_day,
    iso_day,
    number,
    parse_date,
    rupees,
    safe_name,
    whatsapp_phone,
    workday_add,
    yes,
)
from app.logic.engine_templates import EMAIL_META, SIGNATURE_HTML, TEMPLATES, WA_SENDS

INDIA_TIME_OFFSET = timedelta(hours=5, minutes=30)
TICKET_CATEGORIES = (
    "Inspection",
    "Deep Cleaning",
    "Security Deposit Refund",
    "Owner Notification",
    "Tenant Notification",
    "Move-Out Checklist",
    "Utility Bill Settlement",
    "Society Dues Clearance",
    "Inventory Check",
    "Painting / Repairs",
    "Key Handover",
)

_STEPS: tuple[tuple[str, str, str, str], ...] = (
    ("1", "Inform owner of vacate notice (Owner 1st email)", "Service Desk", "Day0"),
    ("2", "WhatsApp groups: rename MoveIn&rarr;MoveOut{non_pm}; search, don't duplicate", "Service Desk", "non_pm"),
    ("3", "WhatsApp owner to read email (QR TenantNoticeReceived)", "Service Desk", "Day 0"),
    ("4", "Owner notifies Association - NOC, viewing access, vendor entry (Owner 2nd email + draft)", "Owner (AE drafts)", "Day 0-3"),
    ("5", "<b>VERIFY owner replied APPROVED to Association; else call & chase</b>", "Service Desk", "Day 0-3"),
    ("6", "Update THM contact with Association email/phone; fill Verified Date + Verified By", "Service Desk", "Day 0-3"),
    ("7", "2nd WhatsApp to owner for access approval (QR ApprovalRequestMoveOut)", "Service Desk", "Day 0-3"),
    ("8", "Owner move-out video guide (Owner_MO email + QR VideoOwnerMO)", "Rental Team", "OwnerVideoDate"),
    ("9", "If Association sends do's/don'ts &rarr; forward to tenant; else skip", "Service Desk", "as received"),
    ("10", "Acknowledge tenant notice; request bank details, agreements, SD, rent, incl/excl maint, dues (Tenant 1st email)", "Service Desk", "Day 0-3"),
    ("11", "WhatsApp tenant to read it (QR TenantRefundInspection)", "Service Desk", "Day 0-3"),
    ("12", "Tenant move-out video guide (Tenant_MO email + QR VideoTenantMO)", "Rental Team", "TenantVideoDate"),
    ("13", "<b>Call tenant next day</b> - collect all figures; explain pro-rata, penalties, 2-inspection model", "Service Desk", "Day 1-2"),
    ("14", "Schedule 1st Move-Out inspection via THM. <b>Create 11 tickets, assign vendors</b>", "Service Desk", "Day 1-10"),
    ("15", "On completion: THM ticket &rarr; Completed; paste inspection report URL in comments", "Service Desk", "on inspection"),
    ("16", "Prepare rough estimate within 72 hrs of 1st inspection", "Supervisor", "inspection + 72h"),
    ("17", "Email tenant estimate + Refund_MoveOutExpense.xls (Tenant 2nd email); save to My Drive", "Service Desk", "after estimate"),
    ("18", "WhatsApp tenant (QR TenantEstimateSent)", "Service Desk", "after estimate"),
    ("19", "Call tenant next day; seek approval within a week; verify SD/rent/maint in xls", "Service Desk", "estimate + 1"),
    ("20", "Plan all repairs, schedule in THM/CRM", "Service Desk", "within 6 days of notice"),
    ("21", "Painting (next working day after D; 2 days; +2 if seepage)", "Vendor", "paintRange"),
    ("22", "Pest control (within 2 working days of D)", "Vendor", "PestBy"),
    ("23", "Chimney cleaning (within 2 working days of D)", "Vendor", "ChimneyBy"),
    ("24", "Deep cleaning (3rd WD after D; 5th if seepage). <b>Pest+chimney must finish first</b>", "Vendor", "DeepCleanDate"),
    ("25", "Request Rs.10,000 buffer advance approval (Owner 3rd / UC email)", "Service Desk", "UCBufferDate"),
    ("26", "Send rough estimate to owner &rarr; WhatsApp &rarr; call next day (only after BOTH emails sent)", "Service Desk", "Day 3-14"),
    ("27", "Post amount + bank details separately in WhatsApp (QR BankAccountAEHDFC, then deposit line)", "Service Desk", "Day 3-14"),
    ("28", "<b>STEP 28 - senior/peer verification of everything; name verifier in Remarks</b>", "Senior Manager", "VerifyGateDate"),
    ("29", "Share rental agreement with accounts@ for verification before issuing estimates", "Service Desk", "before estimate"),
    ("30", "Obtain owner approval within a week; escalate on delay; issue 1st estimate on approval", "Service Desk", "Day 3-14"),
    ("32", "Obtain tenant approval within a week (no advance from tenant - deducted from SD)", "Service Desk", "Day 3-14"),
    ("33", "Add tenant bank a/c; Rs.100 test transfer; call to confirm receipt", "Service Desk", "TestTransferBy"),
    ("34", "Confirm tenant paid Association move-out charges; get receipt in WhatsApp group", "Service Desk", "DuesCallDate"),
    ("35", "Check pending dues: society maintenance, BESCOM, GAIL gas, etc.", "Service Desk", "DuesCallDate"),
    ("36", "<b>Final joint Move-Out Inspection (empty flat); update THM ticket; collect all keys/cards/NOC</b>", "AE + Owner + Tenant", "MoveOutDatePretty"),
    ("37", "Raise FRESH proforma for new work (do NOT edit 1st estimate); Final_est email; approval in 3 days", "Service Desk", "D to D+10"),
    ("38", "Share photos/proof of additional work with owner while in progress", "Service Desk", "during work"),
    ("39", "Issue 2nd invoice (difference) to owner + audit work done & quality", "Supervisor", "D to D+10"),
    ("40", "Ensure all accounts settled - vendor and AE", "Service Desk", "D+10"),
)

_SCHEDULE: dict[str, tuple[str, int]] = {
    "1": ("N", 0), "2": ("N", 0), "3": ("N", 0), "4": ("N", 1),
    "5": ("N", 1), "6": ("N", 1), "7": ("N", 1), "8": ("N", 2),
    "9": ("N", 5), "10": ("N", 0), "11": ("N", 1), "12": ("N", 2),
    "13": ("N", 1), "14": ("N", 0), "15": ("N", 3), "16": ("N", 3),
    "17": ("N", 4), "18": ("N", 4), "19": ("N", 5), "20": ("N", 5),
    "25": ("N", 2), "26": ("N", 4), "27": ("N", 4), "28": ("N", 9),
    "29": ("N", 5), "30": ("N", 6), "32": ("N", 6), "34": ("Mm", 2),
    "35": ("Mm", 2), "36": ("M", 0), "37": ("M", 3), "38": ("M", 5),
    "39": ("M", 10), "40": ("M", 10),
}
_EMAIL_ROW = {
    "Owner 1st email (notice received)": 5,
    "Owner 2nd email (Association access)": 8,
    "Owner Day-3 video-guide email": 11,
    "Tenant 1st email (acknowledge notice)": 13,
    "Tenant Day-3 video-guide email": 15,
    "Tenant 2nd email (rough estimate)": 20,
    "Owner 3rd email (Rs.10k buffer + UC price list)": 28,
    "Owner 4th email (rough estimate + advance)": 29,
    "Owner bank-account / test-transfer email": 36,
    "Final estimate email (pre-move-out, tenant CC owner)": 40,
}
_CHECKLIST_HEADER = [
    "No",
    "Action Item",
    "Owner",
    "When / Channel",
    "Planned Date",
    "Email",
    "Done",
    "Remarks",
]


def _js_string(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _first(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).split(";")[0].strip()


def _recipients(value: Any) -> list[dict[str, dict[str, str]]]:
    seen: set[str] = set()
    result = []
    for address in re.split(r"[;,]", str(value or "")):
        address = address.strip()
        if "@" not in address or address.lower() in seen:
            continue
        seen.add(address.lower())
        result.append({"emailAddress": {"address": address}})
    return result


def _clean_recipients(value: Any) -> str:
    return "; ".join(
        entry["emailAddress"]["address"] for entry in _recipients(value)
    )


def _merge(value: Any, data: dict[str, Any]) -> str:
    text = str(value if value is not None else "")
    return re.sub(
        r"\{\{(\w+)\}\}",
        lambda match: _js_string(data.get(match.group(1), match.group(0))),
        text,
    )


def _strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]*>", "", str(value))
    for old, new in (
        ("&rarr;", "->"),
        ("&mdash;", "-"),
        ("&amp;", "&"),
        ("&ge;", ">="),
        ("&#9744;", ""),
    ):
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def _file_url(file: dict[str, Any], config: dict[str, Any]) -> str:
    web = str(config.get("oneDriveWebBase") or "").rstrip("/")
    if web:
        return web + "/" + encode_uri(
            file["folderPath"] + "/" + file["fileName"]
        )
    root = str(config.get("oneDriveLocalRoot") or "").rstrip("/\\")
    if not root:
        return ""
    local_path = (
        root + "/" + file["folderPath"] + "/" + file["fileName"]
    ).replace("\\", "/")
    return "file:///" + encode_uri(local_path)


def _send_at_utc(day: date, hour_ist: int) -> str:
    minutes = hour_ist * 60 - 330
    utc_day = day
    if minutes < 0:
        utc_day -= timedelta(days=1)
        minutes += 1440
    return f"{iso_day(utc_day)}T{minutes // 60:02d}:{minutes % 60:02d}:00Z"


def _move_date(value: Any, notice_date: date, config: dict[str, Any]) -> date:
    parsed = parse_date(value)
    return parsed if parsed is not None else workday_add(notice_date, 22, config)


def _template_data(
    source: dict[str, Any],
    config: dict[str, Any],
    notice_date: date,
    move_date: date,
) -> tuple[dict[str, Any], dict[str, date], bool, bool]:
    property_type = source.get("propertyType")
    if property_type is None:
        property_type = "PM"
    is_non_pm = (
        str(property_type).lower() == "nonpm" or yes(source.get("newProperty"))
    )
    seepage = yes(source.get("seepage"))
    notice_months = number(source.get("noticePeriodMonths")) or config[
        "noticePeriodMonthsDefault"
    ]
    notice_end = add_months(notice_date, int(notice_months))
    rent_end = max(move_date, notice_end)
    paint_start = workday_add(move_date, config["wdPaintStart"], config)
    paint_days = config["wdPaintDays"] + (
        config["wdSeepagePaintBuf"] if seepage else 0
    )
    paint_end = workday_add(paint_start, paint_days - 1, config)
    pest_by = workday_add(move_date, config["wdPestBy"], config)
    chimney_by = workday_add(move_date, config["wdChimneyBy"], config)
    new_move_in = parse_date(source.get("newTenantMoveIn"))
    deep_clean_note = ""
    if yes(source.get("newTenantConfirmed")) and new_move_in:
        deep_clean = add_days(new_move_in, -3)
        deep_clean_note = " (3 days before new move-in)"
    else:
        offset = (
            config["wdDeepCleanSeepage"] if seepage else config["wdDeepClean"]
        )
        deep_clean = workday_add(move_date, offset, config)
        if seepage:
            deep_clean_note = " (seepage: 5th WD)"
    refund_75_by = workday_add(move_date, config["wdRefund75"], config)
    test_transfer_by = add_days(move_date, -config["daysTestTransfer"])

    def notice_workday(offset: int) -> date:
        current = (
            notice_date
            if offset <= 0
            else workday_add(notice_date, offset, config)
        )
        while not is_working_day(current, config):
            current += timedelta(days=1)
        return min(current, move_date)

    security_deposit = number(source.get("securityDeposit"))
    first_estimate = number(source.get("firstEstimate"))
    final_estimate = number(source.get("finalEstimate"))
    monthly_rent = number(source.get("monthlyRent"))
    if first_estimate > 0:
        advance_amount = first_estimate + config["bufferAdvance"]
        if is_non_pm:
            advance_amount += config["pmFeeNonPM"]
        upfront = rupees(advance_amount)
    else:
        upfront = "1st estimate + " + rupees(config["bufferAdvance"])
        if is_non_pm:
            upfront += " + " + rupees(config["pmFeeNonPM"]) + " PM fee"

    society_value = source.get("society")
    flat_value = source.get("flat")
    society = "" if society_value is None else society_value
    flat = "" if flat_value is None else flat_value
    property_ref = f"{society} {flat}".strip()
    tenant_email = _first(source.get("tenantEmail"))
    owner_email = _first(source.get("ownerEmail"))
    tenant_name = _first(source.get("tenantName"), "Tenant") or "Tenant"
    owner_name = _first(source.get("ownerName"), "Owner") or "Owner"
    service_desk = config.get("serviceDesk", "")
    rental = config.get("rental", "")
    accounts = config.get("accounts", "")
    renewal = config.get("renewal", "")
    tenant_cc = [
        *(
            [owner_email]
            if is_non_pm and not yes(source.get("newProperty")) and owner_email
            else []
        ),
        *([rental] if rental else []),
        *([accounts] if accounts else []),
    ]

    data: dict[str, Any] = {
        "Ref": clean_tag(society) + clean_tag(flat),
        "PropertyRef": property_ref,
        "PropertyType": property_type,
        "Society": society,
        "Flat": flat,
        "DaysToMoveOut": max(0, math_round((move_date - notice_date).days)),
        "AdvanceModel": "Non-PM / New property" if is_non_pm else "Old PM property",
        "TenantName": tenant_name,
        "TenantEmail": tenant_email,
        "OwnerName": owner_name,
        "OwnerEmail": owner_email,
        "OwnerPhone": whatsapp_phone(
            source.get("ownerPhone"), config["waCountryCode"]
        ),
        "TenantPhone": whatsapp_phone(
            source.get("tenantPhone"), config["waCountryCode"]
        ),
        "OwnerCC": "; ".join(filter(None, (accounts, rental, renewal))),
        "TenantCC": "; ".join(tenant_cc),
        "ServiceDesk": service_desk,
        "Rental": rental,
        "RentalEmail": rental,
        "Accounts": accounts,
        "AccountsEmail": accounts,
        "Renewal": renewal,
        "RenewalEmail": renewal,
        "AssociationEmail": source.get("associationEmail") or "",
        "AssociationName": source.get("associationName") or "",
        "AssociationPhone": source.get("associationPhone") or "",
        "BankName": config.get("bankName", ""),
        "BankAcName": config.get("bankAcName", ""),
        "BankAcNo": config.get("bankAcNo", ""),
        "BankIFSC": config.get("bankIFSC", ""),
        "Day0": format_date(notice_date),
        "NoticePeriodEnd": format_date(notice_end),
        "RentLiabilityEnd": format_date(rent_end),
        "MoveOutDatePretty": format_date(move_date),
        "OwnerVideoDate": format_date(notice_workday(config["dayOwnerVideo"] - 1)),
        "TenantVideoDate": format_date(notice_workday(config["dayTenantVideo"] - 1)),
        "UCBufferDate": format_date(notice_workday(config["dayUCBuffer"] - 1)),
        "VerifyGateDate": format_date(notice_workday(config["dayVerifyGate"] - 1)),
        "ReadinessDate": format_date(notice_workday(config["dayReadiness"] - 1)),
        "DuesCallDate": format_date(notice_workday(config["dayDuesCall"] - 1)),
        "PaintStart": format_date(paint_start),
        "PaintEnd": format_date(paint_end),
        "PestBy": format_date(pest_by),
        "ChimneyBy": format_date(chimney_by),
        "DeepCleanDate": format_date(deep_clean) + deep_clean_note,
        "TestTransferBy": format_date(test_transfer_by),
        "Refund75By": format_date(refund_75_by),
        "Refund75Amt": rupees(security_deposit * 0.75)
        + " (indicative; net of dues per xls)",
        "Refund25Amt": rupees(security_deposit * 0.25)
        + " (must retain >= "
        + rupees(config["refundFloor"])
        + " after estimate)",
        "Refund25By": "maintenance completion + "
        + str(config["daysRefund25"])
        + " days (tentative)",
        "SecurityDeposit": (
            rupees(security_deposit) if security_deposit else "[confirm]"
        ),
        "MonthlyRent": rupees(monthly_rent) if monthly_rent else "[confirm]",
        "MaintenanceInclusive": (
            source.get("maintenanceInclusive")
            if source.get("maintenanceInclusive") is not None
            else ""
        ),
        "FirstEstimatePretty": (
            rupees(first_estimate) if first_estimate else "Rs. ____"
        ),
        "FinalEstimatePretty": (
            rupees(final_estimate) if final_estimate else "Rs. ____"
        ),
        "UpfrontAdvance": upfront,
        "RefundInterest": config["refundInterest"],
        "OwnerVideoLink": config["ownerVideoLink"],
        "TenantVideoLink": config["tenantVideoLink"],
        "Today": format_date(date.today()),
    }
    return data, {
        "paintStart": paint_start,
        "pestBy": pest_by,
        "chimneyBy": chimney_by,
        "deepClean": deep_clean,
        "refund75By": refund_75_by,
        "testTransferBy": test_transfer_by,
        "noticeEnd": notice_end,
        "rentEnd": rent_end,
    }, is_non_pm, seepage


def math_round(value: int | float) -> int:
    return math.floor(value + 0.5)


def _send_info(meta: dict[str, Any], notice_date: date, move_date: date) -> dict[str, str]:
    schedule = meta.get("send") or {}
    if schedule.get("preNotice"):
        return {
            "label": "When tenant first calls (before Day 0)",
            "tag": "Pre-notice",
        }
    if schedule.get("moveDay"):
        return {
            "label": f"Move-out day ({format_date(move_date)})",
            "tag": "Move-out day " + move_date.strftime("%d-%b-%Y"),
        }
    if schedule.get("moveMinus") is not None:
        day = add_days(move_date, -int(schedule["moveMinus"]))
        offset = schedule["moveMinus"]
        return {
            "label": f"~{offset} days before move-out ({format_date(day)})",
            "tag": f"{offset} days before move-out {day.strftime('%d-%b-%Y')}",
        }
    if schedule.get("after"):
        return {"label": "After " + schedule["after"], "tag": "After " + schedule["after"]}
    if schedule.get("day") is not None:
        offset = int(schedule["day"])
        day = add_days(notice_date, offset)
        return {
            "label": f"Day {offset} ({format_date(day)})",
            "tag": f"Day {offset} {day.strftime('%d-%b-%Y')}",
        }
    return {"label": "As per process", "tag": "TBD"}


def _planned_date(
    step: str,
    notice_date: date,
    move_date: date,
    computed: dict[str, date],
    config: dict[str, Any],
) -> date | None:
    special = {
        "21": "paintStart",
        "22": "pestBy",
        "23": "chimneyBy",
        "24": "deepClean",
        "33": "testTransferBy",
    }
    if step in special:
        return computed[special[step]]
    schedule = _SCHEDULE.get(step)
    if schedule is None:
        return None
    anchor, offset = schedule
    if anchor == "N":
        current = (
            notice_date
            if offset <= 0
            else workday_add(notice_date, offset, config)
        )
        while not is_working_day(current, config):
            current += timedelta(days=1)
        # Cap at the move-out date only when it is after the notice. If the
        # move-out date is before the notice (late notice / bad input), capping
        # would pull every send date into the past and mark all emails immediate.
        return min(current, move_date) if move_date >= notice_date else current
    if anchor == "M":
        return workday_add(move_date, offset, config)
    return add_days(move_date, -offset)


def _build_pack(source: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    society = source.get("society") or ""
    flat = source.get("flat") or ""
    notice_date = parse_date(source.get("noticeDate")) or date.today()
    move_date = _move_date(source.get("moveOutDate"), notice_date, config)
    if move_date < notice_date:
        print(
            f"[engine] WARNING: move-out date {move_date.isoformat()} is before "
            f"notice date {notice_date.isoformat()}; scheduling emails from "
            "the notice date. Check the source email.",
            file=sys.stderr,
        )
    data, computed, is_non_pm, seepage = _template_data(
        source, config, notice_date, move_date
    )
    property_ref = data["PropertyRef"]
    reference = data["Ref"]

    def planned(step: str) -> date | None:
        return _planned_date(
            step, notice_date, move_date, computed, config
        )

    folder_parent = (
        str(config["oneDriveBase"]) + "/" + str(config.get("emailFolder") or "")
    )
    property_folder = safe_name(property_ref)
    property_path = folder_parent + "/" + property_folder
    emails_to_send: list[dict[str, Any]] = []
    email_files: list[dict[str, Any]] = []
    for sequence, template in enumerate(
        (item for item in TEMPLATES if item.get("type") == "email"), 1
    ):
        label = template["label"]
        meta = EMAIL_META.get(
            label, {"who": "AE", "send": {}, "step": "", "mode": "save"}
        )
        send_info = _send_info(meta, notice_date, move_date)
        to = _clean_recipients(_merge(template.get("to") or "", data))
        cc = _clean_recipients(_merge(template.get("cc") or "", data))
        subject = _merge(template.get("subject") or "", data)
        body = _merge(template.get("body") or "", data)
        if template.get("html"):
            message_html = _merge(template["html"], data)
        else:
            message_html = (
                "<div style=\"font-family:'Montserrat','Calibri',Arial,sans-serif;"
                "font-size:12pt;color:#242424;line-height:1.45\">"
                + body.replace("\n", "<br>")
                + "</div>"
            )
        message_html += "<br>" + _merge(SIGNATURE_HTML, data)
        who = str(meta.get("who", "AE"))
        owner_only = "owner" in who.lower() and "tenant" not in who.lower()
        tenant_only = "tenant" in who.lower() and "owner" not in who.lower()
        recipient_folder = "Owner" if owner_only else "Tenant" if tenant_only else ""
        target_folder = (
            property_path + "/" + recipient_folder
            if recipient_folder
            else property_path
        )
        send_entry = meta.get("mode") == "send"
        if send_entry:
            immediate_recipients = to
            test_address = os.environ.get("MOVEOUT_TEST_EMAIL", "").strip()
            if test_address and immediate_recipients:
                print(
                    "[email] test-mode: redirect "
                    f"{immediate_recipients} -> {test_address}"
                )
                immediate_recipients = test_address
                cc = ""
            email = {
                "to": immediate_recipients,
                "cc": cc,
                "subject": subject,
                "html": message_html,
                "label": label,
                "step": meta.get("step") or "",
                "sendOn": send_info["label"],
                "role": who.lower(),
                "recipient": immediate_recipients or None,
                "body": body,
            }
            due = planned(email["step"]) if email["step"] else None
            immediate = due is None or due <= notice_date
            email["immediate"] = immediate
            email["sendAtISO"] = (
                "" if immediate else _send_at_utc(due, int(config["sendHourIST"]))
            )
            email["sendDateISO"] = iso_day(due) if due else ""
            email["sendDatePretty"] = (
                format_date(due) if due else send_info["label"]
            )
            graph_message: dict[str, Any] = {
                "subject": subject,
                "body": {"contentType": "HTML", "content": message_html},
                "toRecipients": _recipients(immediate_recipients),
                "ccRecipients": _recipients(cc),
            }
            if email["sendAtISO"]:
                graph_message["categories"] = [
                    "MoveOut:Send:" + email["sendDateISO"]
                ]
            email["graphMessage"] = graph_message
            emails_to_send.append(email)
        else:
            file_name = safe_name(f"{sequence:02d} {label}") + ".eml"
            headers = [
                "To: " + to,
                "Cc: " + cc if cc else None,
                "Subject: " + subject,
                "X-Unsent: 1",
                "MIME-Version: 1.0",
                'Content-Type: text/html; charset="utf-8"',
                "Content-Transfer-Encoding: 8bit",
            ]
            eml = "\r\n".join(header for header in headers if header)
            eml += "\r\n\r\n<html><body>" + message_html + "</body></html>\r\n"
            email_files.append(
                {
                    "folderPath": target_folder,
                    "folderParent": folder_parent,
                    "propertyFolder": property_folder,
                    "propertyPath": property_path,
                    "recipientFolder": recipient_folder,
                    "fileName": file_name,
                    "content": eml,
                    "label": label,
                    "step": meta.get("step") or "",
                    "sendOn": send_info["label"],
                }
            )

    emails_to_send_now = [email for email in emails_to_send if email["immediate"]]
    if len(emails_to_send_now) > 2:
        print(
            f"[engine] WARNING: {len(emails_to_send_now)} immediate emails; "
            "expected at most 2 (owner 1st + tenant 1st).",
            file=sys.stderr,
        )
    emails_to_schedule = [email for email in emails_to_send if not email["immediate"]]
    whatsapps: list[dict[str, Any]] = []
    for message in WA_SENDS:
        role = message["to"]
        test_number = config.get("waTestNumber") or ""
        number_to = (
            whatsapp_phone(test_number, config["waCountryCode"])
            if test_number
            else data["OwnerPhone" if role == "owner" else "TenantPhone"]
        )
        if number_to:
            whatsapps.append(
                {
                    "to": number_to,
                    "templateName": message["templateName"],
                    "languageCode": config["waLanguage"],
                    "variables": [_js_value(data.get(key, "")) for key in message["vars"]],
                    "recipientRole": role,
                    "role": role,
                    "phone": number_to,
                    "message": message["templateName"],
                    "immediate": True,
                }
            )

    sent_by_step = {
        email["step"]: email for email in emails_to_send if email.get("step")
    }
    files_by_step = {
        item["step"]: item for item in email_files if item.get("step")
    }

    def sent_text(step: str) -> str:
        item = sent_by_step.get(step)
        if not item:
            return ""
        due = planned(step)
        immediate = due is None or due <= notice_date
        when = format_date(due) if due else item.get("sendOn", "")
        return ("Sent" if immediate else "Scheduled") + (
            " - " + when if when else " via workflow"
        )

    def email_cell(step: str, formula: bool = False) -> str:
        file = files_by_step.get(step)
        if file:
            if formula:
                url = _file_url(file, config)
                return (
                    f'=HYPERLINK("{url}","Open email")'
                    if url
                    else "Saved: " + file["fileName"]
                )
            return "Open email"
        return sent_text(step)

    status_column = [""] * 39
    email_column = [""] * 39
    handled_rows: set[int] = {17}
    for item in emails_to_send:
        row = _EMAIL_ROW.get(item["label"])
        if row:
            status_column[row - 5] = sent_text(item["step"])
            handled_rows.add(row)
    for item in email_files:
        row = _EMAIL_ROW.get(item["label"])
        if row:
            url = _file_url(item, config)
            email_column[row - 5] = (
                f'=HYPERLINK("{url}","Open email")'
                if url
                else "Saved: " + item["fileName"]
            )
            handled_rows.add(row)
    auto_status = [""] * 39
    for row in handled_rows:
        auto_status[row - 5] = "Handled by automation workflow"

    def static_row(action: str, owner: str = "", when: str = "") -> dict[str, str]:
        return {
            "No": "",
            "Action Item": action,
            "Owner": owner,
            "When / Channel": when,
            "Done": "",
            "Remarks": "",
        }

    checklist_rows = [
        static_row("KEY DATES"),
        static_row("Day 0 - notice received", "", data["Day0"]),
        static_row(
            "Day 10 - STEP 28 senior verification",
            "Senior Manager",
            data["VerifyGateDate"],
        ),
        static_row("D - keys handover / move-out", "", data["MoveOutDatePretty"]),
        static_row("Painting", "Vendor", data["PaintStart"] + " -> " + data["PaintEnd"]),
        static_row(
            "Pest / Chimney by",
            "Vendor",
            data["PestBy"] + " / " + data["ChimneyBy"],
        ),
        static_row("Deep cleaning", "Vendor", data["DeepCleanDate"]),
        static_row("Rs.100 test transfer by", "Service Desk", data["TestTransferBy"]),
        static_row(
            "75% / 25% SD refund by",
            "Accounts",
            data["Refund75By"] + " / " + data["Refund25By"],
        ),
        static_row("40 ACTION ITEMS"),
    ]
    for step, action, owner, when in _STEPS:
        if when == "non_pm":
            when_text = " (drop 'PM')" if is_non_pm else ""
            action = action.format(non_pm=when_text)
            when_text = "Day 0"
        elif when == "paintRange":
            when_text = data["PaintStart"] + " -> " + data["PaintEnd"]
        elif when in data:
            when_text = str(data[when])
        elif when == "DuesCallDate":
            when_text = data["DuesCallDate"]
        else:
            when_text = when
        due = planned(step)
        checklist_rows.append(
            {
                "No": step,
                "Action Item": _strip_html(action),
                "Owner": owner,
                "When / Channel": _strip_html(when_text),
                "Planned Date": format_date(due) if due else "",
                "Email": email_cell(step),
                "EmailFormula": email_cell(step, formula=True),
                "Done": "",
                "Remarks": "",
            }
        )

    checklist_values = [_CHECKLIST_HEADER] + [
        [
            row.get("No", ""),
            row.get("Action Item", ""),
            row.get("Owner", ""),
            row.get("When / Channel", ""),
            row.get("Planned Date", ""),
            row.get("Email", ""),
            row.get("Done", ""),
            row.get("Remarks", ""),
        ]
        for row in checklist_rows
    ]
    checklist_formulas = [_CHECKLIST_HEADER] + [
        [
            row.get("No", ""),
            row.get("Action Item", ""),
            row.get("Owner", ""),
            row.get("When / Channel", ""),
            row.get("Planned Date", ""),
            row.get("EmailFormula", ""),
            row.get("Done", ""),
            row.get("Remarks", ""),
        ]
        for row in checklist_rows
    ]
    checklist_tab = build_tab(str(society), str(flat))
    checklist_header_values = [
        ["Flat# :", flat or "", "", "AE Manager:", "", "", "", "Notice Date:", excel_serial(notice_date)],
        ["Apartment Complex:", society or "", "", "AE Supervisor:", "", "", "", "Vacate Date:", excel_serial(move_date)],
    ]

    first_inspection = add_days(notice_date, 2)
    first_estimate = add_days(first_inspection, 1)
    final_estimate = add_days(move_date, 1)
    def next_workday(value: date) -> date:
        while not is_working_day(value, config):
            value += timedelta(days=1)
        return value

    vendor_plus_two = next_workday(add_days(move_date, 2))
    ticket_deep_clean = next_workday(add_days(move_date, 3))
    ticket_dates = {
        "firstInspection": first_inspection,
        "firstEstimate": first_estimate,
        "moveOut": move_date,
        "finalEstimate": final_estimate,
        "paintStart": computed["paintStart"],
        "pestBy": next_workday(add_days(move_date, 1)),
        "chimneyBy": next_workday(add_days(move_date, 1)),
        "vendorPlus2": vendor_plus_two,
        "deepClean": ticket_deep_clean,
    }
    home_id = source.get("thm_home_id") or ""
    project_id = source.get("thm_project_id") or ""
    tickets = []
    for ticket_config in config["ticketTypes"]:
        category = ticket_config.get("category")
        if not category:
            continue
        ticket: dict[str, Any] = {
            "ticketBelongsTo": "Home",
            "ticketBelongsToRefId": home_id,
            "category": category,
            "home": home_id,
            "project": project_id,
            "ticketCategory": category,
            "ticketCategoryType": "Maintenance",
            "status": "open",
            "priority": "low",
            "notes": (
                f"[{str(ticket_config.get('name') or '').upper()}] "
                f"Move-out job for {property_ref} - keys handover "
                f"{data['MoveOutDatePretty']}."
            ),
        }
        if ticket_config.get("agentId"):
            ticket["agents"] = [ticket_config["agentId"]]
        if ticket_config.get("subCategory"):
            ticket["subCategory"] = ticket_config["subCategory"]
        date_key = ticket_config.get("dateKey")
        if date_key in ticket_dates:
            due_date = iso_day(ticket_dates[date_key])
            ticket["jobDate"] = due_date
            ticket["deadlineDate"] = due_date
        tickets.append(ticket)
    tickets_note = ""
    if not home_id or not project_id:
        missing = (
            "home id" if not home_id else ""
        ) + (" + " if not home_id and not project_id else "") + (
            "project id" if not project_id else ""
        )
        tickets_note = (
            f"WARNING: missing {missing} from THM - tickets may fail."
        )

    def reminder(subject: str, due: date) -> dict[str, str]:
        return {
            "subject": subject,
            "startISO": f"{iso_day(due)}T10:00:00",
            "endISO": f"{iso_day(due)}T10:30:00",
        }

    reminders = [
        reminder(
            f"MO {reference}: Owner & Tenant move-out video guides (Day 3)",
            add_days(notice_date, config["dayOwnerVideo"]),
        ),
        reminder(
            f"MO {reference}: STEP 28 senior verification gate - name verifier in checklist",
            add_days(notice_date, config["dayVerifyGate"]),
        ),
        reminder(
            f"MO {reference}: Rs.100 test transfer to tenant + call to confirm (Step 33)",
            computed["testTransferBy"],
        ),
        reminder(
            f"MO {reference}: Day-27 call - association charges + pending dues (Steps 34-35)",
            add_days(notice_date, config["dayDuesCall"]),
        ),
        reminder(f"MO {reference}: Painting starts (Step 21)", computed["paintStart"]),
        reminder(
            f"MO {reference}: Deep cleaning - pest & chimney must be done first (Step 24)",
            computed["deepClean"],
        ),
        reminder(
            f"MO {reference}: Final joint inspection (Step 36)", move_date
        ),
        reminder(
            f"MO {reference}: Release 75% SD refund tranche (Phase 8)",
            computed["refund75By"],
        ),
    ]

    checklist = {
        "checklistWorkbookPath": (
            str(config["oneDriveBase"]) + "/" + str(config["checklistWorkbook"])
        ),
        "checklistTab": checklist_tab,
        "checklistTemplateName": "TEMPLATE",
        "checklistHeaderRange": "A2:I3",
        "checklistHeaderValues": checklist_header_values,
        "checklistTicketRow": 17,
        "checklistStatusColC": status_column,
        "checklistEmailColH": email_column,
        "checklistAutoStatusColK": auto_status,
        "checklistValues": checklist_values,
        "checklistFormulas": checklist_formulas,
        "checklistRange": f"A1:H{len(checklist_values)}",
        "checklistTableAddress": (
            f"'{checklist_tab}'!A1:H{len(checklist_values)}"
        ),
        "checklistRows": checklist_rows,
        "checklist": {
            "tab_name": property_ref,
            "header_values": [
                society,
                flat,
                source.get("tenantName"),
                source.get("moveOutDate") or iso_day(move_date),
            ],
            "status_rows": ["Pending"] * 40,
            "values": checklist_values,
            "formulas": checklist_formulas,
            "rows": checklist_rows,
            "status_col_c": status_column,
            "email_col_h": email_column,
            "auto_status_col_k": auto_status,
            "range": f"A1:H{len(checklist_values)}",
            "template_name": "TEMPLATE",
            "ticket_row": 17,
            "workbook_path": (
                str(config["oneDriveBase"])
                + "/"
                + str(config["checklistWorkbook"])
            ),
            "email_folder_parent": folder_parent,
        },
    }
    summary = (
        f"Move-Out pack for {property_ref}: {len(emails_to_send)} emails to SEND, "
        f"{len(email_files)} .eml files to SAVE, {len(tickets)} tickets, "
        f"{len(reminders)} reminders. Move-out {data['MoveOutDatePretty']}."
    )
    return {
        "ref": reference,
        "propertyRef": property_ref,
        "folderBase": config["oneDriveBase"],
        "ownerPhone": data["OwnerPhone"],
        "tenantPhone": data["TenantPhone"],
        **checklist,
        "emailsToSend": emails_to_send,
        "emailsToSendNow": emails_to_send_now,
        "emailsToSchedule": emails_to_schedule,
        "emailFiles": email_files,
        "emailFolderParent": folder_parent,
        "reminders": reminders,
        "whatsapps": whatsapps,
        "tickets": tickets,
        "ticketsNote": tickets_note,
        "waTestMode": (
            f"TEST MODE: all WhatsApps -> {config['waTestNumber']}"
            if config.get("waTestNumber")
            else "LIVE: WhatsApps go to real owner/tenant"
        ),
        "summary": summary,
        # Backwards-compatible keys consumed by the existing local pipeline.
        "whatsappMessages": whatsapps,
        "checklist": checklist["checklist"],
    }


def _js_value(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def generate_move_out_pack(canonical_item: dict[str, Any]) -> dict[str, Any]:
    """Generate the complete source-compatible move-out automation payload."""
    config = get_config()
    return _build_pack(canonical_item, config)
