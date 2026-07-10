import requests
import configparser
import os
import time
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
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

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

SHIVAM_BASE = str(config.get("api", "radiology_wordole_base", fallback="http://120.138.8.37:9999/shivam")).rstrip("/")
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
    print(f"[FETCH_SCHEME] Session established: {report_fetcher.session is not None}")

    base_url = f"{SHIVAM_BASE}/DgReportingVF"
    date_str = datetime.now().strftime("%d/%m/%Y")

    # Use scheme_testid if available, otherwise null (for ungrouped tests)
    scheme_testid = scheme_key if (scheme_key.startswith("TS") or scheme_key == "") else "null"

    # Use first test's TESTID as representative testid
    first_testid = test_records[0].get("TESTID", "")

    # Debug: show what we're sending
    print(f"[FETCH_SCHEME] scheme_key={scheme_key}, scheme_testid={scheme_testid}, first_testid={first_testid}")
    print(f"[FETCH_SCHEME] test_records count={len(test_records)}, first record keys={list(test_records[0].keys())}")

    # Build query string like outsourced_report_fetcher does
    query = (
        f"scheme_testid={scheme_testid}&chkrephead={'1' if include_header else '0'}"
        f"&keyid={str(reqid).strip()}&testid={str(first_testid).strip()}"
        f"&ptype=null&calledfrom=0&formid=wf145&transid=TR12&fromdt={date_str}&todt={date_str}"
    )

    if reqno:
        query += f"&reqno={str(reqno).strip()}"

    url = f"{base_url}?{query}"

    try:
        print(f"DgReportingVF URL: {url}")
        response = report_fetcher.session.get(url, timeout=HTTP_TIMEOUT_REPORT, allow_redirects=True)

        content_type = response.headers.get("Content-Type", "")
        content = response.content or b""
        print(f"DgReportingVF response for {scheme_key}: status={response.status_code}, content_type={content_type[:80]}, content_length={len(content)}")

        if not content:
            raise Exception(f"Empty response from DgReportingVF for {scheme_key}")

        if "application/pdf" not in content_type and not content.startswith(b"%PDF"):
            response_text = response.text or ""
            print(f"[FETCH_SCHEME] Full response: {response_text[:500]}")
            raise Exception(f"DgReportingVF error: {response_text[:200]}")

        # Save PDF temporarily
        temp_path = os.path.join(OUTPUT_DIR, f"{reqid}_{scheme_key}_temp.pdf")
        with open(temp_path, "wb") as f:
            f.write(content)

        # Check file size
        file_size = os.path.getsize(temp_path)
        if file_size < 1000:
            raise Exception(f"PDF file too small ({file_size} bytes) for scheme {scheme_key}")

        print(f"[FETCH_SCHEME] Saved {file_size} bytes to {temp_path}")
        return temp_path

    except requests.exceptions.Timeout:
        raise Exception(f"Timeout fetching {scheme_key} from DgReportingVF")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Network error fetching {scheme_key}: {e}")
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

        # Get full status to extract reqno if needed
        if reqno:
            status = fetch_report_status(reqno)
        else:
            status = fetch_report_status_by_reqid(reqid)

        tests = status.get("tests", []) if status else []
        print(f"[LAB_REPORT] Status API returned {len(tests)} tests")

        # Ensure we have reqno for DgReportingVF (needed for print counter update)
        if not reqno and status:
            reqno = status.get("reqno", "")
            print(f"[LAB_REPORT] Extracted reqno from status: {reqno}")

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
                if pdf_path is None:
                    print(f"[LAB_REPORT] WARNING: _fetch_scheme_pdf returned None for {scheme_key}")
                else:
                    print(f"[LAB_REPORT] Successfully fetched scheme {scheme_key}: {pdf_path}")
                    temp_pdfs.append(pdf_path)
            except Exception as e:
                error_msg = str(e)
                print(f"[LAB_REPORT] ERROR: failed to fetch scheme {scheme_key}: {error_msg}")
                # Store last error for debugging
                get_lab_collated_report._last_scheme_error = error_msg
                # Continue fetching other schemes instead of failing completely

        if not temp_pdfs:
            last_error = getattr(get_lab_collated_report, '_last_scheme_error', 'unknown')
            debug_info = f"Tried to fetch {len(schemes_ordered)} schemes but all failed. Last error: {last_error}"
            print(f"[LAB_REPORT] ERROR: {debug_info}")
            raise Exception(f"Failed to fetch any lab report PDFs ({debug_info})")

        # Verify all temp PDFs exist and have content
        for pdf_path in temp_pdfs:
            if not os.path.exists(pdf_path):
                raise Exception(f"PDF file missing: {pdf_path}")
            size = os.path.getsize(pdf_path)
            if size < 100:
                raise Exception(f"PDF file too small ({size} bytes): {pdf_path}")
            print(f"[LAB_REPORT] Verified PDF: {os.path.basename(pdf_path)} ({size} bytes)")

        # Merge PDFs if multiple, or copy if single
        print(f"[LAB_REPORT] Merging {len(temp_pdfs)} PDF(s)")
        try:
            if len(temp_pdfs) == 1:
                shutil.copyfile(temp_pdfs[0], cache_path)
            else:
                merge_pdfs(temp_pdfs, cache_path)
        except Exception as e:
            raise Exception(f"Failed to merge/copy PDFs: {e}") from e

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
