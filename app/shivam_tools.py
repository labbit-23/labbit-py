import configparser
import json
from pathlib import Path

import requests

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

GET_DEMOGRAPHICS_BY_MRNO_API = config["api"].get("getdemographicsbymrnoapi", "").strip()
UPDATE_DEMOGRAPHICS_API = config["api"].get("updatedemographicsapi", "").strip()
GET_PRICELIST_API = config["api"].get("getpricelistapi", "").strip()


def _call_tapi_query(api_url, payload):
    if not api_url:
        raise Exception("Shivam tools API is not configured in config.ini [api]")

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


def _first(rows):
    if isinstance(rows, list) and rows:
        return rows[0]
    if isinstance(rows, dict):
        return rows
    return {}


def _row_value(row, *keys):
    if not isinstance(row, dict):
        return None

    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def fetch_demographics_by_mrno(mrno):
    clean_mrno = str(mrno or "").strip()
    if not clean_mrno:
        raise Exception("mrno is required")

    payload = {"mrno": clean_mrno}
    rows = _unwrap_rows(_call_tapi_query(GET_DEMOGRAPHICS_BY_MRNO_API, payload))
    row = _first(rows)

    return {
        "mrno": _row_value(row, "MRNO", "mrno", "CREGNO", "cregno") or clean_mrno,
        "reqno": _row_value(row, "REQNO", "reqno"),
        "reqid": _row_value(row, "REQID", "reqid"),
        "patient_name": _row_value(row, "PATIENTNM", "patientnm", "PATIENT_NAME", "patient_name", "NAME", "name"),
        "mobile_no": _row_value(row, "PHONENO", "phoneno", "MOBILENO", "mobileno", "PHONE", "phone"),
        "age": _row_value(row, "AGE", "age"),
        "dob": _row_value(row, "DOB", "dob", "DATEOFBIRTH", "dateofbirth", "DATE_OF_BIRTH", "date_of_birth"),
        "gender": _row_value(row, "SEX", "sex", "GENDER", "gender"),
        "raw": row,
    }


def update_demographics(payload):
    if not isinstance(payload, dict):
        raise Exception("payload must be an object")

    identifiers = [
        str(payload.get("reqno") or "").strip(),
        str(payload.get("reqid") or "").strip(),
        str(payload.get("mrno") or "").strip(),
    ]
    if not any(identifiers):
        raise Exception("at least one identifier is required (reqno/reqid/mrno)")

    allowed_keys = [
        "reqno",
        "reqid",
        "mrno",
        "patient_name",
        "mobile_no",
        "age",
        "dob",
        "gender",
    ]
    clean_payload = {}
    for key in allowed_keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text == "":
            continue
        clean_payload[key] = value

    if not any(
        key in clean_payload for key in ["patient_name", "mobile_no", "age", "dob", "gender"]
    ):
        raise Exception("no update fields provided")

    rows = _unwrap_rows(_call_tapi_query(UPDATE_DEMOGRAPHICS_API, clean_payload))
    return {
        "ok": True,
        "payload": clean_payload,
        "result": rows,
    }


def fetch_pricelist(lab_id=None):
    payload = {}
    if lab_id is not None and str(lab_id).strip():
        payload["lab_id"] = str(lab_id).strip()

    rows = _unwrap_rows(_call_tapi_query(GET_PRICELIST_API, payload))
    if not isinstance(rows, list):
        raise Exception(f"Unexpected pricelist response: {rows}")

    return {
        "tests": rows
    }

