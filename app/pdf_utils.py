from pypdf import PdfReader, PdfWriter
import os
import subprocess


# -----------------------------
# Apply Background + Compress
# -----------------------------

def apply_background_image(input_pdf, output_pdf, bg_path):

    if not os.path.exists(bg_path):
        raise Exception("Background not found: " + bg_path)

    subprocess.run([
        "magick",
        "-density", "150",
        input_pdf,
        "-resize", "1240x1754",
        bg_path,
        "-resize", "1240x1754",
        "-gravity", "center",
        "-compose", "over",
        "-composite",
        "-compress", "jpeg",
        "-quality", "70",
        "-strip",
        output_pdf
    ], check=True)

    return output_pdf

from pypdf import PdfReader, PdfWriter


def apply_background(input_pdf, output_pdf, bg_pdf_path):

    reader = PdfReader(input_pdf)
    bg_reader = PdfReader(bg_pdf_path)

    writer = PdfWriter()

    bg_page = bg_reader.pages[0]

    for page in reader.pages:
        page.merge_page(bg_page)   # overlay background
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    return output_pdf

# -----------------------------
# Check if PDF has real content
# -----------------------------
def is_pdf_blank(path):

    reader = PdfReader(path)

    for page in reader.pages:
        text = page.extract_text()

        if text and text.strip():
            return False

    return True


# -----------------------------
# Remove blank pages
# -----------------------------
def remove_blank_pages(input_path):

    reader = PdfReader(input_path)
    writer = PdfWriter()

    removed = 0

    for page in reader.pages:

        text = page.extract_text()

        if text and text.strip():
            writer.add_page(page)
        else:
            removed += 1

    output_path = input_path

    with open(output_path, "wb") as f:
        writer.write(f)

    return removed


# -----------------------------
# Validate report PDF
# -----------------------------
def validate_pdf(path):

    # tiny files usually blank
    if os.path.getsize(path) < 20000:
        return False

    remove_blank_pages(path)

    if is_pdf_blank(path):
        return False

    return True

# -----------------------------
# Merge PDFs (if multiple)
# -----------------------------

def merge_pdfs(files, output_path):

    writer = PdfWriter()

    for f in files:
        reader = PdfReader(f)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_path, "wb") as out:
        writer.write(out)

    return output_path