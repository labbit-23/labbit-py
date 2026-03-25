import datetime
import time
import configparser
import requests
import os
from pathlib import Path

from app.delivery_api import (
    get_requisitions_by_date,
    get_delivery_status,
    update_delivery_status,
    get_report_status
)

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BASE = config["whatsapp"].get("delivery_api_base", "http://127.0.0.1:8000").rstrip("/")
REPORT_PUBLIC_BASE = config["whatsapp"].get("report_public_base", BASE).rstrip("/")
WHATSAPP_ENDPOINT = config["whatsapp"].get("whatsapp_endpoint", "").strip()
WHATSAPP_API_KEY = config["whatsapp"].get("whatsapp_api_key", "").strip()
SEND_REPORTS_TEMPLATE = config["whatsapp"].get("send_reports_template", "").strip()
DEFAULT_PHONE = config["whatsapp"].get("default_phone", "").strip()
FALLBACK_PHONE = config["whatsapp"].get("fallback_phone", "").strip()
WHATSAPP_LANGUAGE = config["whatsapp"].get("language_code", "en").strip() or "en"
BASE = os.environ.get("DELIVERY_API_BASE", BASE).rstrip("/")
REPORT_PUBLIC_BASE = os.environ.get("REPORT_PUBLIC_BASE", REPORT_PUBLIC_BASE).rstrip("/")
WHATSAPP_ENDPOINT = os.environ.get("WHATSAPP_ENDPOINT", WHATSAPP_ENDPOINT).strip()
WHATSAPP_API_KEY = os.environ.get("WHATSAPP_API_KEY", WHATSAPP_API_KEY).strip()
SEND_REPORTS_TEMPLATE = os.environ.get("SEND_REPORTS_TEMPLATE", SEND_REPORTS_TEMPLATE).strip()
DEFAULT_PHONE = os.environ.get("DEFAULT_PHONE", DEFAULT_PHONE).strip()
FALLBACK_PHONE = os.environ.get("FALLBACK_PHONE", FALLBACK_PHONE).strip()
WHATSAPP_LANGUAGE = os.environ.get("WHATSAPP_LANGUAGE", WHATSAPP_LANGUAGE).strip() or "en"
REQUEST_TIMEOUT = 30
POLL_INTERVAL_SECONDS = 300


def normalize_phone(phone):
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def resolve_destination_phone(phone):
    requested_phone = normalize_phone(phone)
    override_phone = normalize_phone(DEFAULT_PHONE)
    fallback_phone = normalize_phone(FALLBACK_PHONE)

    destination = override_phone or requested_phone or fallback_phone
    if not destination:
        raise Exception("No destination phone configured")

    return destination


def get_report_url(reqid):
    return f"{REPORT_PUBLIC_BASE}/report/{reqid}"


def verify_report_download(reqid):
    response = requests.get(f"{BASE}/report/{reqid}", timeout=REQUEST_TIMEOUT)
    if response.status_code == 200 and "application/pdf" in response.headers.get("Content-Type", ""):
        return True
    return False


def build_template_payload(destination, reqid):
    return {
        "messaging_product": "whatsapp",
        "to": destination,
        "type": "template",
        "template": {
            "name": SEND_REPORTS_TEMPLATE,
            "language": {"code": WHATSAPP_LANGUAGE},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "link": get_report_url(reqid),
                                "filename": f"{reqid}.pdf"
                            }
                        }
                    ]
                }
            ]
        }
    }


def send_whatsapp(phone, reqid, reqno):
    if not WHATSAPP_ENDPOINT:
        raise Exception("whatsapp_endpoint is not configured")
    if not WHATSAPP_API_KEY:
        raise Exception("whatsapp_api_key is not configured")
    if not SEND_REPORTS_TEMPLATE:
        raise Exception("send_reports_template is not configured")

    destination = resolve_destination_phone(phone)
    payload = build_template_payload(destination, reqid)

    print(f"Sending report {reqno}/{reqid} to {destination}")
    print(f"Report URL: {get_report_url(reqid)}")

    response = requests.post(
        WHATSAPP_ENDPOINT,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": WHATSAPP_API_KEY
        },
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code >= 400:
        raise Exception(f"WhatsApp send failed: {response.status_code} {response.text[:500]}")

    return True


def process(row):
    reqno = row["reqno"]
    reqid = row["reqid"]
    phone = row.get("phoneno")

    delivery = get_delivery_status(reqno)

    if delivery["status"] in ["S", "L"]:
        return

    report_status = get_report_status(reqno)["overall_status"]

    if report_status == "PARTIAL_REPORT":
        update_delivery_status(
            reqno,
            "P",
            "WHATSAPP",
            "PARTIAL REPORT"
        )
        return

    if report_status != "FULL_REPORT":
        return

    update_delivery_status(
        reqno,
        "L",
        "ENGINE",
        "PROCESSING"
    )

    if not verify_report_download(reqid):
        update_delivery_status(
            reqno,
            "F",
            "WHATSAPP",
            "DOWNLOAD FAILED"
        )
        return

    try:
        send_whatsapp(phone, reqid, reqno)
        update_delivery_status(
            reqno,
            "S",
            "WHATSAPP",
            "OK"
        )
    except Exception as exc:
        print(f"WhatsApp send error for {reqno}: {exc}")
        update_delivery_status(
            reqno,
            "F",
            "WHATSAPP",
            "WHATSAPP FAILED"
        )


def run():
    print("Delivery engine started")

    while True:
        today = datetime.date.today().isoformat()
        data = get_requisitions_by_date(today)
        rows = data["requisitions"]

        for row in rows:
            try:
                process(row)
            except Exception as exc:
                print(f"Delivery engine error for {row.get('reqno')}: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
