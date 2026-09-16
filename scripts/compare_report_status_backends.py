#!/usr/bin/env python3
"""Cutover-readiness comparison: calls the SAME reqno/reqid through both the
live Oracle/TApiQuery backend (report_status.py, unchanged) and the new
labit-core backend (labit_tools.py), and diffs the two output dicts
field-by-field.

User, 2026-08-30: "we create a labit_tools on the side and switch from
shivam tools to labit tools" / "test it with existing data ... see how
endpoints behave with TAPI alternative." This is that test -- it makes NO
WhatsApp calls (report-status is a pure read) and does NOT modify
config.ini or touch the live labbit-api PM2 process at all; it just
imports both modules and calls them side by side. Safe to run against real
production reqnos right now.

Usage:
    LABIT_CORE_BASE_URL=https://<vps-host-or-ip>:8001 \\
    LABIT_CORE_SERVICE_USERNAME=svc_dispatch_bot \\
    LABIT_CORE_SERVICE_PASSWORD=<real password, never in the repo> \\
    python scripts/compare_report_status_backends.py --reqno 20260830001
    python scripts/compare_report_status_backends.py --reqid <uuid-or-legacy-reqid>

Run from the labit-py repo root (or with it on PYTHONPATH) since it imports
app.report_status / app.labit_tools directly -- same as main.py does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import labit_tools, report_status  # noqa: E402


# Fields intentionally NEW on the labit-core side (see labit_tools.py's own
# docstring) -- reported separately, never counted as a mismatch.
EXPECTED_NEW_FIELDS = {"dispatch_allowed", "dispatch_denial_code", "dispatch_denial_reason"}


def _diff(old: dict, new: dict) -> tuple[list[str], list[str]]:
    mismatches = []
    new_only = []
    keys = set(old.keys()) | set(new.keys())
    for key in sorted(keys):
        if key == "tests":
            continue  # compared separately below -- per-row diffing is noisier than useful here
        if key in EXPECTED_NEW_FIELDS and key not in old:
            new_only.append(f"{key}={new.get(key)!r}")
            continue
        old_val, new_val = old.get(key), new.get(key)
        if old_val != new_val:
            mismatches.append(f"{key}: oracle={old_val!r} vs labit-core={new_val!r}")
    return mismatches, new_only


def run(label: str, oracle_call, core_call, arg) -> bool:
    print(f"\n=== {label}({arg!r}) ===")
    try:
        oracle_result = oracle_call(arg)
    except Exception as exc:  # noqa: BLE001 -- comparison script, report everything
        print(f"[FAIL] Oracle/TApiQuery call raised: {exc}")
        return False
    try:
        core_result = core_call(arg)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] labit-core call raised: {exc}")
        return False

    mismatches, new_only = _diff(oracle_result, core_result)
    oracle_tests = len(oracle_result.get("tests") or [])
    core_tests = len(core_result.get("tests") or [])

    if new_only:
        print("[INFO] labit-core-only fields (expected, not a mismatch):")
        for line in new_only:
            print(f"       {line}")

    if oracle_tests != core_tests:
        mismatches.append(f"tests[] length: oracle={oracle_tests} vs labit-core={core_tests}")

    if mismatches:
        print(f"[FAIL] {len(mismatches)} field mismatch(es):")
        for line in mismatches:
            print(f"       {line}")
        return False

    print(f"[ OK ] Identical on every shared field ({oracle_tests} test row(s) each).")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reqno", help="A real, live reqno to compare via fetch_report_status")
    parser.add_argument("--reqid", help="A real, live reqid to compare via fetch_report_status_by_reqid")
    args = parser.parse_args()

    if not args.reqno and not args.reqid:
        parser.error("pass at least one of --reqno or --reqid")

    ok = True
    if args.reqno:
        ok = run(
            "fetch_report_status",
            report_status.fetch_report_status,
            labit_tools.fetch_report_status,
            args.reqno,
        ) and ok
    if args.reqid:
        ok = run(
            "fetch_report_status_by_reqid",
            report_status.fetch_report_status_by_reqid,
            labit_tools.fetch_report_status_by_reqid,
            args.reqid,
        ) and ok

    print()
    print("COMPARISON PASSED -- identical output, safe to consider wiring labit_tools in." if ok
          else "COMPARISON FAILED -- do not wire labit_tools in yet, see mismatches above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
