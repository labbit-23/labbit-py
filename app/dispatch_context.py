from app.report_status import fetch_report_status
from app.outsourced_report_fetcher import classify_outsourced_report


def _row_value(row, *keys):
    if not isinstance(row, dict):
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value is not None:
            return value
    return None


def _is_truthy_approved(value):
    return str(value or "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def _base_test_context(row):
    report_status = str(_row_value(row, "REPORT_STATUS", "report_status") or "").strip().upper()
    deptid = str(_row_value(row, "DEPTID", "deptid") or "").strip().upper()
    groupid = str(_row_value(row, "GROUPID", "groupid") or "").strip().upper()

    if report_status == "OUTSOURCED" or deptid == "DPT00033":
        dept_display = "outsourced"
    elif groupid == "GDEP0002":
        dept_display = "radiology"
    else:
        dept_display = "lab"

    return {
        "reqno": str(_row_value(row, "REQNO", "reqno") or "").strip(),
        "reqid": str(_row_value(row, "REQID", "reqid") or "").strip(),
        "testid": str(_row_value(row, "TESTID", "testid") or "").strip(),
        "test_name": str(_row_value(row, "TESTNM", "testnm") or "").strip() or None,
        "report_status": report_status,
        "approved": _is_truthy_approved(_row_value(row, "APPROVEDFLG", "approvedflg")),
        "dept_display": dept_display,
        "is_outsourced": dept_display == "outsourced",
        "outsourced_mode": None,
        "dispatch_route": "/report",
        "dispatch_state": "ready",
        "resolved_document_url": None,
        "resolver_status": None,
    }


def _build_send_plan(reqid, tests):
    in_report = []
    outsourced = []
    hold = []

    for item in tests:
        approved = bool(item.get("approved"))
        route = str(item.get("dispatch_route") or "").strip()
        state = str(item.get("dispatch_state") or "").strip()

        light = {
            "testid": item.get("testid"),
            "test_name": item.get("test_name"),
            "dept_display": item.get("dept_display"),
            "dispatch_state": state,
            "outsourced_mode": item.get("outsourced_mode"),
        }

        if not approved or route in {"hold", "flag_manual"}:
            hold.append(light)
            continue

        if route == "/outsourced-report":
            row = dict(light)
            row["route"] = route
            row["resolved_document_url"] = item.get("resolved_document_url")
            outsourced.append(row)
            continue

        row = dict(light)
        row["route"] = "/report"
        in_report.append(row)

    actions = []
    if in_report and reqid:
        actions.append(
            {
                "type": "send_combined_report",
                "route": "/report",
                "reqid": reqid,
                "covers_testids": [x.get("testid") for x in in_report if x.get("testid")],
            }
        )

    for item in outsourced:
        actions.append(
            {
                "type": "send_outsourced_report",
                "route": "/outsourced-report",
                "reqid": reqid,
                "testid": item.get("testid"),
                "outsourced_mode": item.get("outsourced_mode"),
            }
        )

    can_auto_dispatch = len(actions) > 0
    has_manual_flags = any(str(x.get("dispatch_state") or "") == "flagged_unavailable" for x in hold)

    outsourced_actions = [a for a in actions if str(a.get("type") or "") == "send_outsourced_report"]
    return {
        "strategy": "mixed_bucket_dispatch",
        "can_auto_dispatch": can_auto_dispatch,
        "has_manual_flags": has_manual_flags,
        "outsourced_present": len(outsourced) > 0 or any(str(x.get("dept_display") or "") == "outsourced" for x in (in_report + hold)),
        "counts": {
            "in_report": len(in_report),
            "outsourced": len(outsourced),
            "hold": len(hold),
            "actions": len(actions),
            "outsourced_actions": len(outsourced_actions),
        },
        "buckets": {
            "in_report": in_report,
            "outsourced": outsourced,
            "hold": hold,
        },
        "actions": actions,
        "outsourced_actions": outsourced_actions,
    }


def build_dispatch_context(reqno):
    status_payload = fetch_report_status(reqno)
    tests = status_payload.get("tests") if isinstance(status_payload, dict) else []
    if not isinstance(tests, list):
        tests = []

    enriched = []
    for row in tests:
        item = _base_test_context(row)

        if not item["is_outsourced"]:
            enriched.append(item)
            continue

        if not item["approved"]:
            item["dispatch_state"] = "pending_approval"
            item["dispatch_route"] = "hold"
            enriched.append(item)
            continue

        try:
            resolved = classify_outsourced_report(item["reqid"], item["testid"])
        except Exception as exc:
            item["outsourced_mode"] = "unavailable"
            item["dispatch_state"] = "flagged_unavailable"
            item["dispatch_route"] = "flag_manual"
            item["resolver_status"] = "exception"
            item["resolver_error"] = str(exc)
            enriched.append(item)
            continue

        mode = str(resolved.get("outsourced_mode") or "").strip().lower() or "unavailable"
        item["outsourced_mode"] = mode
        item["resolver_status"] = str(resolved.get("status") or "").strip() or None

        if mode == "transcribed":
            item["dispatch_state"] = "ready_transcribed"
            item["dispatch_route"] = "/report"
        elif mode == "attached_pending_resolution":
            item["dispatch_state"] = "ready_attached"
            item["dispatch_route"] = "/outsourced-report"
        elif mode == "attached_qr":
            item["dispatch_state"] = "ready_attached_qr"
            item["dispatch_route"] = "/outsourced-report"
        elif mode == "attached_base":
            item["dispatch_state"] = "ready_attached_base"
            item["dispatch_route"] = "/outsourced-report"
        else:
            item["dispatch_state"] = "flagged_unavailable"
            item["dispatch_route"] = "flag_manual"

        letterhead = resolved.get("letterhead") if isinstance(resolved, dict) else None
        if isinstance(letterhead, dict):
            item["resolved_document_url"] = letterhead.get("url")

        enriched.append(item)

    reqid = str(status_payload.get("reqid") or "").strip() if isinstance(status_payload, dict) else ""

    payload = dict(status_payload)
    payload["dispatch_tests"] = enriched
    payload["send_plan"] = _build_send_plan(reqid=reqid, tests=enriched)
    return payload
