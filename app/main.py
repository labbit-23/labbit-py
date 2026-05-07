from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import FileResponse, HTMLResponse
from pypdf import PdfReader, PdfWriter
from app.radiology_fetcher import get_radiology_report
from app.req_lookup import fetch_reqids, fetch_reqid_direct
from app.report_fetcher import get_report, get_combined_report
from app.report_status import fetch_report_status, fetch_report_status_by_reqid
from app.report_fetcher import get_trend_report
from app.trends_data_api import fetch_trends_data, TrendsDataError
from app.delivery_api import (
    fetch_requisitions_by_date,
    fetch_delivery_status,
    fetch_update_delivery_status,
)
from app.shivam_tools import (
    fetch_demographics_by_mrno,
    update_demographics,
    fetch_pricelist,
)
from app.department_worklist_api import fetch_department_worklist
from app.attachment_fetcher import fetch_attachment
from app.outsourced_report_fetcher import fetch_outsourced_report, classify_outsourced_report
from app.dispatch_context import build_dispatch_context
from app.pdf_utils import apply_background
import logging
import tempfile
import copy
import os
import configparser
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

BG_PATH = str(ROOT_DIR / "assets" / "background.pdf")
LOG_DIR = config["paths"]["logs"]

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "api.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = FastAPI(
    root_path=os.getenv("FASTAPI_ROOT_PATH", "/py")
)


class DeliveryStatusUpdateRequest(BaseModel):
    reqno: str
    status: str
    channel: str
    message: str


class ShivamDemographicsUpdateRequest(BaseModel):
    reqno: Optional[str] = None
    reqid: Optional[str] = None
    mrno: Optional[str] = None
    patient_name: Optional[str] = None
    mobile_no: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[str] = None
    gender: Optional[str] = None
    sex: Optional[int] = None
    email: Optional[str] = None
    pincode: Optional[str] = None
    ageyrs: Optional[int] = None
    agemonths: Optional[int] = None
    agedays: Optional[int] = None


class DepartmentWorklistRequest(BaseModel):
    fromreqdate: Optional[str] = None
    toreqdate: Optional[str] = None
    department: Optional[str] = None
    department_name: Optional[str] = None





def _read_do_not_send_source_ids():
    values = set()

    env_raw = str(os.getenv("DO_NOT_SEND_SOURCE_IDS", "")).strip()
    if env_raw:
        for part in env_raw.split(","):
            token = str(part or "").strip()
            if token:
                values.add(token)

    if config.has_section("dispatch_policy"):
        cfg_raw = str(config.get("dispatch_policy", "do_not_send_source_ids", fallback="") or "").strip()
        if cfg_raw:
            for part in cfg_raw.split(","):
                token = str(part or "").strip()
                if token:
                    values.add(token)

    return values


def _first_non_empty_source_value(status_data, *keys):
    if not isinstance(status_data, dict):
        return None

    for key in keys:
        val = status_data.get(key)
        text = str(val or "").strip()
        if text:
            return text

    tests = status_data.get("tests")
    if not isinstance(tests, list):
        return None

    wanted = {str(k).strip().lower() for k in keys}
    for row in tests:
        if not isinstance(row, dict):
            continue
        lowered = {str(k).strip().lower(): v for k, v in row.items()}
        for key in wanted:
            val = lowered.get(key)
            text = str(val or "").strip()
            if text:
                return text

    return None


def _build_deny_payload(status_data):
    source_id = _first_non_empty_source_value(status_data, "source_id", "SOURCE_ID", "sourceid", "SOURCEID", "refdoctor", "REFDOCTOR")
    source_name = _first_non_empty_source_value(status_data, "source_name", "SOURCE_NAME", "sourcenm", "SOURCENM", "drname", "DRNAME")

    if source_id and source_id in DO_NOT_SEND_SOURCE_IDS:
        return {
            "dispatch_allowed": False,
            "code": "SOURCE_CONFIDENTIAL_DO_NOT_SEND",
            "reason": "source_confidential_do_not_send",
            "source_id": source_id,
            "source_name": source_name,
        }

    return {
        "dispatch_allowed": True,
        "code": None,
        "reason": None,
        "source_id": source_id,
        "source_name": source_name,
    }


