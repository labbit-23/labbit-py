import json
import os
import socket
import time
from datetime import datetime, timezone

import requests


VALID_STATUSES = {"healthy", "degraded", "down", "unknown"}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


class CheckError(Exception):
    pass


def _base_result(section_name, cfg):
    return {
        "service_key": section_name.split(":", 1)[-1],
        "category": cfg.get("category", "other"),
        "label": cfg.get("label", section_name),
        "status": "unknown",
        "latency_ms": None,
        "message": None,
        "payload": {},
        "checked_at": utc_now_iso(),
    }


def _finalize(result, started_at, status, message, payload=None):
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    result["latency_ms"] = latency_ms
    result["status"] = status if status in VALID_STATUSES else "unknown"
    result["message"] = message
    result["payload"] = payload or {}
    return result


def run_http_check(section_name, cfg, default_timeout):
    result = _base_result(section_name, cfg)
    started_at = time.perf_counter()
    timeout = float(cfg.get("timeout_seconds", default_timeout))
    method = cfg.get("method", "GET").upper()
    expected_status = int(cfg.get("expected_status", "200"))
    slow_threshold_ms = int(cfg.get("slow_threshold_ms", "0"))
    url = cfg.get("url", "").strip()
    verify_ssl = cfg.get("verify_ssl", "1").strip().lower() not in {"0", "false", "no", "off"}
    headers = {}

    if not url:
        return _finalize(result, started_at, "unknown", "Missing URL")

    for key, value in cfg.items():
        if key.startswith("header_"):
            header_name = key[len("header_"):].replace("_", "-")
            headers[header_name] = value

    auth = None
    if cfg.get("type") == "http_json_auth":
        auth = (cfg.get("username", ""), cfg.get("password", ""))

    try:
        response = requests.request(method, url, timeout=timeout, auth=auth, headers=headers or None, verify=verify_ssl)
        payload = {
            "url": url,
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "verify_ssl": verify_ssl,
        }
        if headers:
            payload["headers"] = headers
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        status = "healthy"
        message = f"HTTP {response.status_code}"

        if response.status_code != expected_status:
            status = "down"
            message = f"Expected {expected_status}, got {response.status_code}"
        elif slow_threshold_ms and latency_ms > slow_threshold_ms:
            status = "degraded"
            message = f"Slow response ({latency_ms} ms)"

        return _finalize(result, started_at, status, message, payload)
    except requests.Timeout:
        return _finalize(result, started_at, "down", f"Timed out after {timeout}s", {"url": url})
    except Exception as exc:
        return _finalize(result, started_at, "down", str(exc), {"url": url})


def run_tcp_check(section_name, cfg, default_timeout):
    result = _base_result(section_name, cfg)
    started_at = time.perf_counter()
    host = cfg.get("host", "").strip()
    port = int(cfg.get("port", "0"))
    timeout = float(cfg.get("timeout_seconds", default_timeout))
    slow_threshold_ms = int(cfg.get("slow_threshold_ms", "0"))

    if not host or not port:
        return _finalize(result, started_at, "unknown", "Missing host or port")

    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            status = "healthy"
            message = "TCP connection successful"
            if slow_threshold_ms and latency_ms > slow_threshold_ms:
                status = "degraded"
                message = f"Slow TCP connect ({latency_ms} ms)"
            return _finalize(result, started_at, status, message, {"host": host, "port": port})
    except Exception as exc:
        return _finalize(result, started_at, "down", str(exc), {"host": host, "port": port})


def run_heartbeat_check(section_name, cfg, _default_timeout):
    result = _base_result(section_name, cfg)
    started_at = time.perf_counter()
    path = cfg.get("path", "").strip()
    max_age_seconds = int(cfg.get("max_age_seconds", "300"))

    if not path:
        return _finalize(result, started_at, "unknown", "Missing heartbeat file path")

    if not os.path.exists(path):
        return _finalize(result, started_at, "down", "Heartbeat file not found", {"path": path})

    try:
        stat = os.stat(path)
        age_seconds = int(time.time() - stat.st_mtime)
        payload = {"path": path, "age_seconds": age_seconds}

        if age_seconds > max_age_seconds:
            return _finalize(result, started_at, "down", f"Heartbeat too old ({age_seconds}s)", payload)

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload["content"] = json.load(handle)
        except Exception:
            payload["content"] = None

        status = "healthy"
        message = f"Heartbeat age {age_seconds}s"
        if age_seconds > max(30, max_age_seconds // 2):
            status = "degraded"
            message = f"Heartbeat aging ({age_seconds}s)"

        return _finalize(result, started_at, status, message, payload)
    except Exception as exc:
        return _finalize(result, started_at, "down", str(exc), {"path": path})


def run_check(section_name, cfg, default_timeout):
    check_type = cfg.get("type", "http_json").strip().lower()

    if check_type in {"http_json", "http_json_auth"}:
        return run_http_check(section_name, cfg, default_timeout)
    if check_type == "tcp":
        return run_tcp_check(section_name, cfg, default_timeout)
    if check_type == "heartbeat_file":
        return run_heartbeat_check(section_name, cfg, default_timeout)

    result = _base_result(section_name, cfg)
    return _finalize(result, time.perf_counter(), "unknown", f"Unsupported check type: {check_type}")
