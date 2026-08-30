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


def _get(path, params=None):
    if not LABIT_CORE_BASE_URL:
        raise Exception("LABIT_CORE_BASE_URL must be set in the environment to call labit_tools.")
    url = f"{LABIT_CORE_BASE_URL}{path}"
    try:
        r = requests.get(url, params=params, auth=_auth(), timeout=(3, 20))
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


def fetch_outsourced_attachment(reqno, test_code):
    """Calls labit-core's new GET /api/dispatch-status/{reqno}/outsourced-attachment
    (built 2026-08-30, first content-serving endpoint for outsourced
    results -- previously nothing proxied outsourced content through
    labit-core at all). Returns (content_bytes, content_type)."""
    if not LABIT_CORE_BASE_URL:
        raise Exception("LABIT_CORE_BASE_URL must be set in the environment to call labit_tools.")
    url = f"{LABIT_CORE_BASE_URL}/api/dispatch-status/{reqno}/outsourced-attachment"
    try:
        r = requests.get(url, params={"test_code": test_code}, auth=_auth(), timeout=(3, 30))
    except requests.RequestException as exc:
        raise Exception(f"labit-core outsourced-attachment call failed: {exc}") from exc
    if r.status_code == 404:
        raise LabitCoreReportNotFound(f"labit-core outsourced-attachment: none found for {reqno}/{test_code}")
    if not r.ok:
        raise Exception(f"labit-core outsourced-attachment API failed: {r.status_code} {r.text[:500]}")
    return r.content, r.headers.get("content-type", "application/pdf")


def fetch_requisitions_by_date(date, org_id=None):
    """Drop-in replacement for delivery_api.fetch_requisitions_by_date --
    labit-core's GET /api/dispatch-status/by-date/{day} (built 2026-08-30,
    same session) was traced field-for-field against this exact contract
    (reqno, reqid, patient_name, phoneno, mrno, org_id, org_name), so no
    reshaping is needed. Sweep item 2026-08-30: the staff Report Dispatch
    page's date search, not bot/sender-critical but real, active daily use."""
    params = {"org_id": org_id} if org_id else None
    return _get(f"/api/dispatch-status/by-date/{date}", params=params)


def fetch_lookup(phone):
    """Drop-in replacement for req_lookup.fetch_reqids(phone) -- labit-core's
    /api/dispatch-lookup/{phone} already merges labit_core + shivam_archive
    rows (2026-08-30: closed the one gap left after status/PDF/trend all
    got the same treatment -- /lookup/{phone} is the bot's primary "find my
    reports" entry point). Returns the raw {"phone", "latest_reports", ...}
    payload; report_backend.fetch_lookup adapts the shape for main.py."""
    return _get(f"/api/dispatch-lookup/{phone}")


def fetch_dispatch_pdf(reqno, scope="all", testids=None, include_trends=False):
    """Drop-in replacement for report_fetcher.get_report()-family calls --
    labit-core's /api/dispatch-status/{reqno}/pdf now black-boxes core vs
    archive rendering itself (2026-08-30: "Make labit-core handle the
    black-boxing?"), so this needs no separate Shivam-fallback branch --
    once the cutover flag is on, labit-core alone is a complete answer for
    BOTH a new and a legacy reqno. Returns raw PDF bytes."""
    if not LABIT_CORE_BASE_URL:
        raise Exception("LABIT_CORE_BASE_URL must be set in the environment to call labit_tools.")
    params = {"scope": scope}
    if testids:
        params["testids"] = testids if isinstance(testids, str) else ",".join(testids)
    if include_trends:
        params["include_trends"] = "true"
    url = f"{LABIT_CORE_BASE_URL}/api/dispatch-status/{reqno}/pdf"
    try:
        r = requests.get(url, params=params, auth=_auth(), timeout=(3, 30))
    except requests.RequestException as exc:
        raise Exception(f"labit-core dispatch PDF call failed: {exc}") from exc
    if r.status_code == 404:
        raise LabitCoreReportNotFound(f"labit-core dispatch PDF: unknown requisition for {reqno}")
    if not r.ok:
        raise Exception(f"labit-core dispatch PDF API failed: {r.status_code} {r.text[:500]}")
    return r.content


DELIVER_INTERNAL_TOKEN = os.environ.get(
    "DELIVER_INTERNAL_TOKEN",
    config["cutover"].get("deliver_internal_token", "") if config.has_section("cutover") else "",
)


def fetch_trend_data(mrno):
    """Drop-in replacement for trends_data_api.fetch_trends_data(mrno) --
    labit-core's /internal/dispatch/trend-data/{mrn} already merges
    labit_core + shivam_archive (patient_archive_service.previous_values_by_mrn,
    the SAME merge Consultant View's "Previous Reports" tab uses), so this
    is inherently a combined answer with no separate archive-fallback
    branch needed -- MRN-keyed identity spans the cutover cleanly, unlike a
    reqno. User, 2026-08-30: "Trends... the json which TAPIQuery provides,
    which we need to blackbox to get shivam archive to render... in
    combination with labit's data" -- this is that blackboxing, done at
    the labit-core layer, not here."""
    if not LABIT_CORE_BASE_URL:
        raise Exception("LABIT_CORE_BASE_URL must be set in the environment to call labit_tools.")
    if not DELIVER_INTERNAL_TOKEN:
        raise Exception("DELIVER_INTERNAL_TOKEN must be set in the environment to call labit_tools.fetch_trend_data.")
    url = f"{LABIT_CORE_BASE_URL}/internal/dispatch/trend-data/{mrno}"
    try:
        r = requests.get(url, headers={"X-Internal-Token": DELIVER_INTERNAL_TOKEN}, timeout=(3, 20))
    except requests.RequestException as exc:
        raise Exception(f"labit-core trend-data call failed: {exc}") from exc
    if r.status_code == 404:
        raise LabitCoreReportNotFound(f"labit-core trend-data: unknown mrno {mrno}")
    if not r.ok:
        raise Exception(f"labit-core trend-data API failed: {r.status_code} {r.text[:500]}")
    return r.json()