def _require_dispatch_allowed(*, reqid=None, reqno=None, status_data=None):
    if not DO_NOT_SEND_SOURCE_IDS:
        return

    data = status_data
    if not isinstance(data, dict):
        if reqid:
            data = fetch_report_status_by_reqid(reqid)
        elif reqno:
            data = fetch_report_status(reqno)

    deny = _build_deny_payload(data if isinstance(data, dict) else {})
    if not deny.get("dispatch_allowed", True):
        raise HTTPException(status_code=403, detail=deny)


def _attach_dispatch_policy(data):
    if not isinstance(data, dict):
        return data

    deny = _build_deny_payload(data)
    out = dict(data)
    out["dispatch_allowed"] = bool(deny.get("dispatch_allowed", True))
    out["dispatch_denial_code"] = deny.get("code")
    out["dispatch_denial_reason"] = deny.get("reason")
    out["source_id"] = deny.get("source_id")
    out["source_name"] = deny.get("source_name")
    return out


DO_NOT_SEND_SOURCE_IDS = _read_do_not_send_source_ids()

def _is_truthy(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}




def _outsourced_header_merge_mode():
    raw = str(config.get("outsourced", "header_merge_mode", fallback="overlay") or "overlay").strip().lower()
    if raw in {"underlay", "background_behind", "behind"}:
        return "underlay"
    if raw in {"logo_only", "logo", "logo-overlay"}:
        return "logo_only"
    return "overlay"


def _cfg_float(section, key, fallback):
    try:
        return float(str(config.get(section, key, fallback=str(fallback)) or fallback).strip())
    except Exception:
        return float(fallback)


def _cfg_page_scope(section, key, fallback="1"):
    raw = str(config.get(section, key, fallback=fallback) or fallback).strip().lower()
    return "all" if raw == "all" else "1"



def _make_logo_stamp_pdf(page_width_pt, page_height_pt):
    logo_path = str(ROOT_DIR / "assets" / "logo.png")
    if not os.path.exists(logo_path):
        raise Exception("Logo not found: " + logo_path)

    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception as exc:
        raise Exception(f"reportlab required for logo_only mode: {exc}")

    left = _cfg_float("outsourced", "logo_left_pt", 36)
    top = _cfg_float("outsourced", "logo_top_pt", 20)
    width_cfg = str(config.get("outsourced", "logo_width_pt", fallback="") or "").strip()
    height_cfg = str(config.get("outsourced", "logo_height_pt", fallback="") or "").strip()

    image = ImageReader(logo_path)
    src_w, src_h = image.getSize()
    if src_w <= 0 or src_h <= 0:
        raise Exception("Invalid logo dimensions")

    width_pt = float(width_cfg) if width_cfg else None
    height_pt = float(height_cfg) if height_cfg else None

    if width_pt and height_pt:
        height_pt = width_pt * (src_h / src_w)
    elif width_pt:
        height_pt = width_pt * (src_h / src_w)
    elif height_pt:
        width_pt = height_pt * (src_w / src_h)
    else:
        width_pt = 120.0
        height_pt = width_pt * (src_h / src_w)

    x = float(left)
    y = float(page_height_pt) - float(top) - float(height_pt)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_pdf = tmp.name

    c = canvas.Canvas(tmp_pdf, pagesize=(float(page_width_pt), float(page_height_pt)))
    c.drawImage(image, x, y, width=float(width_pt), height=float(height_pt), mask='auto', preserveAspectRatio=True)
    c.showPage()
    c.save()

    return tmp_pdf


