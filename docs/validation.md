# Development validation — 0.1.0

Local checks completed on 2026-10-13:

- 74 Python tests: protocol fixtures, fixed MDC allowlist, GET-only MTConnect, persistent TCP reuse, e-stop/alarm separation, actual versus programmed feed, unavailable readings, multiple spindle observations, invalid XML, endpoint test tokens, shared title, shifts/overnight accounting, bounded retention and local MQTT broker restart/outage behavior.
- 8 frontend preference/sorting tests.
- Chromium checks: HA appearance, local overrides, persistence, all four sorts, shared title, responsive layout, and switching MTConnect/MDC ports and device-name input.
- No live Haas machine connection was made. Software fixtures are not hardware acceptance.

The GitHub validation workflow runs the suite and builds the container. Docker is not available in the current development environment; the container has not yet been built or published. No registry image is referenced in the initial add-on configuration. The project is a development candidate, not a production-certified release.
