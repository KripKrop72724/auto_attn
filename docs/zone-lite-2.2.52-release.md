# Zone Lite 2.2.52 release

Zone Lite 2.2.52 stabilizes fail-closed authoritative attendance retries observed during the 2.2.51 nationwide rollout.

- A complete terminal dump with unresolved identities now advances only the ESP's observed terminal counter baseline. Oracle replacement remains blocked, every unresolved punch remains preserved, and an identical heavy dump is not immediately retriggered as a false counter mismatch.
- A transport failure gets one bounded fresh-session retry. If that retry also fails, authoritative truth cools down for 30 minutes while live capture continues and the unchanged ZKT remains the source of truth.
- Applying a new verified identity catalog clears the cooldown and permits an immediate authoritative retry.
- OTA restart safepoints, Wi-Fi setup portal behavior, identity fail-closed enforcement, live event durability, rollback protection, and all terminal mutation controls remain unchanged from 2.2.51.

Production evidence motivating the patch:

- Karachi-01 repeated a forced dump with `device_delta=0` until the ESP stopped heartbeating during the third read attempt.
- Peshawar-06 repeated the same complete identity-incomplete dump because the observed attendance counter was not advanced after the fail-closed decision.

Acceptance requires the existing build, signature, HIL, canary, exact-image, terminal-identity, attendance-count, and post-boot stability gates.
