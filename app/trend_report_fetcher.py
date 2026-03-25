import requests
import configparser
import os
from datetime import datetime
from pathlib import Path

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

TREND_URL = config["trends"]["url"]
OUTPUT_DIR = config["paths"]["reports"]


def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)


def get_trend_report(mrno):

    session = requests.Session()

    today = datetime.today().strftime("%d/%m/%Y")

    query = f"select cregno From ots1.Patientsregistration Where cregno in ('{mrno}')"

    payload = {
        "Createsubdeptview": "SubDeptView",
        "view_qry": query,
        "strnames": query.replace("cregno", "cregno regno"),
        "fromdt": today,
        "todt": today,
        "years1": datetime.today().strftime("%Y")
    }

    r = session.post(TREND_URL, data=payload)

    ensure_output_dir()

    path = os.path.join(OUTPUT_DIR, f"trend_{mrno}.pdf")

    with open(path, "wb") as f:
        f.write(r.content)

    return path
