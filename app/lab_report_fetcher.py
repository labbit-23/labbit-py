import requests
import os
import time
import hashlib
import shutil
from app.pdf_utils import validate_pdf, merge_pdfs
from app.report_fetcher import (
    download_report,
    _build_combined_cache_path,
    _is_recent_file,
    _acquire_key_lock,
    _release_key_lock,
    _write_cache_metadata,
    ensure_output_dir,
    REPORT_REUSE_WINDOW_SECONDS,
    REPORT_LOCK_WAIT_SECONDS,
    REPORT_LOCK_POLL_SECONDS,
    OUTPUT_DIR,
)
from app.report_status import fetch_report_status, fetch_report_status_by_reqid

COMBINED_CACHE_DIR = os.path.join(OUTPUT_DIR, "_combined_cache")


def _fetch_per_test_status(reqid, reqno=None):
    """
    Fetch per-test status from the status API.
    Returns list of test records with SCHEMEID, APPROVEDFLG, GROUPID, etc.
    """
    try:
        if reqno:
            status = fetch_report_status(reqno)
        else:
            status = fetch_report_status_by_reqid(reqid)
        return status.get("tests", []) if status else []
    except Exception as e:
        raise Exception(f"Failed to fetch test status: {e}") from e


def _filter_approved_lab_tests(tests):
    """
    Filter tests to only include approved lab tests (GROUPID=GDEP0001, APPROVEDFLG=1).
    Returns list of approved lab test records.
    """
    return [
        t for t in tests
        if t.get("GROUPID") == "GDEP0001" and str(t.get("APPROVEDFLG", "")).strip() == "1"
    ]


def _group_by_scheme(tests):
    """
    Group approved lab tests by SCHEMEID.
    Returns dict: {schemeid or testid: [test records]}
    For tests without SCHEMEID, use TESTID as the key.
    """
    schemes = {}
    for test in tests:
        schemeid = test.get("SCHEMEID", "").strip()
        key = schemeid if schemeid else test.get("TESTID", "")
        if key not in schemes:
            schemes[key] = []
        schemes[key].append(test)
    return schemes


def _fetch_scheme_report(reqid, scheme_key, tests, include_header=True, printtype="1", reqno=None):
    """
    Fetch report for a scheme or individual test.
    For now, uses the standard download_report() with reqid.
    Future: use scheme_testid parameter if DgReportingVF supports it.
    """
    try:
        path = download_report(
            reqid=reqid,
            include_header=include_header,
            printtype=printtype,
            reqno=reqno
        )
        return path
    except Exception as e:
        raise Exception(f"Failed to fetch report for scheme {scheme_key}: {e}") from e


def get_lab_collated_report(reqid, include_header=True, printtype="1", reqno=None):
    """
    Fetch and collate lab reports based on per-test status.

    Logic:
    1. Query per-test status API to get approved lab tests with SCHEMEID
    2. Filter for approved lab tests only (GROUPID=GDEP0001, APPROVEDFLG=1)
    3. Group by SCHEMEID (or individual TESTID if no scheme)
    4. Fetch report for entire requisition (all approved lab tests together)
    5. Cache and return combined PDF path
    """
    ensure_output_dir()

    cache_path, meta_path = _build_combined_cache_path(
        reqid=reqid,
        include_header=include_header,
        apply_radiology_background=False,
        printtype=printtype,
        reqno=reqno
    )

    if _is_recent_file(cache_path, meta_path, REPORT_REUSE_WINDOW_SECONDS):
        return cache_path

    lock_path = f"{cache_path}.lock"
    lock_fd = None
    try:
        lock_fd = _acquire_key_lock(
            lock_path=lock_path,
            wait_seconds=REPORT_LOCK_WAIT_SECONDS,
            poll_seconds=REPORT_LOCK_POLL_SECONDS
        )
    except TimeoutError:
        lock_fd = None

    try:
        if _is_recent_file(cache_path, meta_path, REPORT_REUSE_WINDOW_SECONDS):
            return cache_path

        # Fetch per-test status
        tests = _fetch_per_test_status(reqid, reqno)

        # Filter to approved lab tests only
        approved_lab_tests = _filter_approved_lab_tests(tests)

        if not approved_lab_tests:
            raise Exception("No approved lab tests found")

        # For now: fetch entire lab report (all approved tests together)
        # TODO: Later implement scheme-wise extraction when DgReportingVF supports scheme_testid
        lab_path = download_report(
            reqid=reqid,
            include_header=include_header,
            printtype=printtype,
            reqno=reqno
        )

        # Cache and return
        shutil.copyfile(lab_path, cache_path)
        _write_cache_metadata(meta_path)
        return cache_path

    except requests.RequestException as exc:
        raise Exception(f"UPSTREAM_REQUEST_FAILED: {exc}") from exc
    finally:
        if lock_fd is not None:
            _release_key_lock(lock_fd, lock_path)