def _apply_background_first_page(input_pdf, output_pdf, bg_pdf_path):
    if not os.path.exists(bg_pdf_path):
        raise Exception("Background not found: " + bg_pdf_path)

    reader = PdfReader(input_pdf)
    bg_reader = PdfReader(bg_pdf_path)
    writer = PdfWriter()

    bg_page = bg_reader.pages[0]
    merge_mode = _outsourced_header_merge_mode()
    header_scope = _cfg_page_scope("outsourced", "header_apply_pages", fallback="1")
    logo_scope = _cfg_page_scope("outsourced", "logo_apply_pages", fallback="1")
    logo_stamp_pdf = ""
    logo_page = None

    if merge_mode == "logo_only":
        if not reader.pages:
            raise Exception("Input PDF has no pages")
        first = reader.pages[0]
        logo_stamp_pdf = _make_logo_stamp_pdf(float(first.mediabox.width), float(first.mediabox.height))
        logo_reader = PdfReader(logo_stamp_pdf)
        logo_page = logo_reader.pages[0]

    try:
        for idx, page in enumerate(reader.pages):
            apply_header = (header_scope == "all") or (idx == 0)
            apply_logo = (logo_scope == "all") or (idx == 0)

            if merge_mode == "underlay" and apply_header:
                merged = copy.copy(bg_page)
                merged.merge_page(page)
                writer.add_page(merged)
            elif merge_mode == "logo_only" and apply_logo:
                page.merge_page(logo_page)
                writer.add_page(page)
            elif merge_mode == "overlay" and apply_header:
                page.merge_page(bg_page)
                writer.add_page(page)
            else:
                writer.add_page(page)

        with open(output_pdf, "wb") as handle:
            writer.write(handle)
    finally:
        if logo_stamp_pdf and os.path.exists(logo_stamp_pdf):
            try:
                os.remove(logo_stamp_pdf)
            except Exception:
                pass

    return output_pdf


def _resolve_plain_mode(header_mode=None, without_header_background=None, chkrephead=None):
    plain = False

    if chkrephead is not None:
        chk = str(chkrephead).strip().lower()
        if chk in {"0", "false", "no", "off"}:
            plain = True
        elif chk in {"1", "true", "yes", "on"}:
            plain = False

    mode = str(header_mode or "").strip().lower()
    if mode in {"plain", "without_header", "without_header_background", "no_header", "no_bg"}:
        plain = True

    if _is_truthy(without_header_background):
        plain = True

    return plain


# -----------------------------
# Health Check
# -----------------------------
@app.get("/health")
def health():
    return {"status": "running"}


# -----------------------------
# Lookup last 9 reports
# -----------------------------
@app.get("/lookup/{phone}")
def lookup(phone):

    rows = fetch_reqids(phone)

    return {
        "phone": phone,
        "latest_reports": rows
    }


# -----------------------------
# Radiology Fetcher
# -----------------------------
@app.get("/radiologyreport/{reqid}")
def radiology_report(
    reqid,
    header_mode: str = Query(default="default"),
    without_header_background: Optional[str] = Query(default=None)
):

    try:
        plain = _resolve_plain_mode(
            header_mode=header_mode,
            without_header_background=without_header_background
        )
        path = get_radiology_report(reqid, apply_background_overlay=not plain)

        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"radiology_{reqid}.pdf"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc)
        )


# -----------------------------
# Download report by ReqID
# -----------------------------
@app.get("/reports/{reqid}")
def report(
    reqid,
    reqno: Optional[str] = Query(default=None),
    printtype: str = Query(default="1"),
    chkrephead: Optional[str] = Query(default=None),
    header_mode: str = Query(default="default"),
    without_header_background: Optional[str] = Query(default=None)
):
    try:
        plain = _resolve_plain_mode(
            header_mode=header_mode,
            without_header_background=without_header_background,
            chkrephead=chkrephead
        )

        _require_dispatch_allowed(reqid=reqid, reqno=reqno)

        path = get_report(
            reqid,
            include_header=not plain,
            printtype=printtype,
            reqno=reqno
        )

        return FileResponse(
            path,
            media_type="application/pdf",
            filename=f"{reqid}.pdf"
        )
    except Exception as exc:
        message = str(exc)
        if message == "NO_PENDING_REPORTS":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NO_PENDING_REPORTS",
                    "message": "No pending prints. Reports may already be dispatched via bot or agent, or no new reports are pending."
                }
            ) from exc

        if message in {"PENDING_REPORT_NOT_AVAILABLE", "LAB_REPORT_NOT_AVAILABLE"}:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": message,
                    "message": "Requested report is not available right now."
                }
            ) from exc

        raise HTTPException(
            status_code=500,
            detail={
                "code": "REPORT_FETCH_FAILED",
                "message": message
            }
        ) from exc

