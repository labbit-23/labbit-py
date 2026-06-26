import configparser
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import app.report_fetcher as report_fetcher

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BASE = config["server"]["base_url"].rstrip("/")
CONTEXT = config["server"]["context"].strip("/")
OUTPUT_DIR = config["paths"]["reports"]
OUTSOURCED_DIR = os.path.join(OUTPUT_DIR, "outsourced")

DEFAULT_DOCUMENTS_BASE = os.environ.get(
    "SHIVAM_DOCUMENTS_BASE",
    f"{BASE}/documents"
).rstrip("/")


def _clean_alnum(value, field_name):
    text = "".join(ch for ch in str(value or "").strip() if ch.isalnum())
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _build_absolute_from_relative(path_value):
    clean = str(path_value or "").strip()
    if not clean:
        return ""

    if clean.startswith("//"):
        return f"http:{clean}"

    if clean.startswith("/"):
        return f"{BASE}{clean}"

    return ""


def _extract_path_param_if_approvaldisplay(url_text):
    parsed = urlparse(url_text)
    if "approvaldisplay.jsp" not in parsed.path.lower():
        return ""

    query = parse_qs(parsed.query)
    raw_path = ""
    for key in ("path", "PATH"):
        values = query.get(key)
        if values and values[0]:
            raw_path = values[0]
            break

    if not raw_path:
        return ""

    decoded = unquote(str(raw_path or "")).strip()
    decoded = decoded.strip("\"' ")
    return decoded


def _normalize_source_url(source_url):
    text = str(source_url or "").strip()
    if not text:
        return ""

    decoded = unquote(text).strip().strip("\"'")
    parsed = urlparse(decoded)

    if parsed.scheme in {"http", "https"} and parsed.netloc:
        approval_target = _extract_path_param_if_approvaldisplay(decoded)
        if approval_target:
            absolute = _build_absolute_from_relative(approval_target)
            if absolute:
                return absolute
            nested = urlparse(approval_target)
            if nested.scheme in {"http", "https"} and nested.netloc:
                return approval_target
        return decoded

    rel = _build_absolute_from_relative(decoded)
    if rel:
        return rel

    return ""


def build_attachment_filename(reqid, testid):
    clean_reqid = _clean_alnum(reqid, "reqid")
    clean_testid = _clean_alnum(testid, "testid")
    return f"Att{clean_reqid}{clean_testid}.pdf"


def build_attachment_url(reqid, testid, source_url=None):
    explicit = _normalize_source_url(source_url)
    if explicit:
        return explicit

    filename = build_attachment_filename(reqid, testid)
    return f"{DEFAULT_DOCUMENTS_BASE}/{filename}"


def fetch_attachment(reqid, testid, source_url=None, save=True):
    report_fetcher.ensure_session()

    filename = build_attachment_filename(reqid, testid)
    url = build_attachment_url(reqid, testid, source_url=source_url)

    try:
        response = report_fetcher.session.get(url, timeout=45)
    except Exception as exc:
        raise Exception(f"ATTACHMENT_FETCH_NETWORK_ERROR url={url}: {str(exc)}")

    if response.status_code != 200:
        raise Exception(f"ATTACHMENT_FETCH_FAILED status={response.status_code} url={url} reason={response.reason or 'unknown'}")

    content_type = str(response.headers.get("Content-Type", "")).lower()
    content = response.content or b""
    if not content:
        raise Exception(f"ATTACHMENT_EMPTY_RESPONSE url={url}")
    if "application/pdf" not in content_type and not content.startswith(b"%PDF"):
        raise Exception(f"ATTACHMENT_NOT_PDF url={url} content_type={content_type} bytes={len(content)}")

    if not save:
        return {
            "filename": filename,
            "url": url,
            "content": content,
            "content_type": content_type or "application/pdf",
        }

    os.makedirs(OUTSOURCED_DIR, exist_ok=True)
    path = os.path.join(OUTSOURCED_DIR, filename)
    with open(path, "wb") as handle:
        handle.write(content)

    return {
        "filename": filename,
        "url": url,
        "path": path,
        "bytes": len(content),
    }
