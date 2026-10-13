# Haas sensor audit — 0.1.3

Audited against the supplied VF-4SS probe/current structure (MTConnect 1.2, control software REL-100.19.000.1123). Test fixtures preserve its structure but replace identifying serial/program/counter data. All existing machine and signal IDs are preserved; only labels, types, metadata and display precision change. The nested event log adds a previously missed reading.

## Published machine observations

| Source ID | Display name | Type / unit | Interpretation and correction |
| --- | --- | --- | --- |
| avail | Machine availability | Text enum | AVAILABLE is agent-reported availability, separate from monitor connection health. UNAVAILABLE marks the connection offline. |
| mode | Controller mode | Text enum | AUTOMATIC, MANUAL_DATA_INPUT, etc. Unknown modes do not establish production. |
| rstat | Execution | Text enum | ACTIVE confirms execution, not cutting or feed motion by itself. |
| estop | Emergency stop | Binary | ARMED = false; TRIGGERED = true. Other values do not become binary states. |
| ncprog | Active program | Text | This endpoint publishes a filename. Preserve it; do not invent an O-number. Numeric O-numbers on other endpoints retain O formatting. |
| rovrd | Rapid override | % | Override percentage, not actual axis velocity. Zero remains valid. |
| fdovrd | Feed override | % | Override percentage, not feedrate. 100 means normal override. Zero remains valid. |
| sspeed | Spindle RPM | rpm, 0 decimal places | Actual spindle speed is declared under Controller. Preserve the existing mtc_sspeed ID and label it clearly. Do not invent a spindle index. |
| ssovrd | Spindle override | % | Operator override percentage; not actual RPM. |
| tcycle | Current cycle elapsed | Duration, seconds | 107 means 01:47, not a cycle count. Use native SECOND units when units is absent. |
| lcycle | Last cycle duration | Duration, seconds | 4 means 00:04. The source really reports 4 seconds; software cannot infer a longer production cycle. |
| cyremtim | Cycle time remaining (last cycle estimate) | Duration, seconds | The source describes Last Cycle Time Remaining. Zero is valid; this is an estimate, not measured future completion time. MTConnect ACCUMULATED_TIME defaults to SECOND. |
| lpremain | Loops remaining | Integer | Remaining loops, not elapsed time. Never total_increasing: it normally decreases. |
| m30c1 | M30 parts counter 1 | Integer, total_increasing | Numeric count, resettable at the control. It does not establish good/reject counts or OEE quality. |
| m30c2 | M30 parts counter 2 | Integer, total_increasing | Independent resettable M30 count. Do not merge it with counter 1. |
| macdsp1 | Macro display 1 | Raw text | A configured display. Probe does not identify the assigned macro; do not treat its contents as actual feed, a tool, a temperature or a macro address. |
| macdsp2 | Macro display 2 | Raw text | Same rule as display 1. A value of zero is valid. |
| aalarms | Active alarms | Text | Preserve NO ACTIVE ALARMS or the controller's alarm text. An alarm does not imply e-stop. |
| elog | Latest machine event | Text + details array | Parse nested Haas/EventLogEntries. State is the latest message; details preserve the reported entries and timestamps. Previously omitted. |
| mcond | Machine condition · level/code | Text | Preserve Normal/Fault/Warning and native code/message. Normal with empty XML text is still a valid condition. Existing condition IDs remain stable. |

RPM is rounded in published numeric state and has suggested_display_precision 0. Percentages use one decimal place; duration readings use at most two. Duration state remains numeric seconds for Home Assistant, with duration device class. The add-on UI additionally shows MM:SS or H:MM:SS. Native source timestamps are diagnostic; local collection timestamps and local schedule time drive utilization, so the endpoint's 1970 timestamps do not move shifts into 1970.

## Connection metadata

