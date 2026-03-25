import requests
import configparser
import json
from pathlib import Path

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

GET_REQ = config["api"]["getrequisitionsbydateapi"]
UPDATE = config["api"]["updatedeliverystatusapi"]
GET_STATUS = config["api"]["getdeliverystatusapi"]


print("\n--- TEST 1: GET REQUISITIONS ---")

payload = {
    "date": "2026-03-13"
}

r = requests.post(GET_REQ, json=payload)

print("HTTP:", r.status_code)
print(json.dumps(r.json(), indent=2))


print("\n--- TEST 2: GET DELIVERY STATUS ---")

payload = {
    "reqno": "20260311093"
}

r = requests.post(GET_STATUS, json=payload)

print("HTTP:", r.status_code)
print(json.dumps(r.json(), indent=2))


print("\n--- TEST 3: UPDATE DELIVERY STATUS ---")

payload = {
    "reqno": "20260311093",
    "status": "N",
    "channel": "TEST",
    "message": "RESET FROM PYTHON"
}

r = requests.post(UPDATE, json=payload)

print("HTTP:", r.status_code)
print(json.dumps(r.json(), indent=2))
