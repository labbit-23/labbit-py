import requests
import urllib.parse
import configparser
import os
import time
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

SESSION_TIMEOUT = 1800  # 30 minutes

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


# -----------------------------
# Login
# -----------------------------
def login():

    global session
    global last_login

    session = requests.Session()

    print("Logging in...")
    session.cookies.set("style", "teal")
    session.get(f"{APP}/ClientLogin.jsp")

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": USER,
        "table": "loc",
        "uname": USER,
        "reg": REG
    })

    loc_name, loc_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": loc_id,
        "table": "depts",
        "uname": USER,
        "reg": REG
    })

    dept_name, dept_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": dept_id,
        "table": "subdepts",
        "uname": USER,
        "reg": REG
    })

    subdept_name, subdept_id = first_pair(r.text)

    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "table": "shifts",
        "uname": USER,
        "reg": REG
    })

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
    })

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
def download_report(reqid):

    ensure_session()

    print("Fetching report:", reqid)

    r = session.get(f"{APP}/ReportDispatchPrints", params={
        "chkrephead": "1",
        "reqid": reqid,
        "ptype": "0",
        "calledfrom": "2",
        "printtype": "1"
    })

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

    # Radiology placeholder
    print("Non-PDF response — likely radiology report")

    raise Exception("Radiology report handling not implemented yet")

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
        }
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
        }
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

    r = session.get(f"{APP}/globalreport", params=params)

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

def get_combined_report(reqid):

    files = []

    # -----------------------------
    # 1. Lab report
    # -----------------------------
    try:
        lab_path = get_report(reqid)
        files.append(lab_path)
    except Exception as e:
        print("Lab not available:", e)

    # -----------------------------
    # 2. Radiology report
    # -----------------------------
    try:
        from app.radiology_fetcher import get_radiology_report

        rad_path = get_radiology_report(reqid)
        files.append(rad_path)
    except Exception as e:
        print("Radiology not available:", e)

    # -----------------------------
    # 3. Nothing found
    # -----------------------------
    if not files:
        raise Exception("No reports available")

    # -----------------------------
    # 4. Only one → return
    # -----------------------------
    if len(files) == 1:
        return files[0]

    # -----------------------------
    # 5. Merge both
    # -----------------------------
    output_path = os.path.join(OUTPUT_DIR, f"{reqid}_COMBINED.pdf")

    return merge_pdfs(files, output_path)

# -----------------------------
# Public function
# -----------------------------
def get_report(reqid):

    return download_report(reqid)
