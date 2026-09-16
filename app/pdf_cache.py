"""Generic, backend-agnostic, short-TTL PDF cache with a same-key lock to
dedupe concurrent renders of the same document.

Context (2026-09-03): report_backend.py's labit-core PDF path
(fetch_pdf_path, active since the Aug 30 cutover) was calling
labit_tools.fetch_dispatch_pdf() live on every single request, with zero
caching -- the older report_fetcher.py combined-cache (60s TTL,
content-addressed) exists but is now dead code on the active path.
Confirmed root cause of a wave of WhatsApp delivery failures: report
rendering measured at 6-8s+ with no concurrent load, and WhatsApp's own
media-fetch bot (facebookexternalhit) was observed retrying the same
reqid's report URL 2-5 times over 1-4 minutes before giving up -- nginx
access logs show repeated cases of a successful 200 render followed by a
499 (client gave up) only ~15-20s later for the SAME reqid, which a short
cache would have served instantly instead of re-rendering cold.

Deliberately NOT reusing report_fetcher.py's private cache helpers --
that module is Shivam-specific and meant to become dead code as the
cutover completes. This is a fresh, backend-agnostic cache any PDF-
fetching facade in this app can use (labit-core today; nothing stops
report_fetcher.py from adopting it too later).

Freshness: the cache key is content-addressed (reqno + scope + testids,
see report_backend.py's caller) and the reuse window defaults to a short
60s -- long enough to absorb WhatsApp's own retry cadence and our own
immediate resend, short enough that a report re-approved moments earlier
is never served stale for more than a minute. This is NOT a general
long-lived cache.
"""

import os
import time
import hashlib
from pathlib import Path

CACHE_DIR = os.environ.get("PDF_CACHE_DIR") or str(
    Path(__file__).resolve().parents[1] / "reports" / "_dispatch_pdf_cache"
)
REUSE_WINDOW_SECONDS = int(os.environ.get("PDF_CACHE_REUSE_WINDOW_SECONDS", "60"))
LOCK_WAIT_SECONDS = float(os.environ.get("PDF_CACHE_LOCK_WAIT_SECONDS", "20"))
LOCK_POLL_SECONDS = float(os.environ.get("PDF_CACHE_LOCK_POLL_SECONDS", "0.2"))


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _paths_for_key(key: str):
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    pdf_path = os.path.join(CACHE_DIR, f"{digest}.pdf")
    meta_path = os.path.join(CACHE_DIR, f"{digest}.meta")
    lock_path = os.path.join(CACHE_DIR, f"{digest}.lock")
    return pdf_path, meta_path, lock_path


def _is_fresh(pdf_path, meta_path, window_seconds):
    if not os.path.exists(pdf_path) or not os.path.exists(meta_path):
        return False
    try:
        created_at = float(open(meta_path).read().strip())
    except Exception:
        return False
    age = time.time() - created_at
    return 0 <= age <= max(0, window_seconds)


def _write_meta(meta_path):
    with open(meta_path, "w") as f:
        f.write(str(time.time()))


def _acquire_lock(lock_path, wait_seconds, poll_seconds):
    deadline = time.time() + max(0, wait_seconds)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            return fd
        except FileExistsError:
            if time.time() >= deadline:
                # Lock never freed (stale from a crashed renderer) -- steal it
                # rather than making every subsequent caller wait out a full
                # timeout too. A concurrent renderer finishing after this
                # point still lands a correct file; os.replace() is atomic.
                try:
                    os.unlink(lock_path)
                except FileNotFoundError:
                    pass
                continue
            time.sleep(max(0.05, poll_seconds))


def _release_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        os.unlink(lock_path)
    except Exception:
        pass


def get_or_render(key: str, render_fn, *, reuse_window_seconds=None):
    """Return PDF bytes for `key`: from cache if a render for this exact key
    completed within the reuse window, else call render_fn() (zero-arg,
    returns bytes) and cache the result for the next caller.

    A same-key lock means a second caller for the same key while a render
    is in flight (e.g. our own send-time prefetch overlapping WhatsApp's
    fetch, or two of WhatsApp's own retry attempts arriving close together)
    waits for that one render instead of triggering a redundant one -- this
    is what protects against compounding load under the very concurrency
    that was causing renders to slow down in the first place.

    render_fn's exceptions propagate uncaught and nothing is cached for a
    failed render, so callers' existing fallback/error handling (e.g.
    report_backend.py's LabitCoreReportNotFound -> Shivam fallback) is
    unaffected.
    """
    window = REUSE_WINDOW_SECONDS if reuse_window_seconds is None else reuse_window_seconds
    _ensure_dir()
    pdf_path, meta_path, lock_path = _paths_for_key(key)

    if _is_fresh(pdf_path, meta_path, window):
        with open(pdf_path, "rb") as f:
            return f.read()

    fd = _acquire_lock(lock_path, LOCK_WAIT_SECONDS, LOCK_POLL_SECONDS)
    try:
        # Someone else may have rendered and released the lock while we waited.
        if _is_fresh(pdf_path, meta_path, window):
            with open(pdf_path, "rb") as f:
                return f.read()
        pdf_bytes = render_fn()
        tmp_path = f"{pdf_path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp_path, pdf_path)
        _write_meta(meta_path)
        return pdf_bytes
    finally:
        _release_lock(fd, lock_path)
