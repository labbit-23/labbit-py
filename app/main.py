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
from app.delivery_api import (
    fetch_requisitions_by_date,
    fetch_delivery_status,
    fetch_update_delivery_status,
)
import logging
import os
import configparser
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

config = configparser.ConfigParser()
ROOT_DIR = Path(__file__).resolve().parents[1]
config.read(ROOT_DIR / "config.ini")

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


def _is_truthy(value):
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


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

    return data



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

    return data


# -----------------------------
# Report status by ReqID
# -----------------------------
@app.get("/report-status-reqid/{reqid}")
def report_status_reqid(reqid):

    data = fetch_report_status_by_reqid(reqid)

    return data


# -----------------------------
# Delivery requisitions
# -----------------------------
@app.get("/delivery/requisitions-by-date/{date}")
def delivery_requisitions_by_date(date):

    try:
        return fetch_requisitions_by_date(date)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "endpoint": "delivery/requisitions-by-date",
                "date": date,
                "error": str(exc)
            }
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
