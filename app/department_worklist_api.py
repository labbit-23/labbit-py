import configparser
import json
from datetime import date
from pathlib import Path

import requests


config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

GET_DEPARTMENT_LIST_API = config["api"].get("getdepartmentlistapi", "").strip()
DEPARTMENT_REGISTRY = {
    str(key or "").strip().lower(): str(value or "").strip().upper()
    for key, value in config.items("departments")
} if config.has_section("departments") else {}


def _today_iso():
    return date.today().isoformat()


def _clean(value):
    return str(value or "").strip()


def _normalize_date_or_today(value):
    text = _clean(value)
    return text or _today_iso()


def _call_tapi_query(api_url, payload):
    if not api_url:
        raise Exception("getdepartmentlistapi is not configured in config.ini [api]")

    encoded_payload = json.dumps([payload])

    try:
        response = requests.get(
            api_url,
            params={"data": encoded_payload},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise Exception(
            f"HTTP request failed for {api_url}: {exc}. "
            f"Payload={encoded_payload}"
        ) from exc

    if response.status_code != 200:
        raise Exception(
            f"TApiQuery failed for {api_url} with status {response.status_code}. "
            f"Payload={encoded_payload} Body={response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise Exception(
            f"Invalid JSON from {api_url}. "
            f"Payload={encoded_payload} Body={response.text[:500]}"
        ) from exc


def _unwrap_rows(data):
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _row_value(row, *keys):
    if not isinstance(row, dict):
        return ""
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _normalize_department_id(value):
    return _clean(value).upper()


def _normalize_department_name(value):
    return _clean(value).lower()


def _is_dept_id(text):
    t = _normalize_department_id(text)
    return t.startswith("DPT") or t.startswith("DEP")


def _resolve_department_id(department=None, department_name=None):
    dep = _clean(department)
    dep_name = _clean(department_name)

    # ID passed directly.
    if dep and _is_dept_id(dep):
        return _normalize_department_id(dep), None

    # Alias in `department` (e.g. radiology) or in `department_name`.
    alias = _normalize_department_name(dep or dep_name)
    if alias:
        resolved = DEPARTMENT_REGISTRY.get(alias)
        if resolved:
            return resolved, alias

    # No name fallback to upstream to keep behavior deterministic.
    return "", alias or None


def _to_item(row):
    return {
        "accession_no": _row_value(row, "ACCESSION_NO", "accession_no", "REQNO", "reqno") or None,
        "reqno": _row_value(row, "REQNO", "reqno", "ACCESSION_NO", "accession_no") or None,
        "reqid": _row_value(row, "REQID", "reqid") or None,
        "patient_id": _row_value(row, "PATIENT_ID", "patient_id", "MRNO", "mrno") or None,
        "patient_name": _row_value(row, "PATIENT_NAME", "patient_name", "PATIENTNM", "patientnm") or None,
        "mobileno": _row_value(row, "MOBILENO", "mobileno", "PHONENO", "phoneno") or None,
        "patient_sex": _row_value(row, "PATIENT_SEX", "patient_sex", "SEX", "sex") or None,
        "patient_dob": _row_value(row, "PATIENT_DOB", "patient_dob", "DOB", "dob") or None,
        "reqdt": _row_value(row, "REQDT", "reqdt") or None,
        "reqtm": _row_value(row, "REQTM", "reqtm") or None,
        "testid": _row_value(row, "TESTID", "testid") or None,
        "tcode": _row_value(row, "TCODE", "tcode") or None,
        "procedure_name": _row_value(row, "PROCEDURE_NAME", "procedure_name", "TESTNM", "testnm") or None,
        "deptid": _row_value(row, "DEPTID", "deptid") or None,
        "department_name": _row_value(row, "DEPARTMENT_NAME", "department_name", "DEPTNM", "deptnm") or None,
        "groupid": _row_value(row, "GROUPID", "groupid") or None,
        "groupnm": _row_value(row, "GROUPNM", "groupnm") or None,
        "cancelled_flg": _row_value(row, "CANCELLED_FLG", "cancelled_flg", "CANCELLED", "cancelled") or None,
        "approved_flg": _row_value(row, "APPROVED_FLG", "approved_flg", "APPROVEDFLG", "approvedflg") or None,
        "performed": _row_value(row, "PERFORMED", "performed") or None,
        "raw": row,
    }


def fetch_department_worklist(fromreqdate=None, toreqdate=None, department=None, department_name=None):
    clean_from = _normalize_date_or_today(fromreqdate)
    clean_to = _normalize_date_or_today(toreqdate)

    resolved_department_id, resolved_alias = _resolve_department_id(
        department=department,
        department_name=department_name,
    )

    if not resolved_department_id:
        raise Exception(
            "department DEPTID is required. "
            "Pass a valid DEPTID (DPT.../DEP...) or configure alias under [departments]."
        )

    payload = {
        "fromreqdate": clean_from,
        "toreqdate": clean_to,
        "department": resolved_department_id,
    }

    rows = _unwrap_rows(_call_tapi_query(GET_DEPARTMENT_LIST_API, payload))
    if not isinstance(rows, list):
        raise Exception(f"Unexpected department list response: {rows}")

    items = [_to_item(row) for row in rows]

    return {
        "fromreqdate": clean_from,
        "toreqdate": clean_to,
        "department": resolved_department_id,
        "department_alias": resolved_alias,
        "count": len(items),
        "items": items,
    }
