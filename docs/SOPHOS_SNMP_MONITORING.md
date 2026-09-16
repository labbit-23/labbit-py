# Sophos XG106w Firewall SNMP Monitoring

Monitor Sophos XG106w WAN connectivity via SNMP from the local monitoring machine.

## Setup

### 1. Environment Variable

Set the SNMP community string on the local machine:

```bash
export SOPHOS_SNMP_COMMUNITY="your_community_string"
```

Add to `.env` or monitoring startup script:
```
SOPHOS_SNMP_COMMUNITY=your_community_string
```

### 2. Sophos Firewall Configuration

- **Model**: XG106w
- **Firmware**: SFOS 17.5.17 MR-17-Build837
- **SNMP IP**: 192.168.134.1
- **SNMP Port**: 161
- **SNMP Access Restricted To**: 192.168.134.185 (monitoring machine LAN IP)
- **Mode**: Multilink load-balance (both WANs may be active)

### 3. Monitoring Machine

- **Host**: sdrc-report-delivery
- **LAN IP**: 192.168.134.185
- **Tailscale IP**: 100.65.63.54
- **Role**: local (runs via `run_on = local` in services.local.ini)

## Configuration

In `services.local.ini`:

```ini
[service:sophos_firewall]
type = snmp_sophos
enabled = 1
run_on = local
category = infrastructure
label = Sophos XG106w WAN Status
host = 192.168.134.1
community = ENV:SOPHOS_SNMP_COMMUNITY
version = 2c
port = 161
timeout_seconds = 8
interval_seconds = 60
```

## Testing

### Test SNMP connectivity:

```bash
snmpwalk -v2c -c "$SOPHOS_SNMP_COMMUNITY" 192.168.134.1 1.3.6.1.2.1.1
```

Expected: System description, uptime, contact info.

### Test interface discovery (Port2, Port4):

```bash
snmpwalk -v2c -c "$SOPHOS_SNMP_COMMUNITY" 192.168.134.1 1.3.6.1.2.1.2.2.1.2
```

Expected: List of interfaces including "Port2" and "Port4".

### Test interface status:

```bash
snmpwalk -v2c -c "$SOPHOS_SNMP_COMMUNITY" 192.168.134.1 1.3.6.1.2.1.2.2.1.8
```

Expected: Status codes (1=up, 2=down, etc.) for each interface.

### Test 64-bit traffic counters:

```bash
snmpwalk -v2c -c "$SOPHOS_SNMP_COMMUNITY" 192.168.134.1 1.3.6.1.2.1.31.1.1.1.6
snmpwalk -v2c -c "$SOPHOS_SNMP_COMMUNITY" 192.168.134.1 1.3.6.1.2.1.31.1.1.1.10
```

Expected: Byte counts for in/out traffic per interface.

## Monitoring Output

The collector returns JSON to the monitoring dashboard:

```json
{
  "service_key": "sophos_firewall",
  "status": "healthy|degraded|down",
  "message": "Firewall up, WANs: ok|warning|critical",
  "latency_ms": 42,
  "payload": {
    "firewall": {
      "name": "SDRC Sophos XG106w",
      "host": "192.168.134.1",
      "reachable": true,
      "uptime": "42h 15m",
      "mode": "multilink_load_balance"
    },
    "wans": [
      {
        "name": "WAN Port2",
        "interface": "Port2",
        "ip": "192.168.1.75",
        "gateway": "192.168.1.1",
        "link_up": true,
        "internet_reachable": true,
        "latency_ms": null,
        "rx_bytes": 1234567890,
        "tx_bytes": 9876543210
      },
      {
        "name": "WAN Port4",
        "interface": "Port4",
        "ip": "192.168.37.6",
        "gateway": "192.168.37.1",
        "link_up": true,
        "internet_reachable": true,
        "latency_ms": null,
        "rx_bytes": 5432109876,
        "tx_bytes": 6789012345
      }
    ],
    "timestamp": "2026-07-02T10:30:45.123456Z"
  }
}
```

## Status Interpretation

- **healthy**: Firewall reachable, both WANs up
- **degraded**: Firewall reachable, one WAN down
- **down**: Firewall unreachable or both WANs down

## Latency Collection (Optional)

Current implementation uses SNMP only (no SSH). For per-WAN latency metrics, a separate SSH-based collector can be added:

```bash
ping -I Port2 8.8.8.8 -c 1
ping -I Port4 8.8.8.8 -c 1
ip route get 8.8.8.8
```

This requires SSH access to Sophos advanced shell and is decoupled from the main dashboard for safety.

## Logs Collection (Optional)

Sophos syslog can be collected separately (see `SOPHOS_SYSLOG_MONITORING.md` if implemented).

## Troubleshooting

### SNMP timeout or no response

1. Verify Sophos is reachable: `ping 192.168.134.1`
2. Verify SNMP community: check Sophos admin console
3. Verify firewall rule: SNMP access should be allowed from 192.168.134.185
4. Check monitoring machine logs: `tail -f /var/log/monitoring.log` (or PM2 logs)

### Port2/Port4 not found

- Verify interface names in Sophos admin: may be different firmware versions
- Manual SNMP walk to list all interfaces and their descriptions

### Traffic counters stuck at 0

- Interface may be down
- 64-bit counters require IF-MIB v2 (SNMP v2c+)
- Check `ifOperStatus` to verify link is up

## Requirements

- `pysnmp` library (auto-loaded by monitoring system)
- Network connectivity from 192.168.134.185 to 192.168.134.1:161