@app.get("/report/{reqid}")
def combined_report(
    reqid,
    reqno: Optional[str] = Query(default=None),
    printtype: str = Query(default="1"),
    chkrephead: Optional[str] = Query(default=None),
    header_mode: str = Query(default="default"),
    without_header_background: Optional[str] = Query(default=None)
):

    plain = _resolve_plain_mode(
        header_mode=header_mode,
        without_header_background=without_header_background,
        chkrephead=chkrephead
    )

    _require_dispatch_allowed(reqid=reqid, reqno=reqno)

    path = get_combined_report(
        reqid,
        include_header=not plain,
        apply_radiology_background=not plain,
        printtype=printtype,
        reqno=reqno
    )

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{reqid}.pdf"
    )

# -----------------------------
# Fetch latest report directly
# -----------------------------
@app.get("/latest-report/{phone}")
def latest_report(phone):

    rows = fetch_reqids(phone)

    if not rows:
        return {"error": "No reports found"}

    reqid = rows[0]["reqid"]

    _require_dispatch_allowed(reqid=reqid)

    path = get_combined_report(reqid)

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{reqid}.pdf"
    )


@app.get("/latest-report-meta/{phone}")
def latest_report_meta(phone):

    rows = fetch_reqids(phone)

    if not rows:
        return {"error": "No reports found"}

    reqid = rows[0]["reqid"]

    data = fetch_report_status_by_reqid(reqid)

    return _attach_dispatch_policy(data)



@app.get("/report-path/{reqid}")
def report_path(reqid):

    path = get_report(reqid)

    return {
        "reqid": reqid,
        "path": path
    }

# -----------------------------
# Report status
# -----------------------------
@app.get("/report-status/{reqno}")
def report_status(reqno):

    data = fetch_report_status(reqno)

    return _attach_dispatch_policy(data)


# -----------------------------
# Report status by ReqID
# -----------------------------
@app.get("/report-status-reqid/{reqid}")
def report_status_reqid(reqid):

    data = fetch_report_status_by_reqid(reqid)

    return _attach_dispatch_policy(data)


@app.get("/dispatch-context/{reqno}")
def dispatch_context(reqno):
    try:
        data = build_dispatch_context(reqno)
        return _attach_dispatch_policy(data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# -----------------------------
# Delivery requisitions
# -----------------------------
@app.get("/delivery/requisitions-by-date/{date}")
def delivery_requisitions_by_date(
    date,
    org_id: str = Query(default=""),
    org_ids: str = Query(default="")
):

    try:
        return fetch_requisitions_by_date(date, org_id=org_id, org_ids=org_ids)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/requisitions-by-date",
                "date": date,
                "org_id": org_id,
                "org_ids": org_ids,
                "error": str(exc)
            }
        ) from exc


# -----------------------------
# Department worklist
# -----------------------------
@app.get("/delivery/department-worklist")
def delivery_department_worklist(
    fromreqdate: Optional[str] = Query(default=None),
    toreqdate: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    department_name: Optional[str] = Query(default=None),
):

    try:
        return fetch_department_worklist(
            fromreqdate=fromreqdate,
            toreqdate=toreqdate,
            department=department,
            department_name=department_name,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/department-worklist",
                "fromreqdate": str(fromreqdate or "").strip() or None,
                "toreqdate": str(toreqdate or "").strip() or None,
                "department": str(department or "").strip() or None,
                "department_name": str(department_name or "").strip() or None,
                "error": str(exc),
            },
        ) from exc


@app.post("/delivery/department-worklist")
def delivery_department_worklist_post(filters: list[DepartmentWorklistRequest]):
    try:
        if not isinstance(filters, list) or len(filters) == 0:
            raise Exception("filters array is required")

        rows = []
        for entry in filters:
            result = fetch_department_worklist(
                fromreqdate=entry.fromreqdate,
                toreqdate=entry.toreqdate,
                department=entry.department,
                department_name=entry.department_name,
            )
            rows.append(result)

        return {"count": len(rows), "results": rows}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/department-worklist",
                "error": str(exc),
            },
        ) from exc


# -----------------------------
# Delivery status
# -----------------------------
@app.get("/delivery/status/{reqno}")
def delivery_status(reqno):

    try:
        return fetch_delivery_status(reqno)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/status",
                "reqno": reqno,
                "error": str(exc)
            }
        ) from exc


