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
OUTPUT_DIR = str(ROOT_DIR / "reports")

BG_PATH = str(ROOT_DIR / "assets" / "background.pdf")


# -----------------------------
# Helpers
# -----------------------------
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
                    "filename": filename
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

        print("Downloading:", f["url"])

        try:
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
            print("Download failed:", f["url"], e)

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
