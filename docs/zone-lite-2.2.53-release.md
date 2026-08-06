# Zone Lite 2.2.53 release

Zone Lite 2.2.53 bounds historical attendance reconstruction observed during
the 2.2.52 Tower-3FL acceptance gate. It does not change identity validation,
live punch capture, Oracle fail-closed behavior, Wi-Fi setup, or OTA security.

## Production correction

- Retains one count-verified, append-only ZKT attendance snapshot in PSRAM for
  the active historical sweep instead of rereading the same multi-megabyte
  terminal table for every month.
- Reuses that snapshot only while its record prefix remains valid and for at
  most eight hours; count regression, current-truth work, sweep completion,
  or a new sweep invalidates it.
- Restricts historical reconstruction to 22:00-05:00 Pakistan time
  (17:00-00:00 UTC). Current authoritative truth and live capture remain
  available at every hour.
- Measures the next reconciliation interval from completion. A slow terminal
  read can therefore no longer make another historical cycle immediately due.
- Preserves all existing fail-closed identity and Oracle membership checks.

## Acceptance gates

- Focused and full Python contract suites pass.
- Ruff passes.
- ESP-IDF production build succeeds with the generic signed OTA image.
- Repository, identity-catalog, and Oracle load preflights pass.
- HIL, SWAT canary, Tower-3FL recovery acceptance, and subsequent sequential
  zone rollout remain mandatory; no multi-zone campaign is permitted.
