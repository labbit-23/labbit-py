import requests
import configparser
from pathlib import Path

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

LOOKUP_URL = config["api"]["lookup_url"]


def fetch_today_requisitions(date):

    query = f"""
    SELECT
        REQNO,
        REQID,
        MRNO,
        PATIENTNM,
        PHONENO
    FROM diagnotech.REQUISITIONS
    WHERE TRUNC(REQDT)=TO_DATE('{date}','YYYY-MM-DD')
    AND PHONENO IS NOT NULL
    """

    payload = {
        "qry": query
    }

    r = requests.post(LOOKUP_URL, json=payload)

    return r.json()["data"]
