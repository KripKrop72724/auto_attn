# Hikvision profile command pilot — 2026-09-18

Scope: DS-K1T342EFWX, terminal V3.3.5 build 20220310, profile
`ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1`. This is an explicitly approved
single-terminal profile pilot, not full model/release certification.

## Hardware evidence

Protected local qualification journals contain the exact terminal binding and
before/after profiles. They are excluded from Git and diagnostic exports.

- Disposable regular profile creation, name modification and removal read back
  successfully; unrelated profile fields were preserved.
- A separate disposable profile changed `localUIRight` false → true → false.
  Complete readbacks differed only in that field; the profile was then removed.
- A consenting tester enrolled one face on another disposable profile and made
  two successful punches. Targeted deletion completed, the profile disappeared,
  and both historical records remained byte-for-byte unchanged in the returned
  source representation. The tester then confirmed face authentication was
  rejected/unrecognized.
- Fingerprint/card removal and profiles with more than one face are not qualified.
  Firmware rejects their deletion. Permanent administrators must be demoted first.

## Implementation guarantees

Signed Hikvision firmware advertises profile command protocol version 1. ADD
requires an audited, password-confirmed policy approval for the bound terminal,
exact profile, confirmed identity and complete stable user snapshot before enabling
`PROFILE_PILOT` controls. Heartbeats cannot self-approve writes. Temporary admin
leases remain disabled. A separate signed ZKT image is unaffected.

Employee numbers remain strings. Updates compare the complete original profile
fingerprint and read back all profile fields after changing only name/localUIRight.
Retries verify the desired state before issuing another write. Deletion targets
one employee, requires verified absence and preserves ADD identity tombstones.
ADD validates the terminal/employee-bound readback receipt before marking any
profile command successful. Hikvision deletion does not reuse the ZKT assumption
that a global attendance count must be unchanged while concurrent punches arrive.

The deletion confirmation explains face removal, retained attendance and remaining
fingerprint/card restrictions. Device Users shows the already-confirmed terminal
identity instead of suggesting another serial confirmation will enable commands.

## Remaining release evidence

Host regression tests and an ESP-IDF build support the implementation; they do
not replace end-to-end ADD → signed ESP → terminal command checks. Record those
checks after deployment. Full retained-history reconciliation, fault testing and
the 72-hour soak remain separate release gates. Keep general qualification
`NOT_QUALIFIED` and factory artifacts `HIL_ONLY` until those gates pass.

## Create readback normalization

The first signed ESP command created its disposable profile but correctly withheld
success because the terminal canonicalized disabled validity dates to
`1970-01-01T00:00:00`. Firmware 3.0.7 accepts this one observed normalization only
when validity remains disabled and all other requested fields match. Arbitrary
date changes, enabled validity, changed rights and changed timezone are rejected.
The regression harness now returns the hardware-observed normalized response.
