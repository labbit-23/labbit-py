import requests
import urllib.parse
import configparser
import os
import time
import hashlib
import shutil
from pathlib import Path
from app.pdf_utils import validate_pdf, merge_pdfs

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BASE = config["server"]["base_url"]
CONTEXT = config["server"]["context"]

APP = f"{BASE}/{CONTEXT}"

USER = config["login"]["username"]
PASS = config["login"]["password"]
USER = os.environ.get("NEOSOFT_LOGIN_USERNAME", USER)
PASS = os.environ.get("NEOSOFT_LOGIN_PASSWORD", PASS)

REG = config["defaults"]["reg"]
VERSION = config["defaults"]["version"]
CLIENTTYPE = config["defaults"]["clienttype"]

OUTPUT_DIR = config["paths"]["reports"]
COMBINED_CACHE_DIR = os.path.join(OUTPUT_DIR, "_combined_cache")

REPORT_REUSE_WINDOW_SECONDS = int(os.environ.get("REPORT_REUSE_WINDOW_SECONDS", "60"))
REPORT_LOCK_WAIT_SECONDS = int(os.environ.get("REPORT_LOCK_WAIT_SECONDS", "15"))
REPORT_LOCK_POLL_SECONDS = float(os.environ.get("REPORT_LOCK_POLL_SECONDS", "0.2"))

SESSION_TIMEOUT = 1800  # 30 minutes
HTTP_TIMEOUT_FAST = (3, 20)
HTTP_TIMEOUT_REPORT = (5, 90)

session = None
last_login = 0


# -----------------------------
# Helpers
# -----------------------------
def first_pair(text):
    parts = text.strip().split(":")
    return parts[0].strip(), parts[1].strip()


def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    if not os.path.exists(COMBINED_CACHE_DIR):
        os.makedirs(COMBINED_CACHE_DIR)


def _build_combined_cache_path(reqid, include_header=True, apply_radiology_background=True, printtype="1", reqno=None):
    key = "|".join(
        [
            str(reqid or "").strip(),
            str(reqno or "").strip(),
            str(printtype or "1").strip(),
            "H1" if include_header else "H0",
            "R1" if apply_radiology_background else "R0",
        ]
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    safe_reqid = str(reqid or "UNKNOWN").strip() or "UNKNOWN"
    return os.path.join(COMBINED_CACHE_DIR, f"{safe_reqid}_{digest}.pdf")


def _is_recent_file(path, window_seconds):
    if not path or not os.path.exists(path):
        return False
    try:
        age_seconds = time.time() - os.path.getmtime(path)
        return age_seconds >= 0 and age_seconds <= max(0, window_seconds)
    except Exception:
        return False


def _acquire_key_lock(lock_path, wait_seconds, poll_seconds):
    deadline = time.time() + max(0, wait_seconds)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            return fd
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for report lock: {lock_path}")
            time.sleep(max(0.05, poll_seconds))


def _release_key_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        if os.path.exists(lock_path):
            os.unlink(lock_path)
    except Exception:
        pass


# -----------------------------
# Login
# -----------------------------
def login():

    global session
    global last_login

    session = requests.Session()

    print("Logging in...")
    session.cookies.set("style", "teal")
    session.get(f"{APP}/ClientLogin.jsp", timeout=HTTP_TIMEOUT_FAST)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": USER,
        "table": "loc",
        "uname": USER,
        "reg": REG
    }, timeout=HTTP_TIMEOUT_FAST)

    loc_name, loc_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": loc_id,
        "table": "depts",
        "uname": USER,
        "reg": REG
    }, timeout=HTTP_TIMEOUT_FAST)

    dept_name, dept_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": dept_id,
        "table": "subdepts",
        "uname": USER,
        "reg": REG
    }, timeout=HTTP_TIMEOUT_FAST)

    subdept_name, subdept_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "table": "shifts",
        "uname": USER,
        "reg": REG
    }, timeout=HTTP_TIMEOUT_FAST)

    shift_name, shift_id = first_pair(r.text)

    session.get(f"{APP}/ClientSubmit", params={
        "txtnm": USER,
        "txtpwd": PASS,
        "dept": dept_name,
        "deptid": dept_id,
        "subdept": subdept_name,
        "subdeptid": subdept_id,
        "loc": loc_name,
        "locid": loc_id,
        "shift": shift_name,
        "shiftid": shift_id,
        "reg": REG,
        "version": VERSION,
        "clienttype": CLIENTTYPE
    }, timeout=HTTP_TIMEOUT_FAST)

    last_login = time.time()

    print("Login successful")


# -----------------------------
# Session manager
# -----------------------------
def ensure_session():

    global session
    global last_login

    if session is None:
        login()
        return

    if time.time() - last_login > SESSION_TIMEOUT:
        print("Session expired. Re-logging...")
        login()


