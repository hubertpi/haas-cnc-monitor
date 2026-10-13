# Changelog

## 0.1.2

- Audit all VF-4SS MTConnect 1.2 observations; preserve existing signal IDs and improve names.
- Round RPM and expose display precision; set cycle timers to seconds/duration with readable UI formatting.
- Read controller software/serial metadata; report published axes without guessing installed rotary axes.
- Parse nested Haas event logs, numeric parts/loop counters, and Classic MDC framed/addressed replies.
- Add a fixed read-only MDC capability script for unverified feed/axis candidates; no extra automatic polling.
- Publish and verify a precompiled image on the release branch before advancing the add-on store version.

## 0.1.1

- Fix live collection crashing after a machine is saved: provide the collector mode required by the manager.
- Add integration regression tests for the live factory, scheduling and MQTT publication with both MTConnect and MDC.
- Machine identities, saved endpoints and retained data remain unchanged.

## 0.1.0 — development

- Separate Haas variant derived from FANUC CNC Monitor 0.3.4.
- Read-only MTConnect collection for NGC or external agents.
- Persistent MDC TCP collection for compatible Ethernet or raw serial gateways.
- Shared shifts and breaks, overnight accounting and seven-day minute retention.
- MQTT discovery, unavailable-state handling and durable reconnect journal.
- Appearance, sorting and shared editable floor title.
- No CNC controls, arbitrary macro queries or macro writes.
