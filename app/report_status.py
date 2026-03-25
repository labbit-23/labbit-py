import requests
import json
import configparser
from pathlib import Path

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

STATUS_API = config["api"]["reportstatusapi"]
REQID_STATUS_API = config["api"]["reportstatusreqidapi"]


def row_value(row, *keys):

    if not isinstance(row, dict):
        return None

    lowered = {str(k).lower(): v for k, v in row.items()}

    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value

    return None


# -----------------------------
# NEW: Common processor (NON-BREAKING)
# -----------------------------
def _process_status_rows(rows, identifier):

    if not isinstance(rows, list):
        raise Exception(f"Unexpected report status response: {rows}")

    lab_total = 0
    lab_ready = 0
    radiology_total = 0
    radiology_ready = 0

    for row in rows:

        group_id = row_value(row, "GROUPID", "groupid")
        report_status = row_value(row, "REPORT_STATUS", "report_status")
        approved_flag = row_value(row, "APPROVEDFLG", "approvedflg")

        if group_id == "GDEP0001":
            lab_total += 1

            if report_status == "LAB_READY" or str(approved_flag) == "1":
                lab_ready += 1

        elif group_id == "GDEP0002":
            radiology_total += 1

            if report_status == "RADIOLOGY_READY" or str(approved_flag) == "1":
                radiology_ready += 1

    if lab_total == 0:
        overall = "NO_LAB_TESTS"
    elif lab_ready == lab_total:
        overall = "FULL_REPORT"
    elif lab_ready > 0:
        overall = "PARTIAL_REPORT"
    else:
        overall = "NO_REPORT"

    return {
        # keep original key for compatibility
        "reqno": identifier,
        "overall_status": overall,
        "lab_total": lab_total,
        "lab_ready": lab_ready,
        "radiology_total": radiology_total,
        "radiology_ready": radiology_ready,
        "tests": rows
    }


# -----------------------------
# Existing REQNO API (UNCHANGED LOGIC)
# -----------------------------
def fetch_report_status(reqno):

    payload = json.dumps([
        {
            "reqno": reqno
        }
    ])

    url = f"{STATUS_API}&data={payload}"

    r = requests.get(url)

    if r.status_code != 200:
        raise Exception("Report status API failed")

    rows = r.json()

    # ONLY CHANGE → reuse helper
    return _process_status_rows(rows, reqno)


# -----------------------------
# REQID API (NOW SAME OUTPUT)
# -----------------------------
def fetch_report_status_by_reqid(reqid):

    payload = json.dumps([
        {
            "reqid": reqid
        }
    ])

    # safer URL handling
    separator = "&" if "?" in REQID_STATUS_API else "?"
    url = f"{REQID_STATUS_API}{separator}data={payload}"

    r = requests.get(url, timeout=20)

    if not r.ok:
        raise Exception(f"Status API failed: {r.status_code}")

    rows = r.json()

    # SAME OUTPUT STRUCTURE
    return _process_status_rows(rows, reqid)