Model, manufacturer, serial number and softwareVersion are read from Description. Agent version comes from Header and is not called controller firmware. Axis counts are based only on published Linear/Rotary axis components; spindle components are excluded. This supplied probe has no axis components or positions. Therefore the UI says axis components are not published; it must not infer three or five configured axes from VF-4SS or add zero-filled rotary sensors.

## Monitor observations and controls

- Machine connected: monitor transport/read health, distinct from reported machine availability.
- Collector mode/error: diagnostics; haas / OK on successful collection.
- Diagnostic boost active/remaining: local scheduler state and seconds remaining.
- OEE - 24hr, Week, Month, and numbered shifts: production utilization. Use automatic mode + ACTIVE execution + positive actual feed where published; otherwise use the approved automatic mode + ACTIVE fallback. Automatic warmup programs count in fallback mode. The rule is exposed in sensor attributes. Temporarily unavailable published feed and missing/ambiguous mode or execution remain unknown.
- Polling switch and interval/duration numbers: configure only the add-on; never send CNC control or write commands.

## Additional read-only routes: findings and limits

The endpoint's probe contains no PATH_FEEDRATE/ACTUAL, axis positions, axis loads, tool number, temperature or power observations. current and sample refer to that declared schema; sample adds history, not additional sensor types. Device-specific paths select a device; they do not unlock hidden items. No alternative undocumented HTTP endpoints are probed.

Haas documents MDC Q600 reads and the following candidates:

- #62593 is called Active Feed Rate in the NGC **lathe** macro table. The public mill table inspected does not establish its availability on this VF-4SS or software 100.19, nor its units/update/idle behavior. It is a candidate for a read-only hardware check, not an enabled feed/OEE source.
- #3022 is a feed timer. A timer increment is not an instantaneous actual feedrate; no unit or production inference is enabled without verification.
- #5041–#5046 are work-coordinate positions. Haas documents that they cannot be read while the machine is moving. They cannot be used to reconstruct a dependable live feedrate; returned zeros do not establish installed axes, and linear/rotary units must be confirmed.
- #4109/modal F is programmed feed and is deliberately not substituted for actual motion.

The bounded `tools/probe_haas_mdc.py` script checks these fixed read queries through an existing Setting 143/gateway port. It never changes that setting, writes a macro, changes the displayed macro assignment, or automatically enables candidate readings as sensors/OEE. The normal collector still polls only its established query list; diagnostic candidates add no automatic traffic.

```sh
.venv/bin/python tools/probe_haas_mdc.py --host MACHINE_IP --port EXISTING_MDC_PORT --output haas-mdc-capabilities.json
```

Run on a host on the machine network with this project's Python dependencies. Compare supported candidate values with the control during idle, warmup, feed hold and cutting before deciding their semantics. Do not change production programs for this check. The script saves unverified raw replies, including errors, to the named file.

## Primary sources

- [Haas NGC machine data collection](https://www.haascnc.com/service/online-manuals/next-gen-control-electrical---service-manual/ngc---machine-data-collection.html): probe/current/sample, Q600 reads and separate E writes.
- [Haas NGC mill macros](https://www.haascnc.com/service/online-operator-s-manuals/mill-operator-s-manual/mill---macros.html): timers, work coordinates and motion limitations.
- [Haas NGC lathe macros](https://www.haascnc.com/service/online-operator-s-manuals/lathe-operator-s-manual/lathe---macros): Active Feed Rate candidate #62593.
- [MTConnect 1.2 Components specification](https://docs.mtconnect.org/MTC_Part_2_Components%2BV1.2.pdf): ACCUMULATED_TIME uses SECOND.
- [Home Assistant MQTT sensor](https://www.home-assistant.io/integrations/sensor.mqtt): numeric state, duration class, precision and device metadata.

TrakHound's reference was also inspected. Its normal Q600 reads cover axis positions 5041–5043 and spindle RPM 3027; it does not supply an actual-feed measurement. The add-on is a direct HTTP client when using MTConnect, so this probe's omissions are controller-published limitations rather than a TrakHound filter.
