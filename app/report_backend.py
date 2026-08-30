"""Black-box facade for report-status lookups -- callers (main.py today,
delivery_engine.py/the bot eventually) never need to know whether a given
reqno's status came from labit-core or Shivam/Oracle.

User, 2026-08-30, on the cutover approach: "labit-py doesnt need to know
where the PDF has come from, as long as it lands, or where the status json
has arrived from, thats a safe way to switch. Transparency isnt key, key is
blackboxing the result... if it means redirecting from one py to another on
switchover, its clean." This module is that redirect.

Hybrid, not a blind global flag flip: labit-core is tried FIRST -- it's the
system of record going forward, and every genuinely NEW reqno only ever
exists there (Shivam history is not migrated, per MIGRATION_STANCE.md).
Falls back to the Shivam/TApiQuery path ONLY on labit_tools's specific
LabitCoreReportNotFound signal -- a real legacy-only reqno still
straggling through the pipeline at the moment of cutover. Any OTHER
labit-core failure (network, auth, a genuine 500) is deliberately NOT
swallowed into a Shivam fallback: that would risk silently serving stale
or wrong status for something labit-core actually knows about but just
failed to answer right now. It raises loudly instead, exactly as either
backend would on its own.

User, 2026-08-30 (safety requirement): "if we do deploy labit-py again, it
shouldnt start seeking labit core endpoints but wait for a cutover setting
in its config." `main.py`'s import line is deliberately NOT the switch --
it can safely point here ahead of any actual cutover, because THIS module
reads config.ini's `[cutover] report_status_use_labit_core` flag
(default: false) and, while it's false, never attempts a labit-core call
at all -- goes straight to Shivam, identical to calling report_status.py
directly. A routine redeploy before the real cutover moment is therefore
inert. The actual cutover-night switch is flipping that ONE config value
and restarting the process -- no code or git change needed that night.
"""

import configparser
import tempfile
from pathlib import Path

from app import labit_tools, report_status, req_lookup, trends_data_api
from app.labit_tools import LabitCoreReportNotFound

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")


def _labit_core_enabled() -> bool:
    if not config.has_section("cutover"):
        return False
    return config["cutover"].getboolean("report_status_use_labit_core", fallback=False)


def fetch_report_status(reqno):
    if not _labit_core_enabled():
        return report_status.fetch_report_status(reqno)
    try:
        return labit_tools.fetch_report_status(reqno)
    except LabitCoreReportNotFound:
        return report_status.fetch_report_status(reqno)


def fetch_report_status_by_reqid(reqid):
    if not _labit_core_enabled():
        return report_status.fetch_report_status_by_reqid(reqid)
    try:
        return labit_tools.fetch_report_status_by_reqid(reqid)
    except LabitCoreReportNotFound:
        return report_status.fetch_report_status_by_reqid(reqid)


def fetch_pdf_path(reqno, old_fn, *, scope="all", testids=None):
    """Drop-in replacement for main.py's PDF-fetching routes (/report,
    /reports, /radiologyreport, /lab_report -- the full sweep, 2026-08-30)
    -- report_sender_worker.py (py_utils, confirmed same
    https://api.sdrc.in/py deployment the bot's NEOSOFT_API_BASE_URL also
    points at) fetches its send-time document from /report specifically.
    Returns a FILE PATH, matching the existing FileResponse contract --
    labit_tools.fetch_dispatch_pdf returns raw bytes, written to a temp
    file here so main.py's route logic needs no change beyond calling this.

    `old_fn` is a zero-arg callable the CALLER pre-binds to its own exact
    old Shivam call (get_combined_report/get_report/get_radiology_report/
    get_lab_collated_report all have different signatures -- binding at
    the call site means this facade never needs to know any of them).
    `scope` maps to labit-core's dispatch-status/pdf scope param
    ("radiology" for the radiology-only route, "lab" for the lab-only
    route, "all" for the combined ones). `testids`, when given, is
    meaningful on the labit-core-native branch (dispatch-status/pdf
    supports it directly) -- on the archive-fallback branch it has no
    equivalent (an archived report is one fixed document), so it's simply
    not sent there, an honest degrade to "the whole archived report"
    rather than erroring.

    Needs `reqno` to reach labit-core at all (its PDF endpoint is reqno-
    keyed) -- report_sender_worker.py's own `_build_report_document_url`
    already resolves reqno from the job or the status call in the common
    case; when it's genuinely unavailable, or the flag is off, or
    labit-core+archive BOTH come up empty (LabitCoreReportNotFound --
    Oracle stays live read-only for a month, so this is a legitimate
    last-resort net, not masking a real failure), falls back to the exact
    old Shivam call, unchanged. Old-path print-variant options
    (include_header/apply_radiology_background/printtype) have no
    labit-core equivalent -- its own PDF already applies its own unified
    header/letterhead/background rules, so they're simply not passed
    through on that path, not silently misapplied."""
    if not _labit_core_enabled() or not reqno:
        return old_fn()
    try:
        pdf_bytes = labit_tools.fetch_dispatch_pdf(reqno, scope=scope, testids=testids)
    except LabitCoreReportNotFound:
        return old_fn()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        return tmp.name


