import configparser
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from app.monitoring_checks import run_check
from app.monitoring_writer import MonitoringWriter

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICES_CONFIG_PATH = os.environ.get("MONITORING_SERVICES_INI", str(ROOT_DIR / "services.ini"))
LOG_PATH = os.environ.get("MONITORING_LOG_PATH", str(ROOT_DIR / "logs" / "monitoring.log"))
ALERT_STATE_PATH = os.environ.get("MONITORING_ALERT_STATE_PATH", str(ROOT_DIR / "logs" / "wan_alert_state.json"))
HEALTH_STATE_PATH = os.environ.get("MONITORING_HEALTH_STATE_PATH", str(ROOT_DIR / "logs" / "local_health.json"))
HEALTH_HOST = os.environ.get("MONITORING_HEALTH_HOST", "127.0.0.1")
HEALTH_PORT = int(os.environ.get("MONITORING_HEALTH_PORT", "8090"))

IST = timezone(timedelta(hours=5, minutes=30))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def _read_meminfo():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, raw = line.partition(":")
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1)
    except (FileNotFoundError, ValueError):
        pass
    return values


def _machine_snapshot():
    mem = _read_meminfo()
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
    except (FileNotFoundError, IndexError, ValueError):
        uptime_seconds = None

    disk = shutil.disk_usage("/")
    disk_used_pct = round((disk.total - disk.free) * 100 / disk.total, 1) if disk.total else None
    mem_total = mem.get("MemTotal")
    mem_available = mem.get("MemAvailable", mem.get("MemFree"))
    mem_used_pct = round((mem_total - mem_available) * 100 / mem_total, 1) if mem_total and mem_available is not None else None
    swap_total = mem.get("SwapTotal")
    swap_free = mem.get("SwapFree")
    swap_used_pct = round((swap_total - swap_free) * 100 / swap_total, 1) if swap_total and swap_free is not None else 0.0

    pm2 = []
    try:
        result = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0:
            for item in json.loads(result.stdout or "[]"):
                env = item.get("pm2_env") or {}
                pm_uptime = env.get("pm_uptime")
                pm2.append({
                    "name": item.get("name"),
                    "status": env.get("status", "unknown"),
                    "restarts": env.get("restart_time", 0),
                    "uptime_seconds": round(max(0, time.time() * 1000 - float(pm_uptime)) / 1000, 1) if isinstance(pm_uptime, (int, float)) else None,
                })
        else:
            pm2 = [{"status": "unknown", "error": (result.stderr or "pm2 jlist failed")[:200]}]
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        pm2 = [{"status": "unknown", "error": str(exc)[:200]}]

    systemd = {}
    for unit in ("nginx", "tailscaled", "anydesk"):
        try:
            result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=3, check=False)
            systemd[unit] = result.stdout.strip() or "unknown"
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            systemd[unit] = f"unknown: {exc}"[:80]

    machine_status = "healthy"
    if ((disk_used_pct is not None and disk_used_pct >= 95) or
            (mem_used_pct is not None and mem_used_pct >= 95)):
        machine_status = "down"
    elif ((disk_used_pct is not None and disk_used_pct >= 90) or
            (mem_used_pct is not None and mem_used_pct >= 90) or swap_used_pct >= 90):
        machine_status = "degraded"

    pm2_statuses = [str(item.get("status", "unknown")).lower() for item in pm2]
    if any(status in {"errored", "stopped", "stopping"} for status in pm2_statuses):
        machine_status = "down"
    elif any(status not in {"online"} for status in pm2_statuses):
        machine_status = "degraded"
    if systemd.get("nginx") != "active" or systemd.get("tailscaled") != "active":
        machine_status = "down"
    elif systemd.get("anydesk") not in {"active", "unknown"} and machine_status == "healthy":
        machine_status = "degraded"

    return {
        "status": machine_status,
        "hostname": socket.gethostname(),
        "uptime_seconds": round(uptime_seconds, 1) if uptime_seconds is not None else None,
        "load_average": [round(value, 2) for value in os.getloadavg()],
        "memory_used_percent": mem_used_pct,
        "swap_used_percent": swap_used_pct,
        "disk_root_used_percent": disk_used_pct,
        "pm2": pm2,
        "systemd": systemd,
    }


