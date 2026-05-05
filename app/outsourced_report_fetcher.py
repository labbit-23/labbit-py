import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader

import app.report_fetcher as report_fetcher
from app.attachment_fetcher import fetch_attachment
from app.report_status import fetch_report_status_by_reqid

ROOT_DIR = Path(__file__).resolve().parents[1]
OUTSOURCED_DIR = os.path.join(report_fetcher.OUTPUT_DIR, "outsourced")

URL_PATTERN = re.compile(r"https?://[^\s)>'\"]+", re.IGNORECASE)


def _is_pdf_bytes(content):
    return bool(content) and bytes(content[:4]) == b"%PDF"


def _normalize_candidate_url(value):
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.strip("\"' ")
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    return ""


def _urls_from_annotations(page):
    urls = []
    annots = page.get("/Annots") or []
    for annot_ref in annots:
        try:
            annot = annot_ref.get_object()
            action = annot.get("/A") if hasattr(annot, "get") else None
            uri = action.get("/URI") if action and hasattr(action, "get") else None
            normalized = _normalize_candidate_url(uri)
            if normalized:
                urls.append(normalized)
        except Exception:
            continue
    return urls


def _extract_url_candidates_from_pdf_text(pdf_path, debug):
    reader = PdfReader(pdf_path)
    seen = set()
    ordered = []
    annotation_count = 0
    text_url_count = 0

    for page in reader.pages:
        ann_urls = _urls_from_annotations(page)
        annotation_count += len(ann_urls)
        for url in ann_urls:
            if url not in seen:
                seen.add(url)
                ordered.append(url)

        text = page.extract_text() or ""
        for match in URL_PATTERN.findall(text):
            clean = _normalize_candidate_url(match.rstrip(".,;\"'"))
            if clean:
                text_url_count += 1
            if clean and clean not in seen:
                seen.add(clean)
                ordered.append(clean)

    debug["text_extract"] = {
        "annotation_urls_seen": annotation_count,
        "text_urls_seen": text_url_count,
        "unique_urls": len(ordered),
    }
    return ordered


def _render_page_to_bgr(page, scale, cv2, np, fitz):
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n == 3:
        img = arr.reshape(pix.height, pix.width, 3)
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if pix.n == 4:
        img = arr.reshape(pix.height, pix.width, 4)
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    img = arr.reshape(pix.height, pix.width)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _decode_qr_from_image(img, detector, cv2):
    variants = [img]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    variants.append(gray)

    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    variants.append(blur)

    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(thr)
    variants.append(cv2.bitwise_not(thr))

    h, w = gray.shape[:2]
    crops = [
        gray[max(0, int(h * 0.5)):h, max(0, int(w * 0.5)):w],
        gray[max(0, int(h * 0.5)):h, 0:int(w * 0.5)],
        gray[0:int(h * 0.5), max(0, int(w * 0.5)):w],
    ]
    for c in crops:
        if c.size:
            variants.append(c)

    for frame in variants:
        try:
            value, points, _ = detector.detectAndDecode(frame)
            clean = _normalize_candidate_url(value)
            if points is not None and clean:
                return clean
        except Exception:
            continue

    return ""


def _extract_qr_urls_from_pdf_images(pdf_path, debug):
    try:
        import fitz
        import cv2
        import numpy as np
    except Exception as exc:
        debug["qr_image_extract"] = {
            "dependencies_available": False,
            "dependency_error": str(exc),
        }
        return []

    detector = cv2.QRCodeDetector()
    doc = fitz.open(pdf_path)
    found = []
    attempts = 0
    render_errors = 0

    for i in range(doc.page_count):
        page = doc[i]
        for scale in (2.5, 3.0, 4.0, 5.0):
            attempts += 1
            try:
                img = _render_page_to_bgr(page, scale, cv2, np, fitz)
            except Exception:
                render_errors += 1
                continue
            decoded = _decode_qr_from_image(img, detector, cv2)
            if decoded and decoded not in found:
                found.append(decoded)

    debug["qr_image_extract"] = {
        "dependencies_available": True,
        "page_count": int(doc.page_count),
        "render_attempts": attempts,
        "render_errors": render_errors,
        "qr_urls_found": len(found),
    }
    return found


