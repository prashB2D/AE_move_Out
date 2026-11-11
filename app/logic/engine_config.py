"""Safe configuration defaults for the n8n engine port.

Sensitive deployment values are supplied through ``MOVEOUT_ENGINE_*``
environment variables and are intentionally absent from this module.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any


_DEFAULTS: dict[str, Any] = {
    "serviceDesk": "",
    "rental": "",
    "accounts": "",
    "renewal": "",
    "bankName": "[bank name not configured]",
    "bankAcName": "[bank account name not configured]",
    "bankAcNo": "[bank account number not configured]",
    "bankIFSC": "[bank IFSC not configured]",
    "oneDriveBase": "Move-Out Automation",
    "oneDriveLocalRoot": "",
    "oneDriveWebBase": "",
    "checklistWorkbook": "Move-Out Checklists.xlsx",
    "emailFolder": "",
    "signature": {
        "orgName": "Agarwal Estates",
        "serviceDesk": "",
    },
    "sendHourIST": 10,
    "waCountryCode": "91",
    "waLanguage": "en",
    "ownerVideoLink": "https://youtu.be/Y72l4VvZjs8",
    "tenantVideoLink": "https://youtu.be/lRyO5T1IU5c",
    "waTestNumber": "",
    "sundayOff": True,
    "saturdayOff": False,
    "holidays": ["2026-08-15", "2026-10-02", "2026-11-01", "2026-12-25"],
    "dayOwnerVideo": 3,
    "dayTenantVideo": 3,
    "dayUCBuffer": 3,
    "dayVerifyGate": 10,
    "dayReadiness": 20,
    "dayDuesCall": 27,
    "noticePeriodMonthsDefault": 1,
    "wdPaintStart": 1,
    "wdPaintDays": 2,
    "wdSeepagePaintBuf": 2,
    "wdPestBy": 2,
    "wdChimneyBy": 2,
    "wdDeepClean": 3,
    "wdDeepCleanSeepage": 5,
    "wdRefund75": 2,
    "daysRefund25": 10,
    "daysTestTransfer": 7,
    "bufferAdvance": 10000,
    "mgmtReserve": 5000,
    "workStartHeadroom": 5000,
    "pmFeeNonPM": 5900,
    "refundFloor": 10000,
    "refundInterest": "24% p.a.",
    "ticketTypes": [
        {
            "name": "Deep Cleaning",
            "category": "Home Cleaning",
            "subCategory": "Deep cleaning",
            "dateKey": "deepClean",
        },
        {
            "name": "First Moveout Inspection",
            "category": "Moveout First Inspection",
            "dateKey": "firstInspection",
        },
        {
            "name": "First Moveout Estimate",
            "category": "First Moveout Estimate",
            "dateKey": "firstEstimate",
        },
        {
            "name": "Final Moveout Inspection",
            "category": "Moveout Final Inspection",
            "dateKey": "moveOut",
        },
        {
            "name": "Final Moveout Estimate",
            "category": "Final Moveout Estimate",
            "dateKey": "finalEstimate",
        },
        {
            "name": "Plumbing",
            "category": "Plumbing",
            "dateKey": "vendorPlus2",
        },
        {
            "name": "Painting",
            "category": "Painting",
            "dateKey": "paintStart",
        },
        {
            "name": "Electrical",
            "category": "Electrical",
            "dateKey": "vendorPlus2",
        },
        {
            "name": "Pest Control",
            "category": "Pest Control",
            "dateKey": "pestBy",
        },
        {
            "name": "Carpentry",
            "category": "Carpentry",
            "dateKey": "vendorPlus2",
        },
        {
            "name": "Chimney Cleaning",
            "category": "Home Cleaning",
            "subCategory": "Chimney cleaning",
            "dateKey": "chimneyBy",
        },
    ],
}

_ENV_FIELDS = {
    "serviceDesk": "MOVEOUT_ENGINE_SERVICE_DESK_EMAIL",
    "rental": "MOVEOUT_ENGINE_RENTAL_EMAIL",
    "accounts": "MOVEOUT_ENGINE_ACCOUNTS_EMAIL",
    "renewal": "MOVEOUT_ENGINE_RENEWAL_EMAIL",
    "bankName": "MOVEOUT_ENGINE_BANK_NAME",
    "bankAcName": "MOVEOUT_ENGINE_BANK_ACCOUNT_NAME",
    "bankAcNo": "MOVEOUT_ENGINE_BANK_ACCOUNT_NUMBER",
    "bankIFSC": "MOVEOUT_ENGINE_BANK_IFSC",
    "oneDriveLocalRoot": "MOVEOUT_ENGINE_ONEDRIVE_LOCAL_ROOT",
    "oneDriveWebBase": "MOVEOUT_ENGINE_ONEDRIVE_WEB_BASE",
    "emailFolder": "MOVEOUT_ENGINE_EMAIL_FOLDER",
    "waTestNumber": "MOVEOUT_ENGINE_WHATSAPP_TEST_NUMBER",
}


def get_config() -> dict[str, Any]:
    """Return source defaults with explicitly configured deployment values."""
    config = deepcopy(_DEFAULTS)
    for field, env_name in _ENV_FIELDS.items():
        value = os.environ.get(env_name)
        if value is not None:
            config[field] = value.strip()
    config["signature"]["serviceDesk"] = config["serviceDesk"]
    agent_ids = os.environ.get("MOVEOUT_ENGINE_TICKET_AGENT_IDS", "")
    if agent_ids.strip():
        try:
            configured_ids = json.loads(agent_ids)
        except json.JSONDecodeError as error:
            raise ValueError(
                "MOVEOUT_ENGINE_TICKET_AGENT_IDS must be a JSON object"
            ) from error
        if not isinstance(configured_ids, dict):
            raise ValueError(
                "MOVEOUT_ENGINE_TICKET_AGENT_IDS must be a JSON object"
            )
        for ticket in config["ticketTypes"]:
            agent_id = configured_ids.get(ticket["name"])
            if agent_id:
                ticket["agentId"] = str(agent_id)
    return config