# -----------------------------
# Download report
# -----------------------------
def download_report(reqid, include_header=True, printtype="1", reqno=None):

    ensure_session()

    print("Fetching report:", reqid)

    params = {
        "chkrephead": "1" if include_header else "0",
        "reqid": reqid,
        "ptype": "0",
        "calledfrom": "2",
        "printtype": str(printtype or "1")
    }

    if reqno:
        params["reqno"] = str(reqno)

    r = session.get(f"{APP}/ReportDispatchPrints", params=params, timeout=HTTP_TIMEOUT_REPORT)

    # Lab report
    if "application/pdf" in r.headers.get("Content-Type", ""):

        ensure_output_dir()

        path = os.path.join(OUTPUT_DIR, f"{reqid}.pdf")

        with open(path, "wb") as f:
            f.write(r.content)

        # Validate PDF
        if not validate_pdf(path):
            print("Blank or invalid PDF detected")
            raise Exception("Report PDF is blank")

        return path

    # Non-PDF response handling.
    response_text = (r.text or "")[:1200].strip().lower()

    if str(printtype or "1") == "0":
        # Pending dispatcher mode: no pending output should be treated as a clean business case.
        if "no record" in response_text or "no pending" in response_text or "already" in response_text:
            raise Exception("NO_PENDING_REPORTS")
        raise Exception("PENDING_REPORT_NOT_AVAILABLE")

    raise Exception("LAB_REPORT_NOT_AVAILABLE")

# -----------------------------
# Trend Report (MRNO based)
# -----------------------------
def get_trend_report(mrno):

    ensure_session()

    print("Fetching trend report:", mrno)

    # STEP 1 — open parameter page
    session.get(
        f"{APP}/singleparameter.jsp",
        params={
            "id": "MR No Wise Test Result Trends (Values)",
            "rid": "654",
            "ptype": "2",
            "userid": "IU000120",
            "usernm": "ADMIN",
            "locid": "Loc00001"
        },
        timeout=HTTP_TIMEOUT_FAST,
    )

    # STEP 2 — build dataset
    query = f"select cregno From ots1.Patientsregistration Where cregno in ('{mrno}')"

    session.post(
        f"{APP}/globalreport",
        params={
            "Createsubdeptview": "SubDeptView",
            "view_qry": query,
            "strnames": query.replace("cregno", "cregno regno"),
            "fromdt": "12/03/2026",
            "todt": "12/03/2026",
            "years1": "2025"
        },
        timeout=HTTP_TIMEOUT_REPORT,
    )

    # STEP 3 — render PDF
    params = {
        "desc": "Shows Trends of Test Results (last 5 values)",
        "subdeptname": "                              ",
        "varify": "Test",
        "type": "MR No Wise Test Result Trends (Values)",
        "sname": "undefined",
        "sname1": "undefined",
        "fromdt": "12/03/2026",
        "todt": "12/03/2026",
        "locid": "Loc00001",
        "reportid": "654",
        "locnm": "01 Main,",
        "spid": mrno,
        "chkparamall": "1",
        "chkmsexcel": "0",
        "fromtm": "",
        "totm": "",
        "thirddate": "12/03/2026"
    }

    r = session.get(f"{APP}/globalreport", params=params, timeout=HTTP_TIMEOUT_REPORT)

    print("Content-Type:", r.headers.get("Content-Type"))
    print("Response length:", len(r.content))
    print("Response preview:", r.text[:200])

    if "application/pdf" not in r.headers.get("Content-Type", ""):
        return {
            "status": "error",
            "message": "Trend report not available"
        }
    ensure_output_dir()

    path = os.path.join(OUTPUT_DIR, f"Trend_Report_{mrno}.pdf")

    with open(path, "wb") as f:
        f.write(r.content)

    return path

def get_combined_report(reqid, include_header=True, apply_radiology_background=True, printtype="1", reqno=None):
    ensure_output_dir()
    cache_path = _build_combined_cache_path(
        reqid=reqid,
        include_header=include_header,
        apply_radiology_background=apply_radiology_background,
        printtype=printtype,
        reqno=reqno
    )

    if _is_recent_file(cache_path, REPORT_REUSE_WINDOW_SECONDS):
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
        # Do not fail report delivery if lock contention is high.
        # Continue without a lock (worst case: duplicate generation once).
        lock_fd = None

    try:
        # Double-check after acquiring lock in case another request already generated it.
        if _is_recent_file(cache_path, REPORT_REUSE_WINDOW_SECONDS):
            return cache_path

        files = []

        # -----------------------------
        # 1. Lab report
        # -----------------------------
        try:
            lab_path = get_report(reqid, include_header=include_header, printtype=printtype, reqno=reqno)
            files.append(lab_path)
        except Exception as e:
            print("Lab not available:", e)

        # -----------------------------
        # 2. Radiology report
        # -----------------------------
        try:
            from app.radiology_fetcher import get_radiology_report

            rad_path = get_radiology_report(reqid, apply_background_overlay=apply_radiology_background)
            files.append(rad_path)
        except Exception as e:
            print("Radiology not available:", e)

        # -----------------------------
        # 3. Nothing found
        # -----------------------------
        if not files:
            raise Exception("No reports available")

        # -----------------------------
        # 4. Only one → persist copy and return
        # -----------------------------
        if len(files) == 1:
            shutil.copyfile(files[0], cache_path)
            return cache_path

        # -----------------------------
        # 5. Merge both into cached artifact
        # -----------------------------
        return merge_pdfs(files, cache_path)
    except requests.RequestException as exc:
        raise Exception(f"UPSTREAM_REQUEST_FAILED: {exc}") from exc
    finally:
        if lock_fd is not None:
            _release_key_lock(lock_fd, lock_path)

# -----------------------------
# Public function
# -----------------------------
def get_report(reqid, include_header=True, printtype="1", reqno=None):

    return download_report(
        reqid,
        include_header=include_header,
        printtype=printtype,
        reqno=reqno
    )
