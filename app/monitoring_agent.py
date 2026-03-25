import configparser
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.monitoring_checks import run_check
from app.monitoring_writer import MonitoringWriter

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICES_CONFIG_PATH = os.environ.get("MONITORING_SERVICES_INI", str(ROOT_DIR / "services.ini"))
LOG_PATH = os.environ.get("MONITORING_LOG_PATH", str(ROOT_DIR / "logs" / "monitoring.log"))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


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


def build_payload(config):
    monitoring_cfg = config["monitoring"]
    default_timeout = float(monitoring_cfg.get("request_timeout_seconds", "8"))

    services = []
    for section_name in config.sections():
        if not section_name.startswith("service:"):
            continue
        cfg = config[section_name]
        if cfg.get("enabled", "1").strip() not in {"1", "true", "yes", "on"}:
            continue
        services.append(run_check(section_name, cfg, default_timeout))

    group_definitions = load_group_definitions(config)
    if group_definitions:
        services.append(
            {
                "service_key": "__group_config__",
                "category": "diagnosis",
                "label": "Dependency Groups",
                "status": "healthy",
                "latency_ms": 0,
                "message": "Dependency groups loaded from services.ini",
                "payload": {"groups": group_definitions},
                "checked_at": utc_now_iso(),
            }
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

      service_interval = int(cfg.get("interval_seconds", monitoring_cfg.get("interval_seconds", "60")))
      last_run = last_run_by_service.get(section_name)
      if last_run is not None and now_monotonic - last_run < service_interval:
          continue

      services.append(run_check(section_name, cfg, default_timeout))
      ran_sections.append(section_name)

    payload = {
        "lab_id": monitoring_cfg.get("lab_id", "").strip(),
        "source": monitoring_cfg.get("source", "monitoring-agent").strip(),
        "checked_at": utc_now_iso(),
        "services": services,
    }

    group_definitions = load_group_definitions(config)
    if group_definitions:
        payload["services"].append(
            {
                "service_key": "__group_config__",
                "category": "diagnosis",
                "label": "Dependency Groups",
                "status": "healthy",
                "latency_ms": 0,
                "message": "Dependency groups loaded from services.ini",
                "payload": {"groups": group_definitions},
                "checked_at": utc_now_iso(),
            }
        )

    return payload, ran_sections


def load_group_definitions(config):
    groups = []
    for section_name in config.sections():
        if not section_name.startswith("group:"):
            continue
        cfg = config[section_name]
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
                response = writer.send(payload)
                logging.info("Monitoring ingest success: %s", json.dumps(response))
                now_monotonic = time.monotonic()
                for section_name in ran_sections:
                    last_run_by_service[section_name] = now_monotonic
            else:
                logging.info("Monitoring tick skipped: no services due")
        except Exception as exc:
            logging.exception("Monitoring cycle failed: %s", exc)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    configure_logging()
    run_forever()
