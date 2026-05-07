import os
import requests, configparser, json
import subprocess
from pypdf import PdfReader, PdfWriter
from pathlib import Path
from app.report_status import fetch_report_status, row_value, fetch_report_status_by_reqid
from app.pdf_utils import apply_background, merge_pdfs
# -----------------------------
# CONFIG
# -----------------------------
config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

REQID_STATUS_API = config["api"]["reportstatusreqidapi"]
RADIOLOGY_BASE = "http://120.138.8.37:7777/wordimages"
RADIOLOGY_WORDOLE_BASE = str(config.get("api", "radiology_wordole_base", fallback="http://120.138.8.37:9999/shivam")).rstrip("/")
RADIOLOGY_RESOLVER_ENABLED = str(config.get("radiology", "use_wordole_resolver", fallback="0") or "0").strip().lower() in {"1", "true", "yes", "on"}
OUTPUT_DIR = str(ROOT_DIR / "reports")

BG_PATH = str(ROOT_DIR / "assets" / "background.pdf")


# -----------------------------
# Helpers
# -----------------------------


def _is_probable_pdf_bytes(content: bytes) -> bool:
    return bool(content) and content[:4] == b"%PDF"


def _download_if_pdf(url: str, timeout: int = 20):
    try:
        r = requests.get(url, timeout=timeout)
    except Exception as exc:
        return False, b"", f"request_error:{exc}"

    if r.status_code != 200:
        return False, b"", f"status:{r.status_code}"

    ctype = str(r.headers.get("Content-Type", "")).lower()
    data = r.content or b""
    if "application/pdf" in ctype or _is_probable_pdf_bytes(data):
        return True, data, "ok"

    return False, b"", f"not_pdf:{ctype[:60]}"


def _try_wordole_filename(reqno: str, reqid: str, testid: str):
    if not reqno or not reqid or not testid:
        return "", "missing_keys"

    url = (
        f"{RADIOLOGY_WORDOLE_BASE}/DgWordoleVF"
        f"?reqno={reqno}&type=resultsviewvf&testid={testid}"
        f"&tmplid=0&reporthead=0&reqid={reqid}&formid=wf145"
    )

    try:
        r = requests.get(url, timeout=20)
    except Exception as exc:
        return "", f"resolver_error:{exc}"

    if r.status_code != 200:
        return "", f"resolver_status:{r.status_code}"

    token = str(r.text or "").strip().replace("\r", "").replace("\n", "")
    if token.lower().endswith(".pdf") and len(token) < 120:
        return token, "ok"

    return "", "resolver_no_token"


def _download_radiology_via_resolver(reqno: str, reqid: str, testid: str):
    token, reason = _try_wordole_filename(reqno, reqid, testid)
    if not token:
        print(f"Resolver token missing for {reqid}/{testid}: {reason}")
        return None

    candidates = [
        f"http://120.138.8.37:9999/wordimages/{token}",
        f"http://120.138.8.37:7777/wordimages/{token}",
    ]

    for url in candidates:
        ok, data, why = _download_if_pdf(url, timeout=20)
        if ok:
            print(f"Resolver download success: {url}")
            return {"filename": token, "url": url, "content": data}
        print(f"Resolver candidate failed: {url} ({why})")

    return None

def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)


# -----------------------------
# Extract ALL Radiology Files
# -----------------------------
def get_radiology_files(reqid):

    data = fetch_report_status_by_reqid(reqid)

    if data.get("radiology_total", 0) == 0:
        raise Exception("No radiology tests in requisition")

    files = []

    for row in data["tests"]:

        if row_value(row, "GROUPID", "groupid") == "GDEP0002":

            testid = row_value(row, "TESTID", "testid")

            if reqid and testid:
                filename = f"{reqid}{testid}.pdf"
                url = f"{RADIOLOGY_BASE}/{filename}"

                print("Radiology file:", url)

                files.append({
                    "url": url,
                    "filename": filename,
                    "reqno": str(data.get("reqno") or "").strip(),
                    "testid": str(testid or "").strip(),
                })

    if not files:
        raise Exception("Radiology tests found but no valid files")

    return files


# -----------------------------
# Download Radiology PDFs
# -----------------------------
def download_radiology(reqid):

    ensure_output_dir()

    files = get_radiology_files(reqid)

    paths = []

    for i, f in enumerate(files):

        path = os.path.join(OUTPUT_DIR, f"RAD_{reqid}_{i}_raw.pdf")

        reqno = str(f.get("reqno") or "").strip()
        testid = str(f.get("testid") or "").strip()
        resolver_used = False

        try:
            if RADIOLOGY_RESOLVER_ENABLED:
                resolved = _download_radiology_via_resolver(reqno=reqno, reqid=reqid, testid=testid)
                if resolved and isinstance(resolved.get("content"), (bytes, bytearray)):
                    with open(path, "wb") as out:
                        out.write(resolved["content"])
                    resolver_used = True

            if not resolver_used:
                print("Downloading (legacy):", f["url"])
                r = requests.get(f["url"], timeout=20)

                if r.status_code != 200:
                    print("Not ready yet:", f["url"])
                    continue

                with open(path, "wb") as out:
                    out.write(r.content)

            if os.path.getsize(path) < 5000:
                print("File too small, skipping:", path)
                continue

            paths.append(path)

        except Exception as e:
            print("Download failed:", f.get("url"), e)

    if not paths:
        raise Exception("No radiology PDFs available yet")

    return paths



# -----------------------------
# Process ALL Radiology Files
# -----------------------------
def process_radiology_files(reqid, apply_background_overlay=True):

    raw_files = download_radiology(reqid)

    final_files = []

    for i, raw in enumerate(raw_files):

        if apply_background_overlay:
            out = os.path.join(OUTPUT_DIR, f"RAD_{reqid}_{i}.pdf")
            apply_background(raw, out, BG_PATH)
            final_files.append(out)
        else:
            # Plain mode: keep source radiology file without adding background overlay.
            final_files.append(raw)

    return final_files


# -----------------------------
# Public function (REQID based)
# -----------------------------
def get_radiology_report(reqid, apply_background_overlay=True):

    final_files = process_radiology_files(reqid, apply_background_overlay=apply_background_overlay)

    if len(final_files) == 1:
        return final_files[0]

    merged_path = os.path.join(OUTPUT_DIR, f"RAD_{reqid}_MERGED.pdf")

    return merge_pdfs(final_files, merged_path)