# -----------------------------
# Update delivery status
# -----------------------------
@app.post("/delivery/status/update")
def delivery_status_update(payload: DeliveryStatusUpdateRequest):

    try:
        return fetch_update_delivery_status(
            payload.reqno,
            payload.status,
            payload.channel,
            payload.message
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/status/update",
                "reqno": payload.reqno,
                "error": str(exc)
            }
        ) from exc

# -----------------------------
# Trend data (JSON) by MRNO
# -----------------------------
@app.get("/trend-data/{mrno}")
def trend_data(
    mrno,
    standardized: Optional[str] = Query(default="1"),
    include_raw: Optional[str] = Query(default="1"),
    psyntax_mode: Optional[str] = Query(default="neutral")
):

    try:
        payload = fetch_trends_data(
            mrno,
            standardized=_is_truthy(standardized),
            psyntax_mode=str(psyntax_mode or "neutral").strip().lower(),
        )
    except TrendsDataError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "trend-data",
                "mrno": mrno,
                "error": str(exc)
            }
        ) from exc

    if int(payload.get("row_count", 0) or 0) == 0:
        raise HTTPException(
            status_code=404,
            detail={
                "endpoint": "trend-data",
                "mrno": mrno,
                "error": "No trend data found"
            }
        )

    if not _is_truthy(include_raw):
        payload.pop("data", None)

    return payload


# -----------------------------
# Trend report by MRNO
# -----------------------------
@app.get("/trend-report/{mrno}")
def trend_report(mrno):

    path = get_trend_report(mrno)

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"trend_{mrno}.pdf"
    )


# -----------------------------
# Shivam tools: demographics and pricelist
# -----------------------------
@app.get("/shivam/demographics/{mrno}")
def shivam_demographics(mrno):
    try:
        return fetch_demographics_by_mrno(mrno)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "shivam/demographics",
                "mrno": mrno,
                "error": str(exc)
            }
        ) from exc


@app.post("/shivam/demographics")
def shivam_demographics_update(payload: ShivamDemographicsUpdateRequest):
    try:
        return update_demographics(payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "shivam/demographics",
                "error": str(exc)
            }
        ) from exc


@app.put("/shivam/demographics")
def shivam_demographics_update_put(payload: ShivamDemographicsUpdateRequest):
    try:
        return update_demographics(payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "shivam/demographics",
                "error": str(exc)
            }
        ) from exc


@app.get("/shivam/pricelist")
def shivam_pricelist(lab_id: str = Query(default="")):
    try:
        clean_lab_id = str(lab_id or "").strip()
        return fetch_pricelist(clean_lab_id if clean_lab_id else None)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "shivam/pricelist",
                "lab_id": str(lab_id or "").strip(),
                "error": str(exc)
            }
        ) from exc
# -----------------------------
# Simple Web UI
# -----------------------------
@app.get("/ui", response_class=HTMLResponse)
def ui():

    return """
    <html>

    <head>
        <title>NeoSoft Report Fetch</title>
        <style>
        body{
            font-family:Arial;
            margin:40px;
        }

        input{
            padding:8px;
            width:220px;
        }

        button{
            padding:8px 14px;
        }

        a{
            margin-left:10px;
        }
        </style>
    </head>

    <body>

    <h2>Patient Report Lookup</h2>

    <input id="phone" placeholder="Enter phone number">

    <button onclick="lookup()">Lookup</button>

    <div id="results" style="margin-top:20px;"></div>

    <script>

    async function lookup(){

        let phone = document.getElementById("phone").value

        if(!phone){
            alert("Enter phone number")
            return
        }

        let res = await fetch("/lookup/"+phone)

        let data = await res.json()

        let html = ""

        if(!data.latest_reports || data.latest_reports.length === 0){

            html = "<p>No reports found</p>"

        } else {

            data.latest_reports.forEach(r => {

                html += `
                <p>
                    <b>${r.reqno}</b> - ${r.reqdt}
                    <a href="/report/${r.reqid}" target="_blank">Download</a>
                </p>`
            })
        }

        document.getElementById("results").innerHTML = html
    }

    </script>

    </body>
    </html>
    """


