import requests
import os
import time
import hashlib
import shutil
from datetime import datetime
from app.pdf_utils import validate_pdf, merge_pdfs
import app.report_fetcher as report_fetcher
from app.report_fetcher import (
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
    HTTP_TIMEOUT_REPORT,
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
    Returns list of approved lab test records in original order.
    """
    return [
        t for t in tests
        if t.get("GROUPID") == "GDEP0001" and str(t.get("APPROVEDFLG", "")).strip() == "1"
    ]


def _group_by_scheme_ordered(tests):
    """
    Group approved lab tests by SCHEMEID while preserving order.
    Returns list of (scheme_key, [test records]) tuples in order of first appearance.
    For tests without SCHEMEID, use TESTID as the key.
    """
    seen = {}
    ordered = []
    for test in tests:
        schemeid = test.get("SCHEMEID", "").strip()
        key = schemeid if schemeid else test.get("TESTID", "")
        if key not in seen:
            seen[key] = []
            ordered.append(key)
        seen[key].append(test)
    return [(key, seen[key]) for key in ordered]


def _fetch_scheme_pdf(reqid, scheme_key, test_records, include_header=True, reqno=None):
    """
    Fetch PDF for a scheme or individual test using DgReportingVF.
    Uses scheme_testid for schemes, null for individual tests without scheme.
    """
    report_fetcher.ensure_session()

    base_url = f"{report_fetcher.APP}/DgReportingVF"
    date_str = datetime.now().strftime("%d/%m/%Y")

    # Use scheme_testid if available, otherwise null (for ungrouped tests)
    scheme_testid = scheme_key if (scheme_key.startswith("TS") or scheme_key == "") else "null"

    # Use first test's TESTID as representative testid
    first_testid = test_records[0].get("TESTID", "")

    params = {
        "scheme_testid": scheme_testid,
        "chkrephead": "1" if include_header else "0",
        "keyid": str(reqid).strip(),
        "testid": str(first_testid).strip(),
        "ptype": "null",
        "calledfrom": "0",
        "formid": "wf145",
        "transid": "TR12",
        "fromdt": date_str,
        "todt": date_str,
    }

    if reqno:
        params["reqno"] = str(reqno).strip()

    try:
        print(f"Fetching from DgReportingVF with params: scheme_testid={params.get('scheme_testid')}, testid={params.get('testid')}")
        response = report_fetcher.session.get(base_url, params=params, timeout=HTTP_TIMEOUT_REPORT)

        content_type = response.headers.get("Content-Type", "")
        print(f"DgReportingVF response for {scheme_key}: status={response.status_code}, content_type={content_type[:80]}, content_length={len(response.content)}")

        if "application/pdf" not in content_type:
            response_preview = (response.text or "")[:500]
            raise Exception(f"Not a PDF response for scheme {scheme_key}: status={response.status_code}, preview={response_preview}")

        # Save PDF temporarily
        temp_path = os.path.join(OUTPUT_DIR, f"{reqid}_{scheme_key}_temp.pdf")
        with open(temp_path, "wb") as f:
            f.write(response.content)

        if not validate_pdf(temp_path):
            raise Exception(f"Blank or invalid PDF for scheme {scheme_key}")

        return temp_path
    except Exception as e:
        raise Exception(f"Failed to fetch scheme {scheme_key}: {e}") from e


def get_lab_collated_report(reqid, include_header=True, printtype="1", reqno=None):
    """
    Fetch and collate lab reports using scheme-wise extraction.

    Logic:
    1. Query per-test status API to get approved lab tests with SCHEMEID
    2. Filter for approved lab tests only (GROUPID=GDEP0001, APPROVEDFLG=1)
    3. Group by SCHEMEID, preserving order from status API
    4. For each scheme/test: fetch PDF using DgReportingVF(scheme_testid=...)
    5. Merge all PDFs in order
    6. Cache and return combined PDF path
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

    temp_pdfs = []
    try:
        print(f"[LAB_REPORT] Starting: reqid={reqid}, reqno={reqno}")

        if _is_recent_file(cache_path, meta_path, REPORT_REUSE_WINDOW_SECONDS):
            print(f"[LAB_REPORT] Using cached result")
            return cache_path

        # Fetch per-test status
        print(f"[LAB_REPORT] Fetching status for reqid={reqid}, reqno={reqno}")
        tests = _fetch_per_test_status(reqid, reqno)
        print(f"[LAB_REPORT] Status API returned {len(tests)} tests")

        # Filter to approved lab tests only (preserves order)
        approved_lab_tests = _filter_approved_lab_tests(tests)
        print(f"[LAB_REPORT] After filtering: {len(approved_lab_tests)} approved lab tests")

        if not approved_lab_tests:
            print("[LAB_REPORT] ERROR: No approved lab tests found after filtering")
            raise Exception("No approved lab tests found")

        # Group by scheme, preserving order of first appearance
        schemes_ordered = _group_by_scheme_ordered(approved_lab_tests)
        print(f"[LAB_REPORT] Grouped into {len(schemes_ordered)} schemes/test groups")

        # Fetch PDF for each scheme/test
        for scheme_key, test_records in schemes_ordered:
            try:
                print(f"[LAB_REPORT] Fetching scheme {scheme_key} with {len(test_records)} tests")
                pdf_path = _fetch_scheme_pdf(
                    reqid=reqid,
                    scheme_key=scheme_key,
                    test_records=test_records,
                    include_header=include_header,
                    reqno=reqno
                )
                print(f"[LAB_REPORT] Successfully fetched scheme {scheme_key}: {pdf_path}")
                temp_pdfs.append(pdf_path)
            except Exception as e:
                error_msg = str(e)[:100]
                print(f"[LAB_REPORT] ERROR: failed to fetch scheme {scheme_key}: {error_msg}")
                # Store error for debugging if all schemes fail
                if not hasattr(get_lab_collated_report, '_last_scheme_error'):
                    get_lab_collated_report._last_scheme_error = error_msg
                # Continue fetching other schemes instead of failing completely

        if not temp_pdfs:
            last_error = getattr(get_lab_collated_report, '_last_scheme_error', 'unknown')
            debug_info = f"Tried to fetch {len(schemes_ordered)} schemes but all failed. Last error: {last_error}"
            print(f"[LAB_REPORT] ERROR: {debug_info}")
            raise Exception(f"Failed to fetch any lab report PDFs ({debug_info})")

        # Merge PDFs if multiple, or copy if single
        print(f"[LAB_REPORT] Merging {len(temp_pdfs)} PDF(s)")
        if len(temp_pdfs) == 1:
            shutil.copyfile(temp_pdfs[0], cache_path)
        else:
            merge_pdfs(temp_pdfs, cache_path)

        _write_cache_metadata(meta_path)
        print(f"[LAB_REPORT] Success: {cache_path}")
        return cache_path

    except Exception as exc:
        print(f"[LAB_REPORT] EXCEPTION: {exc}")
        raise Exception(f"REPORT_FETCH_FAILED: {exc}") from exc
    finally:
        # Cleanup temp PDFs
        for pdf_path in temp_pdfs:
            try:
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)
            except Exception:
                pass

        if lock_fd is not None:
            _release_key_lock(lock_fd, lock_path)
