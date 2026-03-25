import configparser
import json
from pathlib import Path

import requests

from app.report_status import fetch_report_status

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

GET_REQ_API = config["api"]["getrequisitionsbydateapi"]
UPDATE_STATUS_API = config["api"]["updatedeliverystatusapi"]
GET_DELIVERY_STATUS_API = config["api"]["getdeliverystatusapi"]

CHANNEL_CODES = {
    "UNKNOWN": 0,
    "WHATSAPP": 1,
    "SMS": 2,
    "EMAIL": 3,
    "ENGINE": 4,
    "TEST": 9,
}

MESSAGE_CODES = {
    "UNKNOWN": 0,
    "OK": 100,
    "PARTIAL REPORT": 101,
    "DOWNLOAD FAILED": 102,
    "WHATSAPP FAILED": 103,
    "PROCESSING": 104,
    "RESET FROM API": 105,
    "RESET FROM PYTHON": 106,
}

CODE_TO_CHANNEL = {str(value): key for key, value in CHANNEL_CODES.items()}
CODE_TO_MESSAGE = {str(value): key for key, value in MESSAGE_CODES.items()}


def _call_tapi_query(api_url, payload):
    encoded_payload = json.dumps([payload])

    try:
        response = requests.get(
            api_url,
            params={"data": encoded_payload},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise Exception(
            f"HTTP request failed for {api_url}: {exc}. "
            f"Payload={encoded_payload}"
        ) from exc

    if response.status_code != 200:
        raise Exception(
            f"TApiQuery failed for {api_url} with status {response.status_code}. "
            f"Payload={encoded_payload} Body={response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise Exception(
            f"Invalid JSON from {api_url}. "
            f"Payload={encoded_payload} Body={response.text[:500]}"
        ) from exc


def _unwrap_rows(data):
    if isinstance(data, dict) and "data" in data:
        return data["data"]

    return data


def _stringify_number(value):
    if value is None:
        return ""

    return str(value).strip()


def _encode_delivery_update(reqno, status, channel, message):
    channel_name = str(channel).strip().upper()
    message_text = str(message).strip().upper()
    status_text = str(status).strip().upper()

    channel_code = CHANNEL_CODES.get(channel_name, CHANNEL_CODES["UNKNOWN"])
    message_code = MESSAGE_CODES.get(message_text, MESSAGE_CODES["UNKNOWN"])

    status_payload = f"{status_text}|CH={channel_name}|MSG={message_text}"

    return {
        "status": status_payload,
        "channel": channel_code,
        "message": message_code,
    }


def _decode_delivery_row(reqno, row):
    raw_status = row.get("STATUS", row.get("status", ""))
    raw_channel = _stringify_number(row.get("CHANNEL", row.get("channel")))
    raw_message = _stringify_number(row.get("MESSAGE", row.get("message")))

    decoded = {
        "reqno": str(reqno),
        "status": raw_status,
        "channel": CODE_TO_CHANNEL.get(raw_channel, raw_channel),
        "message": CODE_TO_MESSAGE.get(raw_message, raw_message),
        "edituserid": row.get("EDITUSERID", row.get("edituserid")),
        "delivery_date": row.get("DELIVERY_DATE", row.get("delivery_date")),
        "raw": row,
    }

    if isinstance(raw_status, str):
        parts = [part.strip() for part in raw_status.split("|") if part.strip()]

        if parts:
            decoded["status_payload"] = raw_status
            decoded["status"] = parts[0]

            for part in parts[1:]:
                if part.startswith("CH="):
                    channel_value = part.split("=", 1)[1].strip()
                    decoded["channel"] = channel_value
                elif part.startswith("MSG="):
                    message_value = part.split("=", 1)[1].strip()
                    decoded["message"] = message_value

    return decoded


def fetch_requisitions_by_date(date):
    rows = _unwrap_rows(_call_tapi_query(GET_REQ_API, {"reqdate": date}))

    if not isinstance(rows, list):
        raise Exception(f"Unexpected requisitions response: {rows}")

    requisitions = []

    for row in rows:
        requisitions.append({
            "reqno": row.get("REQNO", row.get("reqno")),
            "reqid": row.get("REQID", row.get("reqid")),
            "mrno": row.get("MRNO", row.get("mrno")),
            "patient_name": row.get("PATIENTNM", row.get("patient_name")),
            "phoneno": row.get("PHONENO", row.get("phoneno"))
        })

    return {
        "date": date,
        "requisitions": requisitions
    }


def fetch_delivery_status(reqno):
    rows = _unwrap_rows(_call_tapi_query(GET_DELIVERY_STATUS_API, {"reqno": reqno}))

    if isinstance(rows, list):
        latest = rows[0] if rows else {}
    elif isinstance(rows, dict):
        latest = rows
        rows = [rows]
    else:
        raise Exception(f"Unexpected delivery status response: {rows}")

    decoded_rows = [_decode_delivery_row(reqno, row) for row in rows]
    latest_decoded = decoded_rows[0] if decoded_rows else {
        "reqno": str(reqno),
        "status": "",
        "channel": "",
        "message": "",
        "edituserid": "",
        "delivery_date": "",
        "raw": latest,
    }

    return {
        "reqno": latest_decoded["reqno"],
        "status": latest_decoded["status"],
        "channel": latest_decoded["channel"],
        "message": latest_decoded["message"],
        "edituserid": latest_decoded["edituserid"],
        "delivery_date": latest_decoded["delivery_date"],
        "row": latest_decoded,
        "rows": decoded_rows,
    }
def fetch_update_delivery_status(reqno, status, channel, message):
    encoded = _encode_delivery_update(reqno, status, channel, message)
    rows = _unwrap_rows(_call_tapi_query(
        UPDATE_STATUS_API,
        {
            "reqno": reqno,
            "status": encoded["status"],
            "channel": encoded["channel"],
            "message": encoded["message"]
        }
    ))

    return {
        "reqno": reqno,
        "status": status,
        "channel": channel,
        "message": message,
        "stored_as": encoded,
        "backend_result": rows
    }


def get_requisitions_by_date(date):
    return fetch_requisitions_by_date(date)


def get_delivery_status(reqno):
    return fetch_delivery_status(reqno)


def update_delivery_status(reqno, status, channel, message):
    return fetch_update_delivery_status(reqno, status, channel, message)


def get_report_status(reqno):
    return fetch_report_status(reqno)
