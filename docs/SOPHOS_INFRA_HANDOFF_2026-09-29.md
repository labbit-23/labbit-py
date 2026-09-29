# Sophos / Wi-Fi infrastructure handoff

Date: 2026-09-29
Host: `sdrc-integrations` (`192.168.134.62`)

This note records the live Sophos and WAN findings for the next infrastructure discussion.

## Sophos verified live

- Firewall: Sophos XG106w, `192.168.134.1`
- Firmware/API: `SFOS 17.5.17 MR-17-Build837`, XML API `1705.1`
- API endpoint: `https://192.168.134.1:4444/webconsole/APIController`
- API access is enabled.
- API-allowed endpoints: `192.168.134.85` and `192.168.134.62`
- Port2 / Airtel: WAN, gateway `192.168.1.1`, 1000 Mbps full duplex
- Port4 / BSNL: WAN, gateway `192.168.37.1`, 1000 Mbps full duplex
- Both gateways are active and health-check `8.8.8.8`.

The general WebAdmin credential was used for testing. Rotate it and create a dedicated restricted monitoring/API administrator before putting credentials into a service. No secret belongs in Git.

## Historical WAN usage

The Sophos reporting layer provides daily, weekly, and monthly interface/WAN graphs. The supplied last-month report showed:

- Combined WAN maximum: `10,178.25 Kbit/s` (~`10.18 Mbps`)
- Combined WAN average: `3,968.30 Kbit/s` (~`3.97 Mbps`)
- Latest aggregate point: `7,989.08 Kbit/s` (~`7.99 Mbps`)
- Maximum download: `9.15 Mbps`
- Maximum upload: `6.09 Mbps`
- Airtel average: `2.56 Mbps`
- BSNL average: `1.40 Mbps`
- Interface errors, drops, and collisions: zero in the month view

Airtel is 100 Mbps and BSNL is expected to be at least 100 Mbps. The observed peak was therefore about 5% of the combined minimum capacity; there is no evidence of WAN saturation in the available month.

The Sophos `QoSSettings/MaxLimit` is `100000 KBps` (~800 Mbps), a global WAN-zone QoS ceiling. It is already above the combined service capacity and should remain unchanged for full throttle.

## SNMP status

The XML API confirmed UDP/161, SNMP v1/v2c, and manager access from both `.85` and `.62`. SNMP responded from this machine. The XG rejected 64-bit `ifHC*` counters with `noSuchName`, but older 32-bit `ifInOctets`/`ifOutOctets` counters worked.

A live 15-second sample showed approximately:

- Port2: `12.39 Mbps` inbound + `0.65 Mbps` outbound
- Port4: `2.01 Mbps` inbound + `0.55 Mbps` outbound

The running monitoring agent is not yet wired with the SNMP community in its environment, and `pysnmp` is not installed in its service virtualenv. The temporary test dependency was installed under `/tmp` only. SNMP monitoring is not deployed until the community is stored outside Git and the service is restarted/tested.

## AP and guest Wi-Fi audit pending

The lab has access points for staff and guest Wi-Fi connected through this Sophos, with guest/device throttling reportedly configured at the AP/device level. Audit without changing anything first:

1. AP inventory, management IPs, model/firmware, uplink ports, and PoE/switch path.
2. SSID-to-VLAN/network mapping for staff, guest, and clinical/device SSIDs.
3. Guest isolation and firewall policy boundaries.
4. Per-device/per-SSID bandwidth limits and whether they are actually applied.
5. AP channel/interference, client counts, retries, and uplink errors.
6. Agreement between Sophos WAN graphs, AP controller graphs, and switch counters.

Do not change QoS, SSIDs, VLANs, or AP limits during the initial audit.

## Recommended continuation

- Query the Sophos reporting layer for existing day/week/month data.
- Add a 60-second SNMP collector for future history, storing counters and derived Mbps outside the firewall.
- Record peak and 95th-percentile inbound/outbound usage separately for Airtel and BSNL.
- Verify the exact BSNL speed before any QoS change.
