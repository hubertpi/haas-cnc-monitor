# Validation — 0.1.2 release candidate

Local checks completed on 2026-10-13:

- 80 Python tests covering both live collector factories, fixed query allowlist, protocol fixtures, stable identities, MQTT outages, shifts/retention, and the supplied Haas 1.2 controller layout.
- 10 frontend tests covering preferences, sorting, RPM/duration formatting and truthful connection metadata.
- Chromium checks cover responsive layout, display preferences, shared title, protocol selection, audited signal labels/durations, and connection-test software/serial/axis disclosure.
- The Haas XML fixtures are structurally representative, with identifying serial/program/count data replaced.
- No live CNC connection was made from this development environment. The supplied production readings verified the published XML shape; alternative macro candidates have not been verified on that machine.

The release-branch GitHub workflow repeats these checks, builds the container, runs the packaged app offline, and publishes the immutable versioned image. Anonymous image access must pass before main advances to the new store version. Docker is unavailable locally; the GitHub job is the container validation authority.

Earlier 0.1.1 run 38398925505 passed all 76 Python tests, 8 frontend tests, browser checks and Docker build. The 0.1.1 regression tests reproduced and fixed the missing collector mode crash. Version 0.1.2 preserves those checks and adds the sensor audit regressions.

See [sensor audit](sensor-audit.md) for every published observation, units, naming decisions and feed/rotary limitations.