def extract_url_candidates_from_pdf(pdf_path, debug=None):
    dbg = debug if isinstance(debug, dict) else {}
    candidates = []

    qr_urls = _extract_qr_urls_from_pdf_images(pdf_path, dbg)
    for qr_url in qr_urls:
        if qr_url not in candidates:
            candidates.append(qr_url)

    text_urls = _extract_url_candidates_from_pdf_text(pdf_path, dbg)
    for url in text_urls:
        if url not in candidates:
            candidates.append(url)

    dbg["candidate_summary"] = {
        "from_qr_image": len(qr_urls),
        "from_text_or_annotations": len(text_urls),
        "total_unique": len(candidates),
    }
    return candidates


def _download_pdf(url, timeout=60):
    report_fetcher.ensure_session()
    response = report_fetcher.session.get(url, timeout=timeout, allow_redirects=True)
    if response.status_code != 200:
        raise Exception(f"LETTERHEAD_FETCH_FAILED status={response.status_code}")

    content = response.content or b""
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if "application/pdf" not in content_type and not _is_pdf_bytes(content):
        raise Exception("LETTERHEAD_NOT_PDF")

    return content


def _build_base_letterhead_payload(base, status="resolved_base_fallback", reason=""):
    filename = str(base.get("filename") or "outsourced_base.pdf")
    path = str(base.get("path") or "")
    payload = {
        "ok": True,
        "status": status,
        "outsourced_mode": "attached_base" if status != "resolved_direct_source" else "transcribed",
        "base": base,
        "letterhead": {
            "filename": filename,
            "path": path,
            "bytes": base.get("bytes"),
            "url": base.get("url"),
        },
        "qr_candidates": [],
    }
    if status != "resolved_direct_source":
        payload["error_code"] = "BASE_PDF_USED"
        payload["error"] = reason or "Using base PDF directly as fallback"
    return payload


def _row_value_case_insensitive(row, *keys):
    if not isinstance(row, dict):
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is not None:
            return value
    return None


def _resolve_reqno_for_test(reqid, testid):
    try:
        payload = fetch_report_status_by_reqid(reqid)
    except Exception:
        return ""

    tests = payload.get("tests") if isinstance(payload, dict) else []
    wanted_testid = str(testid or "").strip().upper()

    for row in tests if isinstance(tests, list) else []:
        row_testid = str(_row_value_case_insensitive(row, "TESTID", "testid") or "").strip().upper()
        if row_testid != wanted_testid:
            continue
        row_reqno = str(_row_value_case_insensitive(row, "REQNO", "reqno") or "").strip()
        if row_reqno:
            return row_reqno

    top_reqno = str(payload.get("reqno") or "").strip() if isinstance(payload, dict) else ""
    return top_reqno


def _build_direct_dgreporting_urls(reqid, testid, reqno):
    reqid_text = str(reqid or "").strip()
    testid_text = str(testid or "").strip()
    reqno_text = str(reqno or "").strip()
    if not reqid_text or not testid_text:
        return []

    base_url = f"{report_fetcher.APP}/DgReportingVF"
    date_str = datetime.now().strftime("%d/%m/%Y")
    common = (
        f"scheme_testid=null&chkrephead=1&keyid={reqid_text}&testid={testid_text}"
        f"&ptype=null&calledfrom=0&formid=wf145&transid=TR12&fromdt={date_str}&todt={date_str}"
    )
    urls = []

    if reqno_text:
        urls.append(f"{base_url}?{common}&reqno={reqno_text}")

    urls.append(f"{base_url}?{common}")
    return urls


def _find_attachment_rows(reqid, testid):
    report_fetcher.ensure_session()
    url = f"{report_fetcher.APP}/Documents"
    params = {
        "process": "get",
        "id": str(reqid).strip(),
        "no": str(testid).strip(),
        "formname": "/Dg/FWreporitng.jsp",
    }
    response = report_fetcher.session.get(url, params=params, timeout=30)
    if response.status_code != 200:
        return []

    try:
        data = response.json()
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    return data


