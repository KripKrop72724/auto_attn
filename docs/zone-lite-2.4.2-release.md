# Zone Lite 2.4.2 release

Zone Lite 2.4.2 completes the source-capture hardening validated on the Karachi
uFace800 canary.

- After ADD has durably acknowledged every source row through the anchored
  cutoff, the final manifest uses a fresh terminal count and the already
  committed append-only chain. It no longer reopens the terminal's prepared
  attendance buffer solely to seal the certificate.
- A terminal-count regression still stops fail closed. The manifest cannot
  advance or replace the ADD cursor, raw evidence, per-chunk digests, or final
  chain digest.
- Existing 2.4.1 ACK serialization and assignment-priority safeguards remain in
  place. Karachi resumes at its durable ordinal 5,043; it does not rescan or
  recreate the completed source evidence.

The exact signed 2.4.2 image remains HIL-only until the Karachi canary seals its
capture certificate, passes the physical fault matrix, and completes the
required 24-hour soak.
