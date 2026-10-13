# Haas protocol references and mapping decisions

Sources inspected 2026-10-13:

- [Haas NGC machine data collection](https://www.haascnc.com/service/online-manuals/next-gen-control-electrical---service-manual/ngc---machine-data-collection.html): MTConnect 8082, Setting 143 MDC, read Q commands versus write E commands.
- [TrakHound MTConnect.NET-Haas](https://github.com/TrakHound/MTConnect.NET-Haas): current MIT-licensed reference, Ethernet.cs and HaasDataSource.cs inspected from a pinned local checkout.
- [Older TrakHound adapter](https://github.com/TrakHound/MTConnect-Agent-For-Haas): GPL-2.0 legacy implementation, inspected only; not copied or bundled.

The newer TrakHound source retains its socket, configures a 1000 ms read interval, and sleeps 500 ms after each query. Its seven-query read requires at least 3.5 seconds in these waits. This add-on waits for bounded complete reply lines, without copying those fixed sleeps. Actual elapsed read duration is reported by the UI.

The TrakHound reference equates Q500 ALARM ON with emergency stop and marks ZERO RET as NO ZERO X. Neither inference is adopted. Q500 ALARM ON becomes alarm-active and stopped execution only. Unknown controller modes remain UNKNOWN, never AUTOMATIC by default.

## Fixed MDC read allowlist

| Query | Reading | Group |
| --- | --- | --- |
| Q100 | Serial identity | Connection handshake |
| Q104 | Mode | Fast |
| Q500 | Execution, reported program and parts | Fast, whenever returned |
| Q600 3027 | Spindle RPM | Fast |
| Q201 | Active tool | Status |
| Q200 | Tool-change total | Status |
| Q402 / Q403 | Part counters | Status |
| Q101 / Q102 | Software and model | Slow |
| Q300 / Q301 | Power-on and motion totals | Slow |
| Q303 / Q304 | Last and previous cycle times | Slow |

No Q600 4109 feed inference is used: a modal/programmed F value is not confirmed actual movement. Units for axis macro positions vary with machine settings; they are excluded until verified. No true MDC e-stop or unverified temperature/power/override macro mappings are invented.

MTConnect metadata drives available sensors; namespaces are handled by local XML names. Multiple devices require selection. MQTT device identity is locally generated and stable, so repeated Haas agent UUIDs cannot merge separate machines. XML DTD/entities and oversized responses are rejected. HTTP redirects and environment proxies are disabled. No credentials are sent to machine endpoints.

## First hardware acceptance

1. Review diagnostics before adding a live machine; protocol testing is itself read-only.
2. Match mode, execution, program/tool, spindle RPM and counters against the control.
3. Confirm actual feed is labelled ACTUAL, with the probe's units, before trusting utilization.
4. Confirm alarm and e-stop independently; an alarm must never manufacture an e-stop entity.
5. Disconnect the data source and confirm machine availability and utilization unknown time.
6. Restart HA/MQTT and confirm collector continuity; restart add-on and confirm persistent identity/settings.

No live Haas controller was available in this development session. Port and protocol support alone does not certify every machine model or firmware.
