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

The actual cutover-night switch is a one-line change in main.py: import
`fetch_report_status`/`fetch_report_status_by_reqid` from here instead of
from `app.report_status` directly. Nothing else in the codebase needs to
change, and nothing else needs to know which backend answered.
"""

from app import labit_tools, report_status
from app.labit_tools import LabitCoreReportNotFound


def fetch_report_status(reqno):
    try:
        return labit_tools.fetch_report_status(reqno)
    except LabitCoreReportNotFound:
        return report_status.fetch_report_status(reqno)


def fetch_report_status_by_reqid(reqid):
    try:
        return labit_tools.fetch_report_status_by_reqid(reqid)
    except LabitCoreReportNotFound:
        return report_status.fetch_report_status_by_reqid(reqid)
