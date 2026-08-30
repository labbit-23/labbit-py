"""labit-core-backed replacements for report_status.py's Oracle/TApiQuery
calls, built to the SAME output shape so main.py can swap which module it
calls with a one-line change, not a rewrite.

User, 2026-08-30: "OR we create a labit_tools on the side and switch from
shivam tools to labit tools?" Lighter than a full separate clone/repo (the
first idea discussed) -- no new deployment, no new PM2 process, no second
codebase to keep in sync. Test these two functions directly (import + call
against a real reqno) before ever wiring them into main.py's routes; this
module makes zero WhatsApp calls itself (report-status is a pure read, the
send lives entirely in delivery_engine.py/report_sender_worker.py
elsewhere), so there is no patient-facing risk to testing it against real
production reqnos right now.

Contract traced in DISPATCH_CUTOVER_READINESS.md (2026-08-19, verified live
against real data that night): labit-core's `GET /api/report-status/{reqno}`
and `GET /api/report-status-reqid/{reqid}` already return the FULLY
PROCESSED shape report_status.py's `_process_status_rows()` builds from
Oracle's raw rows -- reqno, reqid, overall_status, lab_total, lab_ready,
radiology_total, radiology_ready, patient_name, mrno, patient_phone,
phoneno, test_date, source_id, source_name, first_approved_at,
latest_approved_at, tests[] -- plus three EXTRA fields labit-core adds
(dispatch_allowed, dispatch_denial_code, dispatch_denial_reason, replacing
the old DO_NOT_SEND_SOURCE_IDS env-var deny-list with a real
referrer.confidential column). Extra keys are additive/harmless to any
caller reading individual fields, not diffing the whole dict shape.

Auth: unlike TApiQuery (no login, just a terminalid query param), labit-core
gates this on `require_service()` (HTTP Basic, the same `svc_dispatch_bot`
credential labit-deliver already uses for its own calls into labit-core).
Read from config.ini's `[cutover]` section (gitignored, same as every
other credential in this file -- `[login] password`, `[whatsapp]
whatsapp_api_key`), with an environment-variable override for convenience
-- never hardcoded/committed, same posture the readiness doc itself calls
for ("given to the user once, never in this repo").
"""

import configparser
import os
from pathlib import Path

import requests

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

LABIT_CORE_BASE_URL = os.environ.get(
    "LABIT_CORE_BASE_URL", config["cutover"].get("labit_core_base_url", "") if config.has_section("cutover") else ""
).rstrip("/")
LABIT_CORE_SERVICE_USERNAME = os.environ.get(
    "LABIT_CORE_SERVICE_USERNAME",
    config["cutover"].get("labit_core_service_username", "") if config.has_section("cutover") else "",
)
LABIT_CORE_SERVICE_PASSWORD = os.environ.get(
    "LABIT_CORE_SERVICE_PASSWORD",
    config["cutover"].get("labit_core_service_password", "") if config.has_section("cutover") else "",
)


def _auth():
    if not LABIT_CORE_SERVICE_USERNAME or not LABIT_CORE_SERVICE_PASSWORD:
        raise Exception(
            "LABIT_CORE_SERVICE_USERNAME and LABIT_CORE_SERVICE_PASSWORD "
            "must be set in the environment to call labit_tools -- never "
            "put these in config.ini/the repo."
        )
    return (LABIT_CORE_SERVICE_USERNAME, LABIT_CORE_SERVICE_PASSWORD)


class LabitCoreReportNotFound(Exception):
    """Raised ONLY for a clean 404 -- this reqno/reqid genuinely doesn't
    exist in labit-core (a legacy-only reqno). report_backend.py catches
    exactly this, and nothing else, to decide whether a Shivam fallback is
    warranted -- any other failure here (network, auth, a real 500) must
    propagate as a normal exception, not be mistaken for "not found"."""


def _get(path):
    if not LABIT_CORE_BASE_URL:
        raise Exception("LABIT_CORE_BASE_URL must be set in the environment to call labit_tools.")
    url = f"{LABIT_CORE_BASE_URL}{path}"
    try:
        r = requests.get(url, auth=_auth(), timeout=(3, 20))
    except requests.RequestException as exc:
        raise Exception(f"labit-core report status call failed: {exc}") from exc
    if r.status_code == 404:
        raise LabitCoreReportNotFound(f"labit-core report status: unknown requisition for {path}")
    if not r.ok:
        raise Exception(f"labit-core report status API failed: {r.status_code} {r.text[:500]}")
    return r.json()


def fetch_report_status(reqno):
    """Drop-in replacement for report_status.fetch_report_status(reqno) --
    same output shape (see module docstring), different backend."""
    return _get(f"/api/report-status/{reqno}")


def fetch_report_status_by_reqid(reqid):
    """Drop-in replacement for report_status.fetch_report_status_by_reqid(reqid)."""
    return _get(f"/api/report-status-reqid/{reqid}")