def fetch_requisitions_by_date(date, org_id=None):
    """Sweep item, 2026-08-30: the staff Report Dispatch page's date
    search -- not bot/sender-critical, but real, active daily use.
    labit-core's endpoint is already the combined answer (built the same
    session, traced field-for-field against this exact contract), so no
    archive-fallback branch is needed here either. Import deferred to
    avoid a circular import -- delivery_api.py itself imports
    fetch_report_status from THIS module."""
    if not _labit_core_enabled():
        from app import delivery_api
        return delivery_api.fetch_requisitions_by_date(date, org_id=org_id)
    return labit_tools.fetch_requisitions_by_date(date, org_id=org_id)


def fetch_lookup(phone):
    """User, 2026-08-30: closing the last gap flagged in "are we ready to
    switch" -- /lookup/{phone} is the bot's primary "find my reports" entry
    point and had no labit-core/archive awareness at all until now.
    labit-core's /api/dispatch-lookup/{phone} already merges labit_core +
    shivam_archive (dispatch_lookup_service.py, commit 2da47a2) -- no
    separate archive-fallback branch needed here, same reasoning as
    trend-data. Shape lines up with req_lookup.fetch_reqids' own
    {"reqid","reqno","patient_name","mrno","reqdt"} rows already (plus an
    additive "source" tag) -- main.py's route needs no reshaping."""
    if not _labit_core_enabled():
        return {"phone": phone, "latest_reports": req_lookup.fetch_reqids(phone)}
    return labit_tools.fetch_lookup(phone)


def fetch_trend_data(mrno, standardized=True, psyntax_mode="neutral"):
    """User, 2026-08-30: "Trends... the json which TAPIQuery provides, which
    we need to blackbox to get shivam archive to render... in combination
    with labit's data." labit-core's own trend-data endpoint already does
    that combining (patient_archive_service.previous_values_by_mrn) -- no
    separate archive-fallback branch needed here, since it's MRN-keyed
    identity, not a reqno that may or may not have migrated. Same
    config-gated safety as report-status: while the flag is off, this never
    attempts a labit-core call at all.

    Response shape is labit-core's own `parameters[]`/`points[]` structure,
    NOT trends_data_api.fetch_trends_data's flat Oracle-row `data[]` shape
    -- and that's fine, not a gap: the real consumer
    (labit-main's app/api/patient/portal/route.js) feeds whatever this
    returns straight into `normalizeNeosoftTrendPayload()`, which is
    ALREADY shape-tolerant and accepts labit-core's structure natively
    (confirmed 2026-08-30, same session, when the Smart Trend PDF route
    was repointed) -- no reshaping needed there. The one thing that WOULD
    break un-adapted is main.py's own /trend-data/{mrno} route, which
    gates on `row_count` (an Oracle-shape-specific key) before returning --
    so a `row_count` is added here, counting every point across every
    parameter, purely so that existing gate keeps working for either shape
    without main.py's route needing to know which one it got."""
    if not _labit_core_enabled():
        return trends_data_api.fetch_trends_data(mrno, standardized=standardized, psyntax_mode=psyntax_mode)
    payload = labit_tools.fetch_trend_data(mrno)
    if isinstance(payload, dict) and "row_count" not in payload:
        parameters = payload.get("parameters") or []
        payload["row_count"] = sum(len(p.get("points") or []) for p in parameters if isinstance(p, dict))
    return payload
