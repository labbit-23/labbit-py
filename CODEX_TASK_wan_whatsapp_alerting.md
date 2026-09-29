# Wire up WhatsApp alerting for WAN (Sophos) outages — handover brief

Spans `labit-py` (this repo: `services.ini`, `app/monitoring_agent.py`) and confirms an
existing `labit-main` endpoint (no change expected there). Written 2026-09-29 from the
infra side, after the user asked "Is WhatsApp set up to fire on any issue in WANs?" and
the answer was: the code path exists, but nothing is configured, so nothing fires today.

## What already exists (do not rebuild)

- `app/monitoring_agent.py`'s `maybe_alert_wan_transitions(check_result, cfg,
  monitoring_cfg)` (called at lines ~243 and ~301) watches a check's `payload.wans` list
  for `link_up` transitions per WAN interface, and for total SNMP/firewall
  unreachability, and calls `_send_ops_alert()` on a flip. It is opt-in per service
  (`alert_on_wan_change=1` in that service's `services.ini` section) and survives
  process restarts via a state file (`MONITORING_ALERT_STATE_PATH`, default
  `logs/wan_alert_state.json`) so a monitoring_agent restart never fires a false
  "just changed" alert.
- `_send_ops_alert()` POSTs to `labit-main`'s `/api/internal/whatsapp/staff-notify`
  (confirmed deployed, `app/api/internal/whatsapp/staff-notify/route.js`) using the
  `ops_status_alert` WhatsApp template (already approved 2026-09, generic
  Category/Subject/Status/Time params, reusable beyond WAN alerts). Auth is a shared
  token compared against `WHATSAPP_INTERNAL_SEND_TOKEN` (falls back to
  `WHATSAPP_EXTERNAL_INGEST_TOKEN`) in labit-main's env — read that value from
  wherever labit-main's secrets are kept (backend/.env on VPS1), do not invent a new one.
- The route requires `lab_id` and resolves the destination WhatsApp number from
  `labs_apis.templates.bot_flow.report_notify_number` or `labs.internal_whatsapp_number`
  for that lab — confirm one of those is actually set for the SDRC lab_id before relying
  on this, otherwise the call 422s with "No internal notify number configured".

## What is missing (checked live, 2026-09-29)

1. **No Sophos/WAN check in `services.ini`.** Every current `[service:*]` entry polls
   Mirth, Orthanc, Oracle, Tomcat, Supabase or app health endpoints — none polls the
   Sophos firewall or produces a `wans` payload. The only thing that currently reads
   Sophos data at all is `labit-main`'s `SophosWanCard.js`, a passive staff dashboard
   display fed by a *different* process (a Flask API on port 5000, not this monitoring
   agent) — that display path is unrelated to this alerting path and out of scope here.
2. **`[monitoring]` has no `alert_notify_url` / `alert_notify_token`.** Even with a WAN
   check configured and `alert_on_wan_change=1` set, `_send_ops_alert()` logs
   "Ops alert skipped ... not configured" and sends nothing, because these two keys are
   absent from the `[monitoring]` section today.

## To build

1. **A Sophos WAN check.** Add a `type=http_json` (or a new check type if the existing
   ones don't fit — see `app/monitoring_checks.py run_check()`) service block, e.g.
   `[service:sophos_wan]`, that calls the Sophos XML API
   (`https://192.168.134.1:4444/webconsole/APIController`, confirmed live and reachable
   from this host per `monitoring/SOPHOS_INFRA_HANDOFF_2026-09-29.md` in
   `sdrc-infrastructure`) and returns a `payload.wans` list shaped
   `[{"interface": "Port2", "name": "Airtel", "link_up": true}, {"interface": "Port4",
   "name": "BSNL", "link_up": true}]` — `maybe_alert_wan_transitions()` reads exactly
   this shape. Use a dedicated restricted API admin credential per that handoff doc's
   own instruction ("rotate it and create a dedicated restricted monitoring/API
   administrator before putting credentials into a service"), stored outside Git (same
   pattern as `SOPHOS_SSH_PASSWORD` in the existing `monitoring/sophos_api.py` in
   `sdrc-infrastructure`, or this repo's own `.env` convention — check
   `docs/SOPHOS_SNMP_MONITORING.md` in this repo first, it may already have partial
   groundwork). Set `alert_on_wan_change=1` on this service.
2. **Configure the alert destination.** Add to `[monitoring]` in `services.ini`:
   `alert_notify_url = https://lab.sdrc.in/api/internal/whatsapp/staff-notify`,
   `alert_notify_token = <WHATSAPP_INTERNAL_SEND_TOKEN value>`, and confirm
   `alert_lab_id` (or the existing `lab_id`) resolves to a lab row with a working
   internal notify number (see above).
3. **Add the new service to a `[group:*]` if it should also show up in the existing
   grouped-severity alerting (internet_edge / local_edge etc.)** — check whether that's
   wanted or whether the per-WAN WhatsApp alert alone is sufficient; the two mechanisms
   are separate in this codebase (group failure_condition alerting vs.
   `maybe_alert_wan_transitions`), don't conflate them.
4. **Test the flip, not just the wiring**: simulate a down/up transition (or literally
   unplug one WAN briefly during a maintenance window) and confirm one WhatsApp message
   arrives per transition, not one per poll interval (the state file is what prevents
   repeat-firing — verify it actually persists across a `pm2 restart`).

## Not in scope here

- The passive Sophos dashboard card in `labit-main` (`SophosWanCard.js`) — already works,
  unrelated data path, no change needed.
- SNMP-based collection for historical graphs (`monitoring/SOPHOS_INFRA_HANDOFF_2026-09-29.md`'s
  own "recommended continuation" section) — a separate, larger piece; this brief is
  scoped to just the alert-on-transition path using the XML API's live link status,
  which is simpler and doesn't need SNMP at all.
- The AP/guest Wi-Fi audit from the same handoff doc — unrelated, still pending, do not
  touch AP/QoS/SSID/VLAN config while doing this.

## Constraints

No secret belongs in Git (the handoff doc's own words) — the Sophos API credential and
the WhatsApp token both go in the deployed env, never committed. Test against the live
firewall carefully; this is production network infrastructure for the whole lab.
