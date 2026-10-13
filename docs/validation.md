# Development validation — 0.1.0

Local checks and GitHub Actions validation completed on 2026-10-13:

- 74 Python tests: protocol fixtures, fixed MDC allowlist, GET-only MTConnect, persistent TCP reuse, e-stop/alarm separation, actual versus programmed feed, unavailable readings, multiple spindle observations, invalid XML, endpoint test tokens, shared title, shifts/overnight accounting, bounded retention and local MQTT broker restart/outage behavior.
- 8 frontend preference/sorting tests.
- Chromium checks: HA appearance, local overrides, persistence, all four sorts, shared title, responsive layout, and switching MTConnect/MDC ports and device-name input.
- No live Haas machine connection was made. Software fixtures are not hardware acceptance.

GitHub Actions run 38397609740 passed the complete suite, Docker build and offline read-allowlist check for source commit 6caff1825fbbc6d679d347c076e395088233df9d. Docker is not available in the local development environment; the CI-built container has not been published to a registry. No registry image is referenced in the initial add-on configuration. The project is a development candidate, not a production-certified release.