def _has_pdf_attachment(rows):
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        itype = str(row.get("IMAGETYPE") or row.get("imagetype") or "").strip().lower()
        ipath = str(row.get("IMAGEPATH") or row.get("imagepath") or "").strip()
        if itype == "pdf" and ipath:
            return True
    return False


def fetch_outsourced_report(reqid, testid, source_url=None, qr_url=None):
    debug = {
        "input": {
            "reqid": str(reqid or ""),
            "testid": str(testid or ""),
            "source_url_provided": bool(str(source_url or "").strip()),
            "qr_url_provided": bool(str(qr_url or "").strip()),
        }
    }

    # Step 1 (classification priority): attachment metadata lookup
    attachment_rows = _find_attachment_rows(reqid, testid)
    has_attachment_pdf = _has_pdf_attachment(attachment_rows)
    debug["documents_lookup"] = {
        "rows": len(attachment_rows),
        "has_pdf": has_attachment_pdf,
    }

    # Optional manual direct source override if explicitly provided
    if str(source_url or "").strip():
        try:
            base = fetch_attachment(reqid=reqid, testid=testid, source_url=source_url, save=True)
            debug["direct_source"] = {"ok": True, "url": base.get("url"), "manual_override": True}
            payload = _build_base_letterhead_payload(base, status="resolved_direct_source")
            payload["debug"] = debug
            return payload
        except Exception as exc:
            debug["direct_source"] = {"ok": False, "manual_override": True, "error": str(exc)}

    # Step 2: attached flow if attachment metadata exists
    if has_attachment_pdf:
        try:
            base = fetch_attachment(reqid=reqid, testid=testid, source_url=None, save=True)
        except Exception as exc:
            debug["base"] = {
                "path": None,
                "bytes": None,
                "url": None,
                "attachment_error": str(exc),
            }
            return {
                "ok": False,
                "status": "unavailable",
                "outsourced_mode": "unavailable",
                "error_code": "ATTACHMENT_NOT_FOUND",
                "error": str(exc),
                "base": None,
                "qr_candidates": [],
                "debug": debug,
            }

        base_path = str(base.get("path") or "").strip()
        debug["base"] = {
            "path": base_path,
            "bytes": base.get("bytes"),
            "url": base.get("url"),
        }

        if not base_path:
            return {
                "ok": False,
                "status": "outsourced_base_path_missing",
                "outsourced_mode": "unavailable",
                "base": base,
                "qr_candidates": [],
                "debug": debug,
            }

        explicit_qr = _normalize_candidate_url(qr_url)
        candidates = [explicit_qr] if explicit_qr else []

        extracted = extract_url_candidates_from_pdf(base_path, debug=debug)
        for url in extracted:
            if url not in candidates:
                candidates.append(url)

        ordered = []
        if explicit_qr:
            ordered.append(explicit_qr)
        ranked = sorted(
            [u for u in candidates if u != explicit_qr],
            key=lambda u: (
                0 if "labreportview" in u.lower() else 1,
                0 if "ereports" in u.lower() else 1,
                len(u),
            ),
        )
        ordered.extend(ranked)
        debug["ordered_candidates"] = ordered

        if not ordered:
            payload = _build_base_letterhead_payload(
                base,
                status="resolved_base_fallback",
                reason="No QR URL could be decoded from PDF images, annotations, or text.",
            )
            payload["outsourced_mode"] = "attached_base"
            payload["qr_candidates"] = candidates
            payload["debug"] = debug
            return payload

        last_error = None
        content = None
        chosen = ""
        fetch_attempts = []

        for candidate in ordered:
            try:
                content = _download_pdf(candidate)
                chosen = candidate
                fetch_attempts.append({"url": candidate, "ok": True})
                break
            except Exception as exc:
                last_error = str(exc)
                fetch_attempts.append({"url": candidate, "ok": False, "error": str(exc)})
                continue

        debug["fetch_attempts"] = fetch_attempts

        if not content or not chosen:
            payload = _build_base_letterhead_payload(
                base,
                status="resolved_base_fallback",
                reason=last_error or "QR URLs were decoded but none returned a valid PDF",
            )
            payload["outsourced_mode"] = "attached_base"
            payload["qr_candidates"] = ordered
            payload["debug"] = debug
            return payload

        os.makedirs(OUTSOURCED_DIR, exist_ok=True)
        letterhead_filename = f"Letterhead_{reqid}{testid}.pdf"
        letterhead_path = os.path.join(OUTSOURCED_DIR, letterhead_filename)
        with open(letterhead_path, "wb") as handle:
            handle.write(content)

        return {
            "ok": True,
            "status": "resolved",
            "outsourced_mode": "attached_qr",
            "base": base,
            "letterhead": {
                "filename": letterhead_filename,
                "path": letterhead_path,
                "bytes": len(content),
                "url": chosen,
            },
            "qr_candidates": ordered,
            "debug": debug,
        }

    # Step 3: no attachment found -> try transcribed direct path
    base_error = None
    direct_attempts = []
    resolved_reqno = _resolve_reqno_for_test(reqid, testid)
    debug["resolved_reqno"] = resolved_reqno or None
    direct_candidates = _build_direct_dgreporting_urls(reqid, testid, resolved_reqno)

    for direct_url in direct_candidates:
        try:
            base = fetch_attachment(reqid=reqid, testid=testid, source_url=direct_url, save=True)
            direct_attempts.append({"url": direct_url, "ok": True})
            debug["direct_source"] = {"ok": True, "url": base.get("url"), "attempts": direct_attempts}
            payload = _build_base_letterhead_payload(base, status="resolved_direct_source")
            payload["outsourced_mode"] = "transcribed"
            payload["debug"] = debug
            return payload
        except Exception as exc:
            base_error = str(exc)
            direct_attempts.append({"url": direct_url, "ok": False, "error": base_error})

    debug["direct_source"] = {"ok": False, "error": base_error, "attempts": direct_attempts}
    return {
        "ok": False,
        "status": "unavailable",
        "outsourced_mode": "unavailable",
        "error_code": "NO_ATTACHMENT_AND_NO_TRANSCRIBED_REPORT",
        "error": base_error or "No attachment rows and no direct transcribed report",
        "base": None,
        "qr_candidates": [],
        "debug": debug,
    }


