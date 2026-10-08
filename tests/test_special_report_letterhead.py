"""/special_report asks for letterhead ON unless a plain header is requested (2026-10-08)."""
from __future__ import annotations

from unittest import mock

from app import main


def _call(**kwargs):
    with mock.patch.object(main, "_require_dispatch_allowed"), \
         mock.patch.object(main, "fetch_pdf_path", return_value="/tmp/x.pdf") as fetch, \
         mock.patch.object(main, "FileResponse", side_effect=lambda *a, **k: "ok"):
        main.special_report("req-1", reqno="R1", **{"header_mode": "default", "without_header_background": None, "chkrephead": None, **kwargs})
    return fetch.call_args.kwargs


def test_unset_letterhead_means_on():
    assert _call()["letterhead"] is True
    assert _call()["scope"] == "special"


def test_explicit_plain_requests_still_mean_no_letterhead():
    assert _call(header_mode="plain")["letterhead"] is False
    assert _call(chkrephead="0")["letterhead"] is False
    assert _call(without_header_background="true")["letterhead"] is False
