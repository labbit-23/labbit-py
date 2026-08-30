#!/usr/bin/env python3
"""Verifies app.report_backend's config-gated hybrid fallback -- no real
network calls, no live labit-core/Shivam credentials needed. Proves four
cases before this facade is ever wired into main.py:

0. `[cutover] report_status_use_labit_core` is false (the default) ->
   labit-core is NEVER even attempted -- goes straight to Shivam. This is
   the actual safety property the user asked for: "if we do deploy
   labit-py again, it shouldnt start seeking labit core endpoints but
   wait for a cutover setting in its config."
1. Flag true, labit-core answers normally -> its result is returned,
   Shivam is never called.
2. Flag true, labit-core raises LabitCoreReportNotFound (a real 404 --
   legacy-only reqno) -> falls back to Shivam, returns ITS result.
3. Flag true, labit-core raises any OTHER exception (network/auth/500) ->
   propagates loudly, Shivam is NEVER called as a silent fallback for
   this case.

Run: python scripts/verify_report_backend_fallback.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import labit_tools, report_backend, report_status  # noqa: E402


def case_0_disabled_never_calls_labit_core() -> bool:
    with mock.patch.object(report_backend, "_labit_core_enabled", return_value=False), \
         mock.patch.object(labit_tools, "fetch_report_status") as core, \
         mock.patch.object(report_status, "fetch_report_status", return_value={"source": "shivam"}) as shivam:
        result = report_backend.fetch_report_status("R1")
    ok = result == {"source": "shivam"} and not core.called and shivam.called
    print(f"[{'OK' if ok else 'FAIL'}] Case 0 (flag off -> labit-core never attempted): {result}")
    return ok


def case_1_labit_core_success() -> bool:
    with mock.patch.object(report_backend, "_labit_core_enabled", return_value=True), \
         mock.patch.object(labit_tools, "fetch_report_status", return_value={"source": "labit_core"}) as core, \
         mock.patch.object(report_status, "fetch_report_status") as shivam:
        result = report_backend.fetch_report_status("R1")
    ok = result == {"source": "labit_core"} and core.called and not shivam.called
    print(f"[{'OK' if ok else 'FAIL'}] Case 1 (flag on, labit-core success, no Shivam fallback): {result}")
    return ok


def case_2_not_found_falls_back() -> bool:
    with mock.patch.object(report_backend, "_labit_core_enabled", return_value=True), \
         mock.patch.object(labit_tools, "fetch_report_status", side_effect=labit_tools.LabitCoreReportNotFound("nope")), \
         mock.patch.object(report_status, "fetch_report_status", return_value={"source": "shivam"}) as shivam:
        result = report_backend.fetch_report_status("LEGACY1")
    ok = result == {"source": "shivam"} and shivam.called
    print(f"[{'OK' if ok else 'FAIL'}] Case 2 (flag on, labit-core 404 falls back to Shivam): {result}")
    return ok


def case_3_other_error_does_not_fall_back() -> bool:
    with mock.patch.object(report_backend, "_labit_core_enabled", return_value=True), \
         mock.patch.object(labit_tools, "fetch_report_status", side_effect=Exception("labit-core is down")), \
         mock.patch.object(report_status, "fetch_report_status") as shivam:
        try:
            report_backend.fetch_report_status("R1")
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = "labit-core is down" in str(exc)
    ok = raised and not shivam.called
    print(f"[{'OK' if ok else 'FAIL'}] Case 3 (flag on, labit-core error propagates, Shivam NOT called)")
    return ok


def main() -> int:
    results = [
        case_0_disabled_never_calls_labit_core(),
        case_1_labit_core_success(),
        case_2_not_found_falls_back(),
        case_3_other_error_does_not_fall_back(),
    ]
    print()
    if all(results):
        print("ALL CASES PASSED -- report_backend's config-gated hybrid fallback behaves correctly.")
        return 0
    print("FAILED -- do not wire report_backend into main.py yet.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