def _probe_direct_pdf_url(url, timeout=20):
    report_fetcher.ensure_session()
    response = report_fetcher.session.get(url, timeout=timeout, allow_redirects=True)
    if response.status_code != 200:
        return False
    ctype = str(response.headers.get("Content-Type", "")).lower()
    content = response.content or b""
    return ("application/pdf" in ctype) or content.startswith(b"%PDF")


def classify_outsourced_report(reqid, testid):
    """
    Lightweight classification only.
    No QR extraction, no attachment PDF download/write.
    """
    debug = {
        "input": {"reqid": str(reqid or ""), "testid": str(testid or "")}
    }

    rows = _find_attachment_rows(reqid, testid)
    has_pdf = _has_pdf_attachment(rows)
    debug["documents_lookup"] = {"rows": len(rows), "has_pdf": has_pdf}

    if has_pdf:
        return {
            "ok": True,
            "status": "classified",
            "outsourced_mode": "attached_pending_resolution",
            "dispatch_route": "/outsourced-report",
            "debug": debug,
        }

    resolved_reqno = _resolve_reqno_for_test(reqid, testid)
    debug["resolved_reqno"] = resolved_reqno or None
    attempts = []
    for url in _build_direct_dgreporting_urls(reqid, testid, resolved_reqno):
        ok = False
        err = None
        try:
            ok = _probe_direct_pdf_url(url)
        except Exception as exc:
            err = str(exc)
        attempts.append({"url": url, "ok": ok, "error": err})
        if ok:
            debug["direct_probe_attempts"] = attempts
            return {
                "ok": True,
                "status": "classified",
                "outsourced_mode": "transcribed",
                "dispatch_route": "/report",
                "debug": debug,
            }

    debug["direct_probe_attempts"] = attempts
    return {
        "ok": False,
        "status": "unavailable",
        "outsourced_mode": "unavailable",
        "dispatch_route": "flag_manual",
        "error_code": "NO_ATTACHMENT_AND_NO_TRANSCRIBED_REPORT",
        "debug": debug,
    }
