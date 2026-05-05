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
)
from app.dispatch_context import build_dispatch_context

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
POLL_INTERVAL_SECONDS = int(os.environ.get("DELIVERY_ENGINE_POLL_SECONDS", "300"))

# in-memory process guards (per worker process lifecycle)
OUTSOURCED_ABSENT_CACHE = {}
SENT_OUTSOURCED_KEYS = set()


def _truthy(v):
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


TEST_MODE = _truthy(os.environ.get("DISPATCH_TEST_MODE", "0"))
DISPATCH_PAUSED = _truthy(os.environ.get("DISPATCH_PAUSED", "1")) if TEST_MODE else False
DISPATCH_TEST_UPDATE_STATUS = _truthy(os.environ.get("DISPATCH_TEST_UPDATE_STATUS", "0"))
DISPATCH_TEST_REQNOS = {
    x.strip() for x in str(os.environ.get("DISPATCH_TEST_REQNOS", "")).split(",") if x.strip()
}

ANSI = {
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def clog(msg, color="reset"):
    code = ANSI.get(color, ANSI["reset"])
    print(f"{code}{msg}{ANSI['reset']}")


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


def get_outsourced_report_url(reqid, testid):
    return f"{REPORT_PUBLIC_BASE}/outsourced-report?reqid={reqid}&testid={testid}"


def verify_pdf_download(url):
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    return response.status_code == 200 and "application/pdf" in response.headers.get("Content-Type", "")


def build_template_payload(destination, document_url, filename):
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
                                "link": document_url,
                                "filename": filename,
                            },
                        }
                    ],
                }
            ],
        },
    }


def _safe_update_status(reqno, status, channel, message):
    if TEST_MODE and not DISPATCH_TEST_UPDATE_STATUS:
        clog(f"[TEST][STATUS-SKIP] {reqno} {status} {channel} {message}", "gray")
        return
    update_delivery_status(reqno, status, channel, message)


