import requests
import json
import configparser
from pathlib import Path

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BASE_URL = config["api"]["lookup_url"]
TERMINAL_ID = config["api"]["terminalid"]
WEBFORMID = config["api"]["webformid"]
LOOKUP_DIRECT = config["api"]["lookup_direct"]


def fetch_reqids(phone):

    payload = json.dumps([
        {
            "phone": phone
        }
    ])

    url = f"{BASE_URL}?webformid={WEBFORMID}&terminalid={TERMINAL_ID}&data={payload}"

    r = requests.get(url)

    data = r.json()

    latest = []

    for row in data[:9]:

        latest.append({
            "reqid": row["REQID"],
            "reqno": row["REQNO"],
            "patient_name": row.get("PATIENTNM", "Unknown"),
            "mrno": row.get("MRNO", "Unknown"),
            "reqdt": row["REQDT"].split(" ")[0]
        })

    return latest

def fetch_reqid_direct(phone):
    payload = json.dumps([{"phone": phone}])
    url = f"{LOOKUP_DIRECT}&data={payload}"

    r = requests.get(url, timeout=15)
    r.raise_for_status()

    data = r.json() or []
    if not isinstance(data, list) or len(data) == 0:
        return None  # single-row mode: nothing found

    row = data[0]
    return {
        "reqid": row.get("REQID"),
        "reqno": row.get("REQNO"),
        "patient_name": row.get("PATIENTNM", "Unknown"),
        "mrno": row.get("MRNO", "Unknown"),
        "reqdt": str(row.get("REQDT", "")).split(" ")[0] if row.get("REQDT") else None
    }
