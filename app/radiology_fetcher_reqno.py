import os
import requests
import subprocess
from pypdf import PdfReader, PdfWriter
from pathlib import Path
from app.report_status import fetch_report_status, row_value

# -----------------------------
# CONFIG
# -----------------------------
RADIOLOGY_BASE = "http://120.138.8.37:7777/wordimages"
ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = str(ROOT_DIR / "reports")

BG_PATH = str(ROOT_DIR / "assets" / "background.png")


# -----------------------------
# Helpers
# -----------------------------
def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)


# -----------------------------
# Extract ALL Radiology Files
# -----------------------------
def get_radiology_files(reqno):

    data = fetch_report_status(reqno)

    # Safety: ensure radiology exists
    if data.get("radiology_total", 0) == 0:
        raise Exception("No radiology tests in requisition")

    files = []

    for row in data["tests"]:

        if row_value(row, "GROUPID", "groupid") == "GDEP0002":

            reqid = row_value(row, "REQID", "reqid")
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
def download_radiology(reqno):

    ensure_output_dir()

    files = get_radiology_files(reqno)

    paths = []

    for i, f in enumerate(files):

        path = os.path.join(OUTPUT_DIR, f"RAD_{reqno}_{i}_raw.pdf")

        print("Downloading:", f["url"])

        try:
            r = requests.get(f["url"], timeout=20)

            if r.status_code != 200:
                print("Not ready yet:", f["url"])
                continue

            with open(path, "wb") as out:
                out.write(r.content)

            # basic validation
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
# Apply Background + Compress
# -----------------------------
def apply_background(input_pdf, output_pdf):

    if not os.path.exists(BG_PATH):
        raise Exception("background.png not found at: " + BG_PATH)

    print("Applying background + compression...")

    subprocess.run([
        "magick",
        "-density", "150",                  # lower DPI
        input_pdf,
        "-resize", "1240x1754",             # A4 @ 150 DPI
        BG_PATH,
        "-resize", "1240x1754",
        "-gravity", "center",
        "-compose", "over",
        "-composite",

        "-compress", "jpeg",                # compression
        "-quality", "70",                   # tweakable (60–75)
        "-strip",                           # remove metadata

        output_pdf
    ], check=True)


# -----------------------------
# Process ALL Radiology Files
# -----------------------------
def process_radiology_files(reqno):

    raw_files = download_radiology(reqno)

    final_files = []

    for i, raw in enumerate(raw_files):

        out = os.path.join(OUTPUT_DIR, f"RAD_{reqno}_{i}.pdf")

        apply_background(raw, out)

        final_files.append(out)

    return final_files


# -----------------------------
# Merge PDFs (if multiple)
# -----------------------------

def merge_pdfs(files, output_path):

    print("Merging PDFs...")

    writer = PdfWriter()

    for f in files:
        reader = PdfReader(f)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as out:
        writer.write(out)

    return output_path

# -----------------------------
# Public function
# -----------------------------
def get_radiology_report(reqno):

    final_files = process_radiology_files(reqno)

    # Single report
    if len(final_files) == 1:
        return final_files[0]

    # Multiple → merge
    merged_path = os.path.join(OUTPUT_DIR, f"RAD_{reqno}_MERGED.pdf")

    return merge_pdfs(final_files, merged_path)
