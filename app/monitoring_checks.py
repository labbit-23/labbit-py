import json
import os
import socket
import time
from datetime import datetime, timezone

import requests

try:
    from pysnmp.hlapi import getCmd, ObjectType, ObjectIdentity, SnmpEngine, CommunityData, UdpTransportTarget, ContextData
    SNMP_AVAILABLE = True
except ImportError:
    SNMP_AVAILABLE = False


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


def run_snmp_sophos_check(section_name, cfg, default_timeout):
    """Poll Sophos XG firewall via SNMP for WAN status and traffic."""
    result = _base_result(section_name, cfg)
    started_at = time.perf_counter()

    if not SNMP_AVAILABLE:
        return _finalize(result, started_at, "down", "pysnmp not installed", {})

    host = cfg.get("host", "").strip()
    community = cfg.get("community", "").strip()
    version = cfg.get("version", "2c").strip()
    timeout = float(cfg.get("timeout_seconds", default_timeout))
    port = int(cfg.get("port", "161"))

    if not host or not community:
        return _finalize(result, started_at, "down", "Missing host or community", {})

    try:
        engine = SnmpEngine()

        # Fetch system info
        sys_descr = _snmp_get(engine, host, community, version, port, timeout, "1.3.6.1.2.1.1.1.0")
        sys_uptime = _snmp_get(engine, host, community, version, port, timeout, "1.3.6.1.2.1.1.3.0")

        # Fetch interface names and status
        if_descr_tree = _snmp_walk(engine, host, community, version, port, timeout, "1.3.6.1.2.1.2.2.1.2")
        if_status_tree = _snmp_walk(engine, host, community, version, port, timeout, "1.3.6.1.2.1.2.2.1.8")
        if_hc_in = _snmp_walk(engine, host, community, version, port, timeout, "1.3.6.1.2.1.31.1.1.1.6")
        if_hc_out = _snmp_walk(engine, host, community, version, port, timeout, "1.3.6.1.2.1.31.1.1.1.10")

        # Parse uptime
        uptime_str = _format_uptime(sys_uptime) if sys_uptime else "unknown"

        # Build WAN interfaces
        wans = _build_wan_interfaces(if_descr_tree, if_status_tree, if_hc_in, if_hc_out)

        payload = {
            "firewall": {
                "name": cfg.get("label", "SDRC Sophos XG106w"),
                "host": host,
                "reachable": True,
                "uptime": uptime_str,
                "mode": cfg.get("mode", "multilink_load_balance")
            },
            "wans": wans,
            "timestamp": utc_now_iso()
        }

        # Determine status
        wan_status = _derive_wan_status(wans)
        status = "healthy" if wan_status == "ok" else ("degraded" if wan_status == "warning" else "down")
        message = f"Firewall up, WANs: {wan_status}"

        return _finalize(result, started_at, status, message, payload)
    except Exception as exc:
        return _finalize(result, started_at, "down", f"SNMP error: {str(exc)[:100]}", {"host": host})


def _snmp_get(engine, host, community, version, port, timeout, oid):
    """Fetch single SNMP OID."""
    try:
        errorIndication, errorStatus, errorIndex, varBinds = next(
            getCmd(
                engine,
                CommunityData(community, mpModel=0 if version == "1" else 1),
                UdpTransportTarget((host, port), timeout=timeout),
                ContextData(),
                ObjectType(ObjectIdentity(oid))
            )
        )
        if errorIndication or not varBinds:
            return None
        return varBinds[0][1]
    except Exception:
        return None


def _snmp_walk(engine, host, community, version, port, timeout, oid_base):
    """Walk SNMP subtree."""
    try:
        result = {}
        for errorIndication, errorStatus, errorIndex, varBinds in engine.bulkCmd(
            CommunityData(community, mpModel=0 if version == "1" else 1),
            UdpTransportTarget((host, port), timeout=timeout),
            ContextData(),
            0, 25,
            ObjectType(ObjectIdentity(oid_base))
        ):
            if errorIndication:
                break
            for oid, val in varBinds:
                result[str(oid)] = val
        return result
    except Exception:
        return {}


def _format_uptime(uptime_val):
    """Convert SNMP uptime (ticks) to readable format."""
    try:
        ticks = int(uptime_val)
        seconds = ticks // 100
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    except:
        return str(uptime_val)


def _build_wan_interfaces(if_descr_tree, if_status_tree, if_hc_in, if_hc_out):
    """Extract Port2 and Port4 from SNMP interface tree."""
    wans = []
    wan_map = {}

    # Build name -> status mapping
    for oid_str, name_val in if_descr_tree.items():
        name = str(name_val).strip()
        if name in {"Port2", "Port4"}:
            idx = oid_str.split(".")[-1]
            wan_map[name] = {"index": idx, "link_up": False}

    # Add status info
    for oid_str, status_val in if_status_tree.items():
        idx = oid_str.split(".")[-1]
        status_code = int(status_val) if status_val else 0
        for name, info in wan_map.items():
            if info["index"] == idx:
                info["link_up"] = (status_code == 1)

    # Add traffic info
    for oid_str, bytes_val in if_hc_in.items():
        idx = oid_str.split(".")[-1]
        for name, info in wan_map.items():
            if info["index"] == idx:
                info["rx_bytes"] = int(bytes_val) if bytes_val else 0

    for oid_str, bytes_val in if_hc_out.items():
        idx = oid_str.split(".")[-1]
        for name, info in wan_map.items():
            if info["index"] == idx:
                info["tx_bytes"] = int(bytes_val) if bytes_val else 0

    # Build output for Port2 and Port4
    wan_config = {
        "Port2": {"ip": "192.168.1.75", "gateway": "192.168.1.1"},
        "Port4": {"ip": "192.168.37.6", "gateway": "192.168.37.1"}
    }

    for name, config in wan_config.items():
        info = wan_map.get(name, {})
        wans.append({
            "name": f"WAN {name}",
            "interface": name,
            "ip": config["ip"],
            "gateway": config["gateway"],
            "link_up": info.get("link_up", False),
            "internet_reachable": info.get("link_up", False),
            "latency_ms": None,
            "rx_bytes": info.get("rx_bytes", 0),
            "tx_bytes": info.get("tx_bytes", 0)
        })

    return wans


def _derive_wan_status(wans):
    """Determine overall WAN status."""
    if not wans:
        return "critical"
    up_count = sum(1 for w in wans if w.get("link_up"))
    if up_count == len(wans):
        return "ok"
    elif up_count > 0:
        return "warning"
    else:
        return "critical"


def run_check(section_name, cfg, default_timeout):
    check_type = cfg.get("type", "http_json").strip().lower()

    if check_type in {"http_json", "http_json_auth"}:
        return run_http_check(section_name, cfg, default_timeout)
    if check_type == "tcp":
        return run_tcp_check(section_name, cfg, default_timeout)
    if check_type == "heartbeat_file":
        return run_heartbeat_check(section_name, cfg, default_timeout)
    if check_type == "snmp_sophos":
        return run_snmp_sophos_check(section_name, cfg, default_timeout)

    result = _base_result(section_name, cfg)
    return _finalize(result, time.perf_counter(), "unknown", f"Unsupported check type: {check_type}")
