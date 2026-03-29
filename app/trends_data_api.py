import configparser
import json
import re
from pathlib import Path

import requests


config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

GET_TRENDS_DATA_API = config["api"].get("gettrendsdataapi", "").strip()
REQUEST_TIMEOUT_SECONDS = int(config["api"].get("gettrendsdataapi_timeout", "30") or "30")


class TrendsDataError(Exception):
    pass


PRIORITY_MARKER_PATTERNS = [
    re.compile(r"\bapo[\s-]?a1\b", re.IGNORECASE),
    re.compile(r"\bapo[\s-]?b\b", re.IGNORECASE),
    re.compile(r"\bapolipoprotein\b", re.IGNORECASE),
    re.compile(r"\binsulin\s*resistance\b", re.IGNORECASE),
    re.compile(r"\bhoma\b", re.IGNORECASE),
    re.compile(r"\bcortisol\b", re.IGNORECASE),
    re.compile(r"\bhomocysteine\b", re.IGNORECASE),
    re.compile(r"\bnt[\s-]?pro[\s-]?bnp\b", re.IGNORECASE),
    re.compile(r"\bpro[\s-]?bnp\b", re.IGNORECASE),
]


def _call_tapi_query(api_url, payload):
    if not api_url:
        raise TrendsDataError("gettrendsdataapi is not configured in config.ini [api]")

    encoded_payload = json.dumps([payload])

    try:
        response = requests.get(
            api_url,
            params={"data": encoded_payload},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise TrendsDataError(f"Trend data API request failed: {exc}") from exc

    if response.status_code != 200:
        raise TrendsDataError(
            f"Trend data API failed with status {response.status_code}: {response.text[:400]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise TrendsDataError(
            f"Trend data API returned invalid JSON: {response.text[:400]}"
        ) from exc


def _extract_rows(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("table"), dict):
            rows = payload["table"].get("rows")
            if isinstance(rows, list):
                return rows
        if isinstance(payload.get("data"), list):
            return payload["data"]
    if isinstance(payload, list):
        return payload
    return []


def _to_number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return n


def _row_dicts(payload):
    rows = []
    if not isinstance(payload, dict):
        return rows

    table = payload.get("table")
    if not isinstance(table, dict):
        return rows

    columns = table.get("columns") or []
    data_rows = table.get("rows") or []
    if not isinstance(columns, list) or not isinstance(data_rows, list):
        return rows

    keys = []
    for col in columns:
        if isinstance(col, dict):
            keys.append(str(col.get("name") or "").strip())
        else:
            keys.append(str(col or "").strip())

    for row in data_rows:
        values = row.get("values") if isinstance(row, dict) else None
        if not isinstance(values, list):
            continue
        item = {}
        for idx, key in enumerate(keys):
            if not key:
                continue
            item[key] = values[idx] if idx < len(values) else None
        if item:
            rows.append(item)
    return rows


def _psyntax_quality(psyntax, lettype, mode="neutral"):
    if str(mode or "neutral").strip().lower() != "sdrc_v1":
        return "neutral"
    p = str(psyntax or "").strip()
    l = str(lettype or "").strip().upper()
    if not p or p == "0" or not l:
        return "neutral"
    if "1" in p:
        if "H" in l:
            return "bad"
        if "L" in l:
            return "good"
        return "neutral"
    if "L" in l:
        return "bad"
    if "H" in l:
        return "good"
    return "neutral"


def _is_priority_marker(name):
    text = str(name or "").strip()
    if not text:
        return False
    return any(rx.search(text) for rx in PRIORITY_MARKER_PATTERNS)


def _standardize(payload, mrno, psyntax_mode="neutral"):
    rows = _row_dicts(payload)
    if not rows:
        return {
            "schema_version": "trend.v1",
            "mrno": mrno,
            "row_count": 0,
            "parameters": [],
        }

    grouped = {}
    all_dates = []

    for row in rows:
        compid = str(row.get("COMPID") or row.get("compid") or "").strip()
        name = str(row.get("TESTCOMPONENT") or row.get("testcomponent") or compid).strip()
        unit = str(row.get("UNITS") or row.get("units") or "").strip() or None
        reqdt = str(row.get("REQDT") or row.get("reqdt") or "").strip() or None
        value = _to_number(row.get("RESULTVALUE") or row.get("resultvalue"))
        if reqdt:
            all_dates.append(reqdt)

        key = f"{compid}|{name}|{unit or ''}"
        group = grouped.setdefault(
            key,
            {
                "component_id": compid or None,
                "key": compid or name.lower().replace(" ", "_"),
                "name": name or compid,
                "unit": unit,
                "priority_marker": _is_priority_marker(name),
                "values": [],
            },
        )

        group["values"].append(
            {
                "date": reqdt,
                "value": value,
                "ref_low": _to_number(row.get("MINVAL") or row.get("minval")),
                "ref_high": _to_number(row.get("MAXVAL") or row.get("maxval")),
                "lettype": (row.get("LETTYPE") or row.get("lettype")),
                "psyntax": (row.get("PSYNTAX") or row.get("psyntax")),
                "quality": _psyntax_quality(
                    row.get("PSYNTAX") or row.get("psyntax"),
                    row.get("LETTYPE") or row.get("lettype"),
                    psyntax_mode,
                ),
            }
        )

    parameters = []
    for item in grouped.values():
        item["values"].sort(key=lambda x: str(x.get("date") or ""))
        parameters.append(item)

    parameters.sort(key=lambda x: (x.get("name") or "").lower())
    all_dates = sorted([d for d in all_dates if d])

    first_date = all_dates[0] if all_dates else None
    last_date = all_dates[-1] if all_dates else None

    first = rows[0] if rows else {}
    patient = {
        "name": first.get("PATIENTNM") or first.get("patientnm"),
        "age": first.get("AGE") or first.get("age"),
        "gender": first.get("SEX") or first.get("sex"),
        "mobile": first.get("MOBILENO") or first.get("mobileno"),
    }

    return {
        "schema_version": "trend.v1",
        "mrno": mrno,
        "psyntax_mode": psyntax_mode,
        "row_count": len(rows),
        "timeline": {
            "first_test_date": first_date,
            "last_test_date": last_date,
        },
        "patient": patient,
        "parameters": parameters,
    }


def fetch_trends_data(mrno, standardized=True, psyntax_mode="neutral"):
    clean_mrno = str(mrno or "").strip()
    if not clean_mrno:
        raise TrendsDataError("mrno is required")

    # Try payload keys in order to remain compatible with different TApiQuery bindings.
    attempts = [
        {"s": clean_mrno},
        {"mrno": clean_mrno},
        {"MRNO": clean_mrno},
    ]

    last_payload = None
    last_result = None

    for payload in attempts:
        last_payload = payload
        result = _call_tapi_query(GET_TRENDS_DATA_API, payload)
        rows = _extract_rows(result)
        if rows:
            response = {
                "mrno": clean_mrno,
                "source": "gettrendsdataapi",
                "payload_key": list(payload.keys())[0],
                "row_count": len(rows),
                "data": result,
            }
            if standardized:
                response["standardized"] = _standardize(result, clean_mrno, psyntax_mode=psyntax_mode)
            return response
        last_result = result

    response = {
        "mrno": clean_mrno,
        "source": "gettrendsdataapi",
        "payload_key": list(last_payload.keys())[0] if last_payload else None,
        "row_count": 0,
        "data": last_result,
    }
    if standardized:
        response["standardized"] = _standardize(last_result or {}, clean_mrno, psyntax_mode=psyntax_mode)
    return response
