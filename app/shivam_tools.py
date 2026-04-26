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


def _parse_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _normalize_sex_to_ui(value):
    sex_num = _parse_int(value)
    if sex_num is None:
        return _row_value({"v": value}, "v")
    if sex_num == 1:
        return "Male"
    if sex_num == 0:
        return "Female"
    return str(sex_num)


def _normalize_sex_for_shivam(payload):
    raw_sex = payload.get("sex")
    if raw_sex is not None and str(raw_sex).strip() != "":
        sex_num = _parse_int(raw_sex)
        if sex_num is not None:
            return sex_num

    raw_gender = str(payload.get("gender") or "").strip().lower()
    if raw_gender in {"male", "m", "1"}:
        return 1
    if raw_gender in {"female", "f", "0"}:
        return 0
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
        "patient_name": _row_value(
            row,
            "PATIENTNM",
            "patientnm",
            "PATIENT_NAME",
            "patient_name",
            "NAME",
            "name",
            "FNAME",
            "fname",
        ),
        "mobile_no": _row_value(
            row,
            "PHONENO",
            "phoneno",
            "MOBILENO",
            "mobileno",
            "PHONE",
            "phone",
            "PHONE2",
            "phone2",
        ),
        "age": _row_value(row, "AGE", "age"),
        "dob": _row_value(
            row,
            "DOB",
            "dob",
            "DATEOFBIRTH",
            "dateofbirth",
            "DATE_OF_BIRTH",
            "date_of_birth",
        ),
        "gender": _normalize_sex_to_ui(_row_value(row, "SEX", "sex", "GENDER", "gender")),
        "sex": _parse_int(_row_value(row, "SEX", "sex")),
        "email": _row_value(row, "EMAIL", "email"),
        "pincode": _row_value(row, "PINCODE", "pincode"),
        "ageyrs": _parse_int(_row_value(row, "AGEYRS", "ageyrs")),
        "agemonths": _parse_int(_row_value(row, "AGEMONTHS", "agemonths")),
        "agedays": _parse_int(_row_value(row, "AGEDAYS", "agedays")),
        "raw": row,
    }


def update_demographics(payload):
    if not isinstance(payload, dict):
        raise Exception("payload must be an object")

    mrno = str(payload.get("mrno") or "").strip()
    if not mrno:
        raise Exception("mrno is required")

    allowed_keys = [
        "mrno",
        "patient_name",
        "mobile_no",
        "age",
        "dob",
        "gender",
        "sex",
        "email",
        "pincode",
        "ageyrs",
        "agemonths",
        "agedays",
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

    # Shivam WF compatibility aliases (safe to include alongside canonical keys).
    if "mrno" in clean_payload and "CREGNO" not in clean_payload:
        clean_payload["CREGNO"] = clean_payload["mrno"]
    if "patient_name" in clean_payload and "FNAME" not in clean_payload:
        clean_payload["FNAME"] = clean_payload["patient_name"]
    if "mobile_no" in clean_payload and "PHONE2" not in clean_payload:
        clean_payload["PHONE2"] = clean_payload["mobile_no"]
    normalized_sex = _normalize_sex_for_shivam(clean_payload)
    if normalized_sex is not None:
        clean_payload["SEX"] = normalized_sex
    if "dob" in clean_payload and "DOB" not in clean_payload:
        clean_payload["DOB"] = clean_payload["dob"]
    if "age" in clean_payload and "AGE" not in clean_payload:
        clean_payload["AGE"] = clean_payload["age"]
    if "email" in clean_payload and "EMAIL" not in clean_payload:
        clean_payload["EMAIL"] = clean_payload["email"]
    if "pincode" in clean_payload and "PINCODE" not in clean_payload:
        clean_payload["PINCODE"] = clean_payload["pincode"]
    if "ageyrs" in clean_payload and "AGEYRS" not in clean_payload:
        clean_payload["AGEYRS"] = clean_payload["ageyrs"]
    if "agemonths" in clean_payload and "AGEMONTHS" not in clean_payload:
        clean_payload["AGEMONTHS"] = clean_payload["agemonths"]
    if "agedays" in clean_payload and "AGEDAYS" not in clean_payload:
        clean_payload["AGEDAYS"] = clean_payload["agedays"]

    if not any(
        key in clean_payload
        for key in [
            "patient_name",
            "mobile_no",
            "age",
            "dob",
            "gender",
            "sex",
            "email",
            "pincode",
            "ageyrs",
            "agemonths",
            "agedays",
        ]
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