def _sanitize_service(row):
    return {
        "service_key": row.get("service_key"),
        "category": row.get("category"),
        "label": row.get("label"),
        "status": row.get("status", "unknown"),
        "latency_ms": row.get("latency_ms"),
        "message": row.get("message"),
        "checked_at": row.get("checked_at"),
    }


def _overall_health(services, machine, ingest_status):
    statuses = [str(row.get("status", "unknown")).lower() for row in services]
    statuses.append(str(machine.get("status", "unknown")).lower())
    statuses.append(str(ingest_status or "unknown").lower())
    if "down" in statuses:
        return "down"
    if "degraded" in statuses or "unknown" in statuses or not services:
        return "degraded" if services else "unknown"
    return "healthy"


def _write_health_snapshot(payload=None, ingest_status="unknown", ingest_error=None):
    payload = payload or {}
    previous_services = {}
    try:
        previous = json.loads(Path(HEALTH_STATE_PATH).read_text())
        previous_services = {
            row.get("service_key"): row
            for row in previous.get("services", [])
            if row.get("service_key")
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    for row in payload.get("services", []):
        clean = _sanitize_service(row)
        if clean.get("service_key"):
            previous_services[clean["service_key"]] = clean

    machine = _machine_snapshot()
    snapshot = {
        "status": _overall_health(list(previous_services.values()), machine, ingest_status),
        "checked_at": utc_now_iso(),
        "lab_id": payload.get("lab_id", ""),
        "source": payload.get("source", "local-monitoring"),
        "machine": machine,
        "services": sorted(
            previous_services.values(),
            key=lambda row: (row.get("category") or "", row.get("label") or ""),
        ),
        "monitoring_uplink": {"status": ingest_status},
    }
    if ingest_error:
        snapshot["monitoring_uplink"]["error"] = ingest_error[:300]

    target = Path(HEALTH_STATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, separators=(",", ":")))
    temporary.replace(target)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        try:
            body = Path(HEALTH_STATE_PATH).read_bytes()
        except (FileNotFoundError, OSError):
            body = json.dumps({
                "status": "unknown",
                "message": "Health snapshot not available yet",
            }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer((HEALTH_HOST, HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="local-health", daemon=True)
    thread.start()
    logging.info("Local health endpoint listening on http://%s:%s/health", HEALTH_HOST, HEALTH_PORT)
    return server


def _load_alert_state():
    try:
        with open(ALERT_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_alert_state(state):
    os.makedirs(os.path.dirname(ALERT_STATE_PATH) or ".", exist_ok=True)
    with open(ALERT_STATE_PATH, "w") as f:
        json.dump(state, f)


def _send_ops_alert(monitoring_cfg, category, subject, status):
    """POST to labit-main's staff-notify using the generic ops_status_alert
    WhatsApp template (Category/Subject/Status/Time), approved 2026-09
    specifically to be reused across alert types -- not WAN-specific despite
    this module's only caller today being the Sophos check.
    """
    notify_url = monitoring_cfg.get("alert_notify_url", "").strip()
    notify_token = monitoring_cfg.get("alert_notify_token", "").strip()
    if not notify_url or not notify_token:
        logging.warning("Ops alert skipped for %s: %s (%s): alert_notify_url/alert_notify_token not configured", category, subject, status)
        return

    template_name = monitoring_cfg.get("alert_template_name", "ops_status_alert").strip()
    language_code = monitoring_cfg.get("alert_language_code", "en").strip()
    lab_id = monitoring_cfg.get("alert_lab_id", "").strip() or monitoring_cfg.get("lab_id", "").strip()

    try:
        response = requests.post(
            notify_url,
            json={
                "lab_id": lab_id,
                "template_name": template_name,
                "language_code": language_code,
                "template_params": [category, subject, status, now_ist_str()],
                "source_service": "sophos-wan-monitor",
            },
            headers={"x-internal-token": notify_token},
            timeout=10,
        )
        if response.status_code >= 300:
            logging.error("Ops alert send failed (%s): %s", response.status_code, response.text[:300])
        else:
            logging.info("Ops alert sent: %s / %s is now %s", category, subject, status)
    except Exception as exc:
        logging.exception("Ops alert send raised: %s", exc)


def maybe_alert_wan_transitions(check_result, cfg, monitoring_cfg):
    """Fire a WhatsApp alert when a monitored WAN's link state flips.

    Opt-in per service (alert_on_wan_change=1 in its services.ini section) so
    this only ever fires for checks the operator explicitly wants alerted --
    scoped this way rather than sniffing any payload that happens to contain
    a "wans" key. State survives process restarts via ALERT_STATE_PATH so a
    monitoring_agent restart never fires a false "just went down/up" alert
    for state that hasn't actually changed.
    """
    if str(cfg.get("alert_on_wan_change", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
        return

    payload = check_result.get("payload") or {}
    service_key = check_result.get("service_key", "unknown")
    firewall_label = (payload.get("firewall") or {}).get("name") or check_result.get("label", service_key)
    wans = payload.get("wans")

    state = _load_alert_state()
    changed = False

    if wans:
        for wan in wans:
            wan_key = f"{service_key}:{wan.get('interface') or wan.get('name')}"
            current_up = bool(wan.get("link_up"))
            previous = state.get(wan_key)
            if previous is None:
                state[wan_key] = current_up
                changed = True
                continue
            if previous != current_up:
                _send_ops_alert(monitoring_cfg, "Infrastructure",
                                 f"{wan.get('name', wan_key)} ({firewall_label})",
                                 "RESTORED" if current_up else "DOWN")
                state[wan_key] = current_up
                changed = True
    else:
        # SNMP/firewall itself unreachable -- no per-WAN detail available,
        # but this is exactly the "can't see it at all" case that also
        # deserves a page.
        reach_key = f"{service_key}:__reachable__"
        current_ok = check_result.get("status") == "healthy"
        previous = state.get(reach_key)
        if previous is None:
            state[reach_key] = current_ok
            changed = True
        elif previous != current_ok:
            _send_ops_alert(monitoring_cfg, "Infrastructure",
                             f"{firewall_label} (SNMP unreachable)",
                             "RESTORED" if current_ok else "DOWN")
            state[reach_key] = current_ok
            changed = True

    if changed:
        _save_alert_state(state)


def configure_logging():
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_services_config(path=SERVICES_CONFIG_PATH):
    parser = configparser.ConfigParser()
    found = parser.read(path)
    if not found:
        raise FileNotFoundError(f"Monitoring config not found: {path}")
    if "monitoring" not in parser:
        raise ValueError("Missing [monitoring] section in services.ini")

    for section_name in parser.sections():
        for key, value in list(parser[section_name].items()):
            text = str(value).strip()
            if text.startswith("ENV:"):
                env_key = text.split(":", 1)[1].strip()
                parser[section_name][key] = os.environ.get(env_key, "")

    return parser


def should_run_entry(cfg, node_role):
    # Safety-first mode: if role or run_on is missing, skip the entry.
    run_on = str(cfg.get("run_on", "") or "").strip().lower()
    current_role = str(node_role or "").strip().lower()

    if not current_role:
        return False
    if not run_on:
        return False

    if run_on in {"both", "all", "any"}:
        return True
    if run_on == "vps":
        return current_role == "vps"
    if run_on == "local":
        return current_role == "local"

    # Unknown value => skip to avoid accidental duplicate writes.
    return False


def is_in_scheduled_downtime(cfg):
    """Check if service is in its scheduled downtime window (IST timezone)"""
    scheduled = str(cfg.get("scheduled_downtime", "") or "").strip()
    if not scheduled or scheduled.lower() in {"", "none", "false"}:
        return False

    try:
        # Format: 2100-0800 (start_hhmm-end_hhmm)
        parts = scheduled.split("-")
        if len(parts) != 2:
            return False

        start_hhmm = int(parts[0].strip())
        end_hhmm = int(parts[1].strip())
    except (ValueError, AttributeError):
        return False

    # Get current time in IST
    now_ist = datetime.now(timezone.utc).astimezone()
    current_hhmm = now_ist.hour * 100 + now_ist.minute

    # Handle window crossing midnight (e.g., 21:00 to 08:00)
    if start_hhmm > end_hhmm:
        return current_hhmm >= start_hhmm or current_hhmm < end_hhmm
    return current_hhmm >= start_hhmm and current_hhmm < end_hhmm


def append_node_role_to_service_key(service_row, node_role):
    row = dict(service_row or {})
    base_key = str(row.get("service_key", "") or "").strip()
    role = str(node_role or "").strip().lower()

    if not base_key or not role:
        return row

    suffix = f"__{role}"
    if not base_key.endswith(suffix):
        row["service_key"] = f"{base_key}{suffix}"

    return row


def build_payload(config):
    monitoring_cfg = config["monitoring"]
    default_timeout = float(monitoring_cfg.get("request_timeout_seconds", "8"))
    node_role = monitoring_cfg.get("node_role", "").strip()

    services = []
    for section_name in config.sections():
        if not section_name.startswith("service:"):
            continue
        cfg = config[section_name]
        if cfg.get("enabled", "1").strip() not in {"1", "true", "yes", "on"}:
            continue
        if not should_run_entry(cfg, node_role):
            continue
        check_result = run_check(section_name, cfg, default_timeout)
        maybe_alert_wan_transitions(check_result, cfg, monitoring_cfg)
        services.append(append_node_role_to_service_key(check_result, node_role))

    group_definitions = load_group_definitions(config, node_role)
    if group_definitions:
        services.append(
            append_node_role_to_service_key(
                {
                    "service_key": "__group_config__",
                    "category": "diagnosis",
                    "label": "Dependency Groups",
                    "status": "healthy",
                    "latency_ms": 0,
                    "message": "Dependency groups loaded from services.ini",
                    "payload": {"groups": group_definitions},
                    "checked_at": utc_now_iso(),
                },
                node_role,
            )
        )

    return {
        "lab_id": monitoring_cfg.get("lab_id", "").strip(),
        "source": monitoring_cfg.get("source", "monitoring-agent").strip(),
        "checked_at": utc_now_iso(),
        "services": services,
    }


def build_due_payload(config, last_run_by_service=None, now_monotonic=None):
    monitoring_cfg = config["monitoring"]
    default_timeout = float(monitoring_cfg.get("request_timeout_seconds", "8"))
    node_role = monitoring_cfg.get("node_role", "").strip()
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    last_run_by_service = last_run_by_service or {}

    services = []
    ran_sections = []

    for section_name in config.sections():
        if not section_name.startswith("service:"):
            continue
        cfg = config[section_name]
        if cfg.get("enabled", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            continue
        if not should_run_entry(cfg, node_role):
            continue

        service_interval = int(cfg.get("interval_seconds", monitoring_cfg.get("interval_seconds", "60")))
        last_run = last_run_by_service.get(section_name)
        if last_run is not None and now_monotonic - last_run < service_interval:
            continue

        # Skip checks during scheduled downtime window
        if is_in_scheduled_downtime(cfg):
            continue

        check_result = run_check(section_name, cfg, default_timeout)
        maybe_alert_wan_transitions(check_result, cfg, monitoring_cfg)
        services.append(append_node_role_to_service_key(check_result, node_role))
        ran_sections.append(section_name)

    payload = {
        "lab_id": monitoring_cfg.get("lab_id", "").strip(),
        "source": monitoring_cfg.get("source", "monitoring-agent").strip(),
        "checked_at": utc_now_iso(),
        "services": services,
    }

    group_definitions = load_group_definitions(config, node_role)
    if group_definitions:
        payload["services"].append(
            append_node_role_to_service_key(
                {
                    "service_key": "__group_config__",
                    "category": "diagnosis",
                    "label": "Dependency Groups",
                    "status": "healthy",
                    "latency_ms": 0,
                    "message": "Dependency groups loaded from services.ini",
                    "payload": {"groups": group_definitions},
                    "checked_at": utc_now_iso(),
                },
                node_role,
            )
        )

    return payload, ran_sections


def load_group_definitions(config, node_role=""):
    groups = []
    for section_name in config.sections():
        if not section_name.startswith("group:"):
            continue
        cfg = config[section_name]
        if not should_run_entry(cfg, node_role):
            continue
        groups.append(
            {
                "group_key": section_name.split(":", 1)[-1],
                "label": cfg.get("label", section_name),
                "services": [item.strip() for item in cfg.get("services", "").split(",") if item.strip()],
                "failure_condition": cfg.get("failure_condition", "any_down").strip(),
                "severity": cfg.get("severity", "medium").strip(),
                "message": cfg.get("message", "").strip(),
            }
        )
    return groups


def run_once(config_path=SERVICES_CONFIG_PATH):
    config = load_services_config(config_path)
    monitoring_cfg = config["monitoring"]
    payload = build_payload(config)

    ingest_url = monitoring_cfg.get("ingest_url", "").strip()
    ingest_token = monitoring_cfg.get("ingest_token", "").strip()
    timeout_seconds = float(monitoring_cfg.get("request_timeout_seconds", "8"))

    if not ingest_url or not ingest_token:
        raise ValueError("Monitoring ingest_url and ingest_token must be configured")

    writer = MonitoringWriter(ingest_url, ingest_token, timeout_seconds=timeout_seconds)
    response = writer.send(payload)
    logging.info("Monitoring ingest success: %s", json.dumps(response))
    return response


def run_forever(config_path=SERVICES_CONFIG_PATH):
    try:
        start_health_server()
        _write_health_snapshot()
    except OSError:
        logging.exception("Local health endpoint failed to start")
    except Exception:
        logging.exception("Initial local health snapshot failed")

    config = load_services_config(config_path)
    monitoring_cfg = config["monitoring"]
    interval_seconds = int(monitoring_cfg.get("scheduler_tick_seconds", monitoring_cfg.get("interval_seconds", "60")))
    enabled = monitoring_cfg.get("enabled", "1").strip().lower() in {"1", "true", "yes", "on"}

    if not enabled:
        logging.info("Monitoring is disabled in services.ini")
        return

    ingest_url = monitoring_cfg.get("ingest_url", "").strip()
    ingest_token = monitoring_cfg.get("ingest_token", "").strip()
    timeout_seconds = float(monitoring_cfg.get("request_timeout_seconds", "8"))

    if not ingest_url or not ingest_token:
        raise ValueError("Monitoring ingest_url and ingest_token must be configured")

    writer = MonitoringWriter(ingest_url, ingest_token, timeout_seconds=timeout_seconds)
    last_run_by_service = {}

    while True:
        try:
            config = load_services_config(config_path)
            payload, ran_sections = build_due_payload(config, last_run_by_service=last_run_by_service)
            if payload["services"]:
                ingest_status = "healthy"
                ingest_error = None
                try:
                    response = writer.send(payload)
                    logging.info("Monitoring ingest success: %s", json.dumps(response))
                except Exception as exc:
                    ingest_status = "down"
                    ingest_error = str(exc)
                    logging.exception("Monitoring ingest failed: %s", exc)

                try:
                    _write_health_snapshot(payload, ingest_status, ingest_error)
                except Exception as exc:
                    logging.exception("Local health snapshot failed: %s", exc)

                now_monotonic = time.monotonic()
                for section_name in ran_sections:
                    last_run_by_service[section_name] = now_monotonic
            else:
                logging.info("Monitoring tick skipped: no services due")
        except Exception as exc:
            logging.exception("Monitoring cycle failed: %s", exc)
            try:
                _write_health_snapshot(ingest_status="down", ingest_error=str(exc))
            except Exception:
                logging.exception("Local health snapshot failed after monitoring error")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    configure_logging()
    run_forever()