def send_whatsapp(phone, reqid, reqno, document_url=None, filename=None):
    if not WHATSAPP_ENDPOINT:
        raise Exception("whatsapp_endpoint is not configured")
    if not WHATSAPP_API_KEY:
        raise Exception("whatsapp_api_key is not configured")
    if not SEND_REPORTS_TEMPLATE:
        raise Exception("send_reports_template is not configured")

    destination = resolve_destination_phone(phone)
    resolved_url = str(document_url or "").strip() or get_report_url(reqid)
    resolved_filename = str(filename or "").strip() or f"{reqid}.pdf"

    payload = build_template_payload(destination, resolved_url, resolved_filename)
    clog(f"[SEND] reqno={reqno} reqid={reqid} to={destination}", "cyan")
    clog(f"[SEND] url={resolved_url}", "cyan")

    if TEST_MODE and DISPATCH_PAUSED:
        clog(f"[TEST][PAUSED] Would send: {resolved_filename}", "yellow")
        return

    response = requests.post(
        WHATSAPP_ENDPOINT,
        headers={"Content-Type": "application/json", "X-API-KEY": WHATSAPP_API_KEY},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 400:
        raise Exception(f"WhatsApp send failed: {response.status_code} {response.text[:500]}")


def _send_normal_if_ready(reqno, reqid, phone, overall_status, delivery_status):
    if delivery_status in {"S", "L"}:
        return
    if overall_status == "PARTIAL_REPORT":
        _safe_update_status(reqno, "P", "WHATSAPP", "PARTIAL REPORT")
        return
    if overall_status != "FULL_REPORT":
        return

    report_url = get_report_url(reqid)
    if not verify_pdf_download(report_url):
        _safe_update_status(reqno, "F", "WHATSAPP", "DOWNLOAD FAILED")
        return

    _safe_update_status(reqno, "L", "ENGINE", "PROCESSING NORMAL")
    try:
        send_whatsapp(phone, reqid, reqno, document_url=report_url, filename=f"{reqid}.pdf")
        _safe_update_status(reqno, "S", "WHATSAPP", "OK NORMAL")
    except Exception as exc:
        clog(f"[ERROR] normal send {reqno}: {exc}", "red")
        _safe_update_status(reqno, "F", "WHATSAPP", "WHATSAPP FAILED")


def _send_outsourced_actions(reqno, reqid, phone, context):
    plan = context.get("send_plan") if isinstance(context, dict) else {}
    if not isinstance(plan, dict):
        return

    latest = str(context.get("latest_approved_at") or "").strip() or "-"
    outsourced_present = bool(plan.get("outsourced_present"))
    if not outsourced_present:
        OUTSOURCED_ABSENT_CACHE[reqno] = latest
        return

    actions = plan.get("outsourced_actions") if isinstance(plan.get("outsourced_actions"), list) else []
    for action in actions:
        if not isinstance(action, dict):
            continue
        testid = str(action.get("testid") or "").strip()
        mode = str(action.get("outsourced_mode") or "").strip().lower()
        if not testid:
            continue
        if mode == "transcribed":
            clog(f"[OUTSOURCED][SKIP] transcribed reqno={reqno} testid={testid}", "blue")
            continue

        key = f"{reqno}:{testid}:{mode}"
        if key in SENT_OUTSOURCED_KEYS:
            clog(f"[OUTSOURCED][SKIP] duplicate key={key}", "gray")
            continue

        report_url = get_outsourced_report_url(reqid, testid)
        dispatch_kind = f"OUTSOURCED_{mode.upper()}" if mode else "OUTSOURCED"
        if not verify_pdf_download(report_url):
            _safe_update_status(reqno, "F", "ENGINE", f"OUTSOURCED FLAGGED {testid}")
            continue

        _safe_update_status(reqno, "L", "ENGINE", f"PROCESSING {dispatch_kind} {testid}")
        try:
            send_whatsapp(phone, reqid, reqno, document_url=report_url, filename=f"OUTSOURCED_{reqid}_{testid}.pdf")
            SENT_OUTSOURCED_KEYS.add(key)
            _safe_update_status(reqno, "S", "WHATSAPP", f"OK {dispatch_kind} {testid}")
            clog(f"[OUTSOURCED][OK] reqno={reqno} testid={testid} mode={mode}", "green")
        except Exception as exc:
            clog(f"[OUTSOURCED][ERROR] reqno={reqno} testid={testid}: {exc}", "red")
            _safe_update_status(reqno, "F", "WHATSAPP", f"WHATSAPP FAILED {testid}")


def process(row):
    reqno = row["reqno"]
    reqid = row["reqid"]
    phone = row.get("phoneno")

    if TEST_MODE and DISPATCH_TEST_REQNOS and reqno not in DISPATCH_TEST_REQNOS:
        return

    delivery = get_delivery_status(reqno)
    delivery_status = str(delivery.get("status") or "").strip().upper()

    context = build_dispatch_context(reqno)
    overall = str(context.get("overall_status") or "").strip().upper()

    if TEST_MODE:
        clog(f"[TEST][CTX] reqno={reqno} overall={overall} delivery={delivery_status}", "magenta")
        sp = context.get("send_plan") or {}
        clog(f"[TEST][PLAN] counts={sp.get('counts')} outsourced_present={sp.get('outsourced_present')}", "magenta")

    _send_normal_if_ready(reqno, reqid, phone, overall, delivery_status)
    _send_outsourced_actions(reqno, reqid, phone, context)


def run():
    if TEST_MODE:
        clog("[MODE] DISPATCH TEST MODE ENABLED", "yellow")
        clog(f"[MODE] DISPATCH_PAUSED={DISPATCH_PAUSED}", "yellow")
        clog(f"[MODE] TEST_REQNOS={sorted(list(DISPATCH_TEST_REQNOS))}", "yellow")
        clog(f"[MODE] DEFAULT_PHONE={DEFAULT_PHONE or '(none)'}", "yellow")
        clog(f"[MODE] TEST_UPDATE_STATUS={DISPATCH_TEST_UPDATE_STATUS}", "yellow")
    else:
        clog("[MODE] LIVE MODE", "green")

    clog("Delivery engine started", "cyan")
    while True:
        today = datetime.date.today().isoformat()
        data = get_requisitions_by_date(today)
        rows = data["requisitions"]

        for row in rows:
            try:
                process(row)
            except Exception as exc:
                clog(f"Delivery engine error for {row.get('reqno')}: {exc}", "red")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