@app.get("/outsourced-attachment/meta")
def outsourced_attachment_meta(
    reqid: str = Query(...),
    testid: str = Query(...),
    source_url: Optional[str] = Query(default=None)
):
    try:
        _require_dispatch_allowed(reqid=reqid)
        payload = fetch_attachment(reqid, testid, source_url=source_url, save=True)
        return {
            "ok": True,
            "reqid": reqid,
            "testid": testid,
            "filename": payload.get("filename"),
            "path": payload.get("path"),
            "source_url": payload.get("url"),
            "bytes": payload.get("bytes"),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/outsourced-attachment")
def outsourced_attachment(
    reqid: str = Query(...),
    testid: str = Query(...),
    source_url: Optional[str] = Query(default=None)
):
    try:
        _require_dispatch_allowed(reqid=reqid)
        payload = fetch_attachment(reqid, testid, source_url=source_url, save=True)
        filename = str(payload.get("filename") or "outsourced_attachment.pdf")
        path = str(payload.get("path") or "").strip()
        if not path:
            raise Exception("ATTACHMENT_PATH_MISSING")

        return FileResponse(
            path,
            media_type="application/pdf",
            filename=filename
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc




@app.get("/outsourced-report/classify")
def outsourced_report_classify(
    reqid: str = Query(...),
    testid: str = Query(...)
):
    try:
        _require_dispatch_allowed(reqid=reqid)
        return classify_outsourced_report(reqid=reqid, testid=testid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/outsourced-report/meta")
def outsourced_report_meta(
    reqid: str = Query(...),
    testid: str = Query(...),
    source_url: Optional[str] = Query(default=None),
    qr_url: Optional[str] = Query(default=None)
):
    try:
        _require_dispatch_allowed(reqid=reqid)
        payload = fetch_outsourced_report(
            reqid=reqid,
            testid=testid,
            source_url=source_url,
            qr_url=qr_url,
        )
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/outsourced-report")
def outsourced_report(
    reqid: str = Query(...),
    testid: str = Query(...),
    source_url: Optional[str] = Query(default=None),
    qr_url: Optional[str] = Query(default=None),
    fallback_to_base: Optional[str] = Query(default="false"),
    chkrephead: Optional[str] = Query(default=None),
    header_mode: str = Query(default="default"),
    without_header_background: Optional[str] = Query(default=None)
):
    try:
        _require_dispatch_allowed(reqid=reqid)
        payload = fetch_outsourced_report(
            reqid=reqid,
            testid=testid,
            source_url=source_url,
            qr_url=qr_url,
        )

        plain = _resolve_plain_mode(
            header_mode=header_mode,
            without_header_background=without_header_background,
            chkrephead=chkrephead,
        )

        letterhead = payload.get("letterhead") if isinstance(payload, dict) else None
        if letterhead and letterhead.get("path"):
            selected_path = str(letterhead.get("path"))
            selected_filename = str(letterhead.get("filename") or "outsourced_letterhead.pdf")
            outsourced_mode = str(payload.get("outsourced_mode") or "").strip().lower()

            if (not plain) and outsourced_mode == "attached_base":
                os.makedirs(str(ROOT_DIR / "reports" / "outsourced"), exist_ok=True)
                rendered = str((ROOT_DIR / "reports" / "outsourced" / f"WithHeader_{reqid}{testid}.pdf"))
                _apply_background_first_page(selected_path, rendered, BG_PATH)
                selected_path = rendered
                selected_filename = f"WithHeader_{reqid}{testid}.pdf"

            return FileResponse(
                selected_path,
                media_type="application/pdf",
                filename=selected_filename,
            )

        allow_base = _is_truthy(fallback_to_base)
        base = payload.get("base") if isinstance(payload, dict) else None
        if allow_base and isinstance(base, dict) and base.get("path"):
            selected_path = str(base.get("path"))
            selected_filename = str(base.get("filename") or "outsourced_base.pdf")

            if not plain:
                os.makedirs(str(ROOT_DIR / "reports" / "outsourced"), exist_ok=True)
                rendered = str((ROOT_DIR / "reports" / "outsourced" / f"WithHeader_{reqid}{testid}.pdf"))
                _apply_background_first_page(selected_path, rendered, BG_PATH)
                selected_path = rendered
                selected_filename = f"WithHeader_{reqid}{testid}.pdf"

            return FileResponse(
                selected_path,
                media_type="application/pdf",
                filename=selected_filename,
            )

        raise HTTPException(status_code=404, detail={
            "code": "OUTSOURCED_LETTERHEAD_NOT_FOUND",
            "payload": payload,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
