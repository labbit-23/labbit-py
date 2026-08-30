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
from pathlib import Path

from app import labit_tools, report_status, trends_data_api
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


def fetch_trend_data(mrno):
    """User, 2026-08-30: "Trends... the json which TAPIQuery provides, which
    we need to blackbox to get shivam archive to render... in combination
    with labit's data." labit-core's own trend-data endpoint already does
    that combining (patient_archive_service.previous_values_by_mrn) -- no
    separate archive-fallback branch needed here, since it's MRN-keyed
    identity, not a reqno that may or may not have migrated. Same
    config-gated safety as report-status: while the flag is off, this never
    attempts a labit-core call at all.

    NOTE: response shape is labit-core's own (see
    labit-core/app/routers/internal.py::trend_data's docstring: "wants a
    parameters[]-shaped object", the same shape-tolerant contract
    labit-main's own trend-data consumer already handles) -- NOT
    trends_data_api.fetch_trends_data's Oracle-row shape. Callers reading
    specific old field names off the Oracle shape need their own mapping
    before this is wired into main.py's /trend-data/{mrno} route; not yet
    done as of this commit, deliberately -- see BUILD_LOG/commit message.
    """
    if not _labit_core_enabled():
        return trends_data_api.fetch_trends_data(mrno)
    return labit_tools.fetch_trend_data(mrno)
