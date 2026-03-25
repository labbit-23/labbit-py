import requests
import configparser
import os
import sys
from pathlib import Path

# -----------------------------
# Load configuration
# -----------------------------
config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BASE = config["server"]["base_url"]
CONTEXT = config["server"]["context"]

APP = f"{BASE}/{CONTEXT}"

USER = config["login"]["username"]
PASS = config["login"]["password"]

REG = config["defaults"]["reg"]
VERSION = config["defaults"]["version"]
CLIENTTYPE = config["defaults"]["clienttype"]

OUTPUT_DIR = config["paths"]["output_dir"]

# -----------------------------
# Helpers
# -----------------------------
def first_pair(text):
    parts = text.split(":")
    return parts[0], parts[1]

def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

# -----------------------------
# Login
# -----------------------------
def login():

    session = requests.Session()

    print("Opening login page...")
    session.get(f"{APP}/ClientLogin.jsp")

    print("Fetching locations...")
    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": USER,
        "table": "loc",
        "uname": USER,
        "reg": REG
    })

    loc_name, loc_id = first_pair(r.text)
    print("Location:", loc_name, loc_id)

    print("Fetching departments...")
    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": loc_id,
        "table": "depts",
        "uname": USER,
        "reg": REG
    })

    dept_name, dept_id = first_pair(r.text)
    print("Department:", dept_name, dept_id)

    print("Fetching subdepartments...")
    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "opt": dept_id,
        "table": "subdepts",
        "uname": USER,
        "reg": REG
    })

    subdept_name, subdept_id = first_pair(r.text)
    print("SubDept:", subdept_name, subdept_id)

    print("Fetching shifts...")
    r = session.get(f"{APP}/ClientLoginLoad.jsp", params={
        "table": "shifts",
        "uname": USER,
        "reg": REG
    })

    shift_name, shift_id = first_pair(r.text)
    print("Shift:", shift_name, shift_id)

    print("Logging in...")

    login = session.get(f"{APP}/ClientSubmit", params={
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

    print("Login response:", login.text)
    print("Session cookie:", session.cookies.get_dict())

    return session

# -----------------------------
# Download report
# -----------------------------
def download_report(session, reqid):

    print("Downloading report:", reqid)

    r = session.get(f"{APP}/ReportDispatchPrints", params={
        "chkrephead": "1",
        "reqid": reqid,
        "ptype": "0",
        "calledfrom": "2",
        "printtype": "1"
    })

    if "application/pdf" not in r.headers.get("Content-Type",""):
        print("Unexpected response:")
        print(r.text[:500])
        return

    ensure_output_dir()

    path = os.path.join(OUTPUT_DIR, f"{reqid}.pdf")

    with open(path, "wb") as f:
        f.write(r.content)

    print("Saved:", path)

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage:")
        print("python report_fetcher.py <REQID>")
        sys.exit(1)

    reqid = sys.argv[1]

    session = login()

    download_report(session, reqid)
