# Haas CNC Monitor for Home Assistant

A separate, read-only Haas variant of [FANUC CNC Monitor](https://github.com/hubertpi/fanuc-cnc-monitor), based on version 0.3.4. Initial development version: **0.1.2**. Software tests do not replace acceptance on your actual machines.

## Supported connections

| Control / interface | Connection | Setup |
| --- | --- | --- |
| Haas Next Generation Control (NGC) | Built-in MTConnect HTTP agent | Usually TCP 8082; specify a device name if there is more than one device |
| Haas MDC Ethernet interface | Persistent raw TCP connection | Port configured in Setting 143; Haas examples use 5051 |
| Classic Haas Control (CHC) with RS-232 MDC | Raw serial-to-Ethernet gateway | Gateway's IP/port; baud rate and serial framing must match the control |
| Existing external MTConnect agent | MTConnect HTTP | Agent's IP/port and device name |

Classic controls do not gain Ethernet MDC merely by installing this add-on. Use a compatible interface or gateway. Direct USB/RS-232 access from the add-on is not implemented in this first version. Features and readings depend on control software and installed options. MDC and MTConnect are selected separately for each machine, so both can coexist on one floor.

## Read-only by construction

The MTConnect collector sends HTTP GET requests to `probe` and `current`. The MDC collector accepts only a fixed list of documented `?Q` queries, including `?Q600 3027` for spindle RPM. There is no arbitrary command or macro-address input. No `?E` macro writes, program transfer, resets, cycle starts, offset edits, or CNC controls are implemented. The polling switch/numbers in Home Assistant configure only this add-on's scheduler.

An alarm is not treated as an emergency stop. True e-stop is published only when an MTConnect `EMERGENCY_STOP` observation explicitly reports `ARMED` or `TRIGGERED`. Unknown values and unsupported readings do not become fabricated states.

## Installation and adding machines

1. Add this repository to Home Assistant's add-on store and install **Haas CNC Monitor**.
2. Start in `simulation` mode to review the UI without contacting any equipment.
3. For real machines, choose `mode: haas` and enable `hardware_testing_enabled` in add-on configuration.
4. Open the web interface, add a machine, select **MTConnect** or **MDC**, then enter its address and port.
5. Test the connection, review the detected controller, give it a name, and save.

Use discovery on a selected subnet/port to find candidates. Configured endpoints are excluded. An open port is only a candidate: a successful protocol test is required before saving. Manual addresses and nonstandard ports remain supported.

MQTT is obtained from Home Assistant's MQTT service, or explicit broker settings. The add-on's MQTT namespace and discovery prefix are separate from FANUC's, so the two add-ons can run together. Data and machine identities persist under `/data` and are included in Supervisor backups. Version 0.1.2 and later use a precompiled registry image. Release candidates are built and verified for anonymous download on a release branch before the store version on main advances.

## Sensors and polling

MTConnect uses the agent's probe metadata and creates sensors from actual available observations. Unavailable or invalid readings are omitted. Programmed and actual feed are distinct; only an explicit `PATH_FEEDRATE` / `ACTUAL` reading feeds utilization. Each reported spindle has its own RPM entity. Generic data items retain their source identifier, type-dependent group and published units. Temperature readings of zero are unavailable; zero spindle RPM and zero feedrate remain valid.

MDC reads mode, Q500 status/program/parts, active tool, tool-change count, two part counters, spindle RPM via macro 3027, power-on/motion timers and last/previous cycle times. Unsupported optional queries are recorded and not retried continuously during the same connection. A `STATUS BUSY` response confirms execution but does not reveal a new program number. Program/parts are available only when the control supplies them in Q500. No axis positions are enabled without verified units.

Defaults: 1 second fast/status reads and 60 second diagnostics. Temporary diagnostic boost expires after 10 minutes by default; frequency and duration are adjustable. Calls are sequential per machine; a slow response delays the next read rather than queueing bursts. MTConnect uses a pooled HTTP connection when the agent permits keep-alive; servers may close it. MDC retains its TCP socket until error, endpoint change, stop or shutdown. MTConnect `current` includes the agent’s complete cached observations; the group intervals control which readings this add-on processes and publishes, not the controller’s internal sampling. Faster polling does not guarantee faster internal controller updates.

## Shifts and OEE

The inherited shift editor supports multiple shifts, weekday schedules, breaks and overnight periods. Defaults are Monday–Friday, 06:00–16:00 and 16:00–02:00 America/Chicago, with the established breaks. Overnight shifts belong to the day they started.

The inherited OEE labels represent **confirmed feed-motion utilization**, not full availability × performance × quality OEE. Production requires automatic mode, active execution and actual feed greater than zero. Warmup spindle rotation and MDI are excluded. Missing observations remain unknown; they are never credited as production. Scheduled unknown time remains in the denominator.

The standard MDC queries here do **not** provide actual feedrate. Programmed F values and axis-position changes are not substituted. Thus feed-based OEE remains unavailable for MDC-only machines. MTConnect machines without all required observations, or with ambiguous multiple paths, also remain unavailable. Diagnostics retain the detailed accounting and coverage. Once a period has valid observations, monitoring gaps remain in its scheduled denominator.

Collection and accounting continue when HA or MQTT is down. Only seven days of minute totals are retained; current shift/week/month summaries remain bounded. MQTT journals retain latest states/discovery, not an unlimited raw-sample history.

## Development

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r haas_monitor/requirements.lock
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
npm ci --ignore-scripts
npm run test:frontend
python tools/install_test_browser.py
npm run test:ui
.venv/bin/python tools/run_simulation.py --data ./data/simulation
```

The Chromium installer prints `UI_CHROMIUM_EXECUTABLE`; export that path before `npm run test:ui` when running locally. CI handles it automatically. The tests use local protocol fixtures and an isolated local MQTT broker; they never connect to a CNC. Before production use, compare diagnostics with the real control and capture `probe/current` or MDC responses to verify mappings, cadence and disconnect behavior.

See the complete [sensor audit](docs/sensor-audit.md), [Haas protocol notes](docs/haas-protocols.md), [validation](docs/validation.md) and [NOTICE](NOTICE.md).
