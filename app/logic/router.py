"""Route move-out notices using the THM read-only home lookup API."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.logic.parser import parse_move_out_email

THM_BASE_URL = os.environ.get(
    "THM_BASE_URL",
    "https://dashboard.thehousemonk.com",
).rstrip("/")
THM_TIMEOUT_SECONDS = 20
THM_MAX_PAGES = 40
THM_HOME_FIELDS = (
    "_id",
    "name",
    "displayName",
    "tenancyTerms",
    "sharingType",
    "tenants",
    "tenantObjs",
    "owners",
    "listing",
    "project",
)
_SECURITY_DEPOSIT_FIELDS = (
    "securityDeposit",
    "deposit",
    "depositFromTenant",
    "securityDepositAmount",
)
_OPTIONAL_RESULT_FIELDS = (
    "thm_project_id",
    "thm_listing_id",
    "ownerEmail",
    "ownerPhone",
    "tenantPhone",
    "associationEmail",
    "associationName",
    "associationPhone",
    "thm_category",
    "propertyType",
    "monthlyRent",
    "securityDeposit",
    "leaseExpiry",
)


def _not_found() -> dict[str, Any]:
    return {"thm_home_id": None, "thm_status": "not_found"}


def _thm_test_match_fixture() -> tuple[str, dict[str, Any]] | None:
    raw_fixture = os.environ.get("MOVEOUT_THM_TEST_MATCH")
    if not raw_fixture:
        return None

    try:
        fixture = json.loads(raw_fixture)
    except json.JSONDecodeError as error:
        raise ValueError(
            "MOVEOUT_THM_TEST_MATCH must contain a JSON object."
        ) from error
    if not isinstance(fixture, dict):
        raise ValueError("MOVEOUT_THM_TEST_MATCH must contain a JSON object.")

    test_sender_email = fixture.get("test_sender_email")
    if not isinstance(test_sender_email, str) or not test_sender_email.strip():
        raise ValueError(
            "MOVEOUT_THM_TEST_MATCH must include a non-empty "
            "'test_sender_email'."
        )

    result = {
        key: value
        for key, value in fixture.items()
        if key != "test_sender_email"
    }
    result["thm_status"] = "matched"
    return test_sender_email.strip().casefold(), result


def _valid_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("THM_BASE_URL must be an absolute HTTP(S) URL.")
    return value


def _normalize(value: Any) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _home_name(home: dict[str, Any]) -> str:
    return str(home.get("displayName") or home.get("name") or "")


def _flat_matches(home: dict[str, Any], flat: str) -> bool:
    normalized_flat = _normalize(flat)
    return bool(normalized_flat) and any(
        normalized_flat in _normalize(home.get(field))
        for field in ("displayName", "name")
    )


def _society_matches(home: dict[str, Any], society: str) -> bool:
    normalized_society = _normalize(society)
    return bool(normalized_society) and any(
        normalized_society in _normalize(home.get(field))
        for field in ("displayName", "name")
    )


def _select_property(
    homes: list[dict[str, Any]],
    society: str,
    flat: str,
) -> dict[str, Any] | None:
    for home in homes:
        if _flat_matches(home, flat) and _society_matches(home, society):
            return home
    return None


def _rows(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict):
        raise ValueError("THM home-list response must be a JSON object.")
    values = payload.get("rows", payload.get("data", []))
    if not isinstance(values, list):
        raise ValueError("THM home-list response rows must be a JSON array.")
    homes = [value for value in values if isinstance(value, dict)]
    count = payload.get("count")
    return homes, count if isinstance(count, int) else None


def _query_homes(
    client: httpx.Client,
    token: str,
    query: str,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int | None]:
    homes: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, max_pages + 1):
        response = client.get(
            "/api/home",
            params=[
                ("limit", "100"),
                ("page", str(page)),
                *(("fields", field) for field in THM_HOME_FIELDS),
                ("q", query),
            ],
            headers={"Authorization": token},
        )
        response.raise_for_status()
        page_homes, total = _rows(response.json())
        if not page_homes:
            break
        homes.extend(page_homes)
        if total is not None and page * 100 >= total:
            break
    return homes, total


def _find_home(
    client: httpx.Client,
    token: str,
    society: str,
    flat: str,
    tenant_email: str | None,
) -> dict[str, Any] | None:
    email_lookup_enabled = (
        os.environ.get("MOVEOUT_THM_EMAIL_LOOKUP", "true").strip().casefold()
        in {"1", "true", "yes", "on"}
    )
    if not email_lookup_enabled:
        print(
            "[thm] email lookup disabled "
            "(MOVEOUT_THM_EMAIL_LOOKUP=false) — using society+flat only"
        )

    if (
        email_lookup_enabled
        and isinstance(tenant_email, str)
        and "@" in tenant_email
    ):
        email_homes, total = _query_homes(
            client,
            token,
            tenant_email.strip(),
            max_pages=3,
        )
        if total is None or total <= 10:
            match = _select_property(email_homes, society, flat)
            if match is not None:
                return match
            if len(email_homes) == 1 and _flat_matches(email_homes[0], flat):
                return email_homes[0]

    flat_queries = dict.fromkeys(
        (
            flat.strip(),
            re.sub(r"\s+", "", flat),
            "".join(re.findall(r"\d+", flat)),
        )
    )
    for query in filter(None, flat_queries):
        homes, _ = _query_homes(client, token, query, max_pages=10)
        match = _select_property(homes, society, flat)
        if match is not None:
            return match

    all_homes, _ = _query_homes(client, token, "", max_pages=THM_MAX_PAGES)
    return _select_property(all_homes, society, flat)


def _resource(client: httpx.Client, path: str, token: str) -> dict[str, Any]:
    response = client.get(path, headers={"Authorization": token})
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError(f"THM response from {path} must be a JSON object.")
    return value


def _resource_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        identifier = value.get("_id") or value.get("id")
        return identifier if isinstance(identifier, str) and identifier else None
    return None


def _user_data(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    return value


def _map_thm_home(
    client: httpx.Client,
    token: str,
    home_summary: dict[str, Any],
    society: str,
    flat: str,
) -> dict[str, Any]:
    home_id = _resource_id(home_summary.get("_id") or home_summary.get("home_id"))
    if not home_id:
        raise ValueError(f"THM home result has no ID for property {society} {flat}.")

    home_response = _resource(client, f"/api/home/{home_id}", token)
    home = home_response.get("home")
    if not isinstance(home, dict):
        home = home_response

    result: dict[str, Any] = {
        "thm_home_id": home_id,
        "society": society,
        "flat": flat,
    }

    terms = home.get("tenancyTerms")
    if isinstance(terms, dict):
        result["monthlyRent"] = terms.get("rentFromTenant")
        result["securityDeposit"] = next(
            (terms[field] for field in _SECURITY_DEPOSIT_FIELDS if terms.get(field)),
            None,
        )
        expiry = terms.get("contractPeriodEndDate")
        result["leaseExpiry"] = str(expiry)[:10] if expiry else None

    owners = home.get("owners") or home_summary.get("owners") or []
    if owners:
        owner_id = _resource_id(owners[0])
        if owner_id:
            owner_response = _resource(client, f"/api/user/{owner_id}", token)
            owner = _user_data(owner_response, "user", "owner")
            result["ownerName"] = owner.get("fullName") or " ".join(
                filter(None, (owner.get("firstName"), owner.get("lastName")))
            )
            result["ownerEmail"] = owner.get("email")
            result["ownerPhone"] = owner.get("phoneNumber")

    tenant_objects = home.get("tenantObjs") or home_summary.get("tenantObjs") or []
    tenant = next(
        (
            value
            for value in tenant_objects
            if isinstance(value, dict)
            and any(
                value.get(field)
                for field in ("email", "fullName", "firstName", "lastName")
            )
        ),
        None,
    )
    if tenant is None:
        tenants = home.get("tenants") or home_summary.get("tenants") or []
        for tenant_value in tenants:
            tenant_id = _resource_id(tenant_value)
            if not tenant_id:
                continue
            tenant_response = _resource(
                client,
                f"/api/user/{tenant_id}",
                token,
            )
            tenant = _user_data(tenant_response, "user", "tenant")
            if any(
                tenant.get(field)
                for field in ("email", "fullName", "firstName", "lastName")
            ):
                break
        else:
            tenant = {}

    if tenant:
        result["tenantName"] = tenant.get("fullName") or " ".join(
            filter(None, (tenant.get("firstName"), tenant.get("lastName")))
        )
        result["tenantEmail"] = tenant.get("email")
        result["tenantPhone"] = tenant.get("phoneNumber")

    listing_value = home.get("listing") or home_summary.get("listing")
    listing_id = _resource_id(listing_value)
    if listing_id:
        result["thm_listing_id"] = listing_id
        listing_response = _resource(client, f"/api/listing/{listing_id}", token)
        listing = listing_response.get("listing", listing_response)
    else:
        listing = {}

    project_value = home.get("project") or home_summary.get("project")
    project_id = _resource_id(project_value)
    if not project_id and isinstance(listing, dict):
        project_id = _resource_id(listing.get("project"))
    if project_id:
        result["thm_project_id"] = project_id
        project_response = _resource(
            client,
            f"/api/project/{project_id}",
            token,
        )
        project = project_response.get("project", project_response)
        custom_fields = project.get("customFields", [])
        if isinstance(custom_fields, list):
            email_values = []
            for field in custom_fields:
                if not isinstance(field, dict) or not re.search(
                    r"e-?mail",
                    str(field.get("fieldLabel") or ""),
                    re.I,
                ):
                    continue
                raw_emails = field.get("fieldValue") or field.get("feildValue") or ""
                email_values.extend(
                    re.findall(r"[\w.+-]+@[\w.-]+\.\w+", str(raw_emails))
                )
            result["associationEmail"] = "; ".join(dict.fromkeys(email_values))

        company_info = project.get("companyInfo")
        if isinstance(company_info, dict):
            result["associationName"] = company_info.get("companyName") or ""
            country_code = str(company_info.get("companyCountryCode") or "").strip()
            phone_number = str(company_info.get("companyPhoneNumber") or "").strip()
            result["associationPhone"] = (
                f"{country_code} {phone_number}".strip() if phone_number else ""
            )

    sharing_type = home.get("sharingType") or home_summary.get("sharingType")
    category_id = _resource_id(sharing_type)
    if category_id:
        category_response = _resource(client, f"/api/category/{category_id}", token)
        category = category_response.get("category", category_response)
        category_name = category.get("name")
    else:
        category_name = None
    result["thm_category"] = category_name
    result["propertyType"] = (
        "NonPM"
        if isinstance(category_name, str) and re.search(r"non\s*pm", category_name, re.I)
        else "PM"
        if isinstance(category_name, str) and category_name.casefold() == "pm"
        else None
    )

    display = _home_name(home) or f"{society} {flat}"
    category_label = category_name or "category?"
    result["thm_status"] = (
        f"Matched THM home {display} ({category_label})."
    )

    for field in _OPTIONAL_RESULT_FIELDS:
        if result.get(field) is None:
            print(
                f"[thm] WARNING: null field '{field}' for property "
                f"{society} {flat} — downstream may be incomplete"
            )
    return result


def thm_lookup(
    society: str | None,
    flat: str | None,
    tenant_email: str | None = None,
) -> dict[str, Any]:
    """Log in to THM, find the property, and fetch its related details."""
    test_match = _thm_test_match_fixture()
    if (
        test_match is not None
        and isinstance(tenant_email, str)
        and tenant_email.strip().casefold() == test_match[0]
    ):
        fixture = test_match[1]
        print(
            "[thm] test-mode match: "
            f"{tenant_email.strip()} -> "
            f"{fixture.get('thm_home_id', '<missing-home-id>')}"
        )
        return fixture

    if not isinstance(society, str) or not society.strip():
        return _not_found()
    if not isinstance(flat, str) or not flat.strip():
        return _not_found()

    phone = os.environ.get("THM_PHONE", "").strip()
    password = os.environ.get("THM_PASSWORD", "").strip()
    country_code = os.environ.get("THM_COUNTRY_CODE", "+91").strip()
    if not phone or not password:
        raise RuntimeError(
            "THM credentials are not configured. Set THM_PHONE and THM_PASSWORD."
        )

    with httpx.Client(
        base_url=_valid_base_url(THM_BASE_URL),
        timeout=THM_TIMEOUT_SECONDS,
    ) as client:
        login_response = client.post(
            "/api/login",
            json={
                "countryCode": country_code,
                "phoneNumber": phone,
                "password": password,
            },
        )
        login_response.raise_for_status()
        login = login_response.json()
        token = login.get("token") if isinstance(login, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("THM login response did not contain a token.")

        match = _find_home(
            client,
            token,
            society.strip(),
            flat.strip(),
            tenant_email,
        )
        if match is None:
            return _not_found()
        return _map_thm_home(
            client,
            token,
            match,
            society.strip(),
            flat.strip(),
        )


def process_incoming_email(email: dict[str, Any]) -> dict[str, Any]:
    """Parse an email, look up its THM home, and route it."""
    parsed_item = parse_move_out_email(email)

    if not parsed_item:
        return {"route": "ignored", "reason": "Not a move-out notice"}

    thm_data = thm_lookup(
        parsed_item["society"],
        parsed_item["flat"],
        parsed_item["tenantEmail"],
    )
    canonical_item = {**parsed_item, **thm_data}

    if canonical_item.get("thm_home_id") and canonical_item.get("parseStatus") == "success":
        return {
            "route": "automation_engine",
            "data": canonical_item,
            "message": "Valid match. Proceeding to create tickets and checklists.",
        }

    return {
        "route": "manual_review",
        "data": canonical_item,
        "message": "Could not fully match property. Sending to staff for review.",
    }
