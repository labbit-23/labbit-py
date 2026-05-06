# Dispatch Direction: Per-Test + QR-First Resolution

Date: 2026-05-05
Owner: Labbit/Py dispatch team
Status: Draft for weekend implementation and staged rollout

## Core Revelation
This is not only an outsourced-report fix.

The same `DgReportingVF` test-level path used for transcribed/special reports should become the primary dispatch unit for LAB dispatch too.

Target model:
- Dispatch by `reqid + testid` first.
- Requisition-level combined PDF becomes fallback/backward compatibility.

## Why this matters
Current requisition-level batching hides test-level truth:
- which test was actually dispatched
- which test is still pending approval
- which test failed in send/resolution

Per-test dispatch gives:
- exact send ledger per test
- accurate pending list that can be appended to send context/PDF
- clean scope for page-wise/per-test QR code strategy

## Proposed Architecture
1. Resolve report URL at test-level (LAB + OUTSOURCED + selected RAD):
   - first try direct `DgReportingVF` test path
   - fallback to existing attachment/QR chain where applicable
2. Send using existing template (no template change in phase-1).
3. Persist dispatch events at test granularity.
4. Build requisition outcome as aggregate of test outcomes.

## Resolution Modes (store per test)
- `direct_transcribed` (test-level DgReportingVF)
- `qr_resolved` (Att -> QR -> letterhead)
- `attachment_fallback` (Att used directly)
- `combined_fallback` (legacy requisition PDF fallback)

## Rollout Plan (non-breaking)
1. Shadow mode
- Compute per-test candidate URLs and dispatch decisions.
- Log only; keep current send unchanged.

2. Dual-write mode
- Keep current requisition send live.
- Also persist per-test dispatch state and resolution mode.

3. Controlled switch
- Enable per-test send for `OUTSOURCED` first.
- Then enable for LAB tests where direct test-level URL is reliable.
- Keep combined fallback on any resolution failure.

4. Full adoption
- Per-test as default dispatch path.
- Requisition-level dispatch retained only as explicit fallback.

## Logging Requirements (must-have)
Per test attempt store:
- `reqno`, `reqid`, `testid`, `test_name`
- `dispatch_type` (`lab`, `outsourced`, `radiology`)
- `resolution_mode`
- `resolved_document_url`
- `status`, `error_code`, `error_message`
- `attempted_candidates` (ordered)
- timestamps, actor/service

## Pending List Behavior
At send time, compute pending tests for same requisition:
- approved but not dispatched tests
- not approved tests

Expose this as:
- appended metadata in dispatch log
- optional note block in dispatch communication/report context

## QR Direction
Per-test dispatch enables per-test QR management:
- each test report/page can carry its own QR provenance
- QR can be used as audit pointer for what exact test document was sent


## Current Delivery Rule (implemented in py)
- If outsourced test resolves as `transcribed`, dispatch uses normal combined `/report/{reqid}` path (not separate outsourced PDF).
- If outsourced resolves as `attached_qr` or `attached_base`, dispatch uses `/outsourced-report` path.
- If outsourced resolves as `unavailable`, dispatcher flags item (`OUTSOURCED FLAGGED`) and does not send automatically.

## Extraction TODO (next)
Move transcribed test-level URL builder/prober logic out of outsourced resolver into future per-test `/report` generation module.
That module will later power LAB per-test PDF generation and page-level QR workflows.

## Existing Code Status
Already in place:
- Outsourced resolver: `app/outsourced_report_fetcher.py`
- Attachment + approvaldisplay traversal: `app/attachment_fetcher.py`
- Outsourced endpoint routes: `app/main.py`
- Dispatcher branch support: `app/delivery_engine.py`

## Weekend Implementation Checklist
1. Add feature flag for per-test dispatch (`outsourced_only` -> `outsourced_and_lab`).
2. Add test-level dispatch event persistence.
3. Add pending-list aggregation in dispatch metadata.
4. Pilot on controlled requisition set.
5. Compare with legacy flow and monitor regressions.

## Backout
Single switch to legacy requisition-level combined dispatch path.
All per-test logs retained for diagnostics.

## TODO: Graceful API Deploy (PM2 Cluster Reload)
- Evaluate moving `labbit-api` from PM2 `fork` to `cluster` mode (`instances=2` initially) and use `pm2 reload` instead of hard restart.
- Before rollout, audit codepaths for process-local state assumptions:
  - in-memory caches
n  - session reuse behavior
  - filesystem write/read race conditions across workers
- Validate long-running report calls under `kill_timeout` and set safe timeouts for zero/minimal interruption.
- Add deploy runbook steps: `reload --update-env` + post-reload health checks.
