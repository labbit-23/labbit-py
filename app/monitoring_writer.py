import requests


class MonitoringWriter:
    def __init__(self, ingest_url, ingest_token, timeout_seconds=8):
        self.ingest_url = ingest_url
        self.ingest_token = ingest_token
        self.timeout_seconds = timeout_seconds

    def send(self, payload):
        headers = {
            "Authorization": f"Bearer {self.ingest_token}",
            "Content-Type": "application/json",
        }
        response = requests.post(
            self.ingest_url,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
