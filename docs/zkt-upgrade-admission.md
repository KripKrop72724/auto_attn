# Nationwide ZKT offer admission

PostgreSQL assignment transactions acquire one nonblocking ZKT advisory lock
before examining or reserving a deployment. It remains held through the caller's
assignment and download-grant commit. A competing request receives no assignment
and can retry; it cannot race a second transaction into an extra reservation.
Transaction rollback removes the tentative offer and releases the lock.

The first registered 2.6.16 or 2.7.0 release activates the nationwide constraints
for subsequent ZKT offers, including older releases competing for the same
connectors. New offers require an exact active, connected, non-spare member of
the approved 17-device inventory. At most two reservations may exist, and only
one may belong to a physical location. Physical locations use the approved
inventory, rather than assuming that different ADD zone IDs mean different
buildings. An active connector with an unknown location prevents admission.

An existing offer can resume without consuming a second slot. Pausing its
campaign or losing contact does not free its slot. Administrative cancellation,
supersession or revocation after an offer also remains an uncertain reservation:
those database labels do not prove that firmware stopped installing. An explicit
verified stop/recovery path is still required to resolve that uncertainty. No
operator should delete evidence or change deployment states merely to free a slot.

Hikvision offers retain their own family policy. Prior ZKT releases retain their
existing admission policy until a journal generation is registered. Once the
new limits apply, revoking the candidate does not silently disable them.

This component only restricts admission. It does not qualify or register either
new version, manufacture a signed image, advance a wave, certify an offline site,
or replace predecessor, binding, release, HIL and download checks. The storage
contract still rejects the unfinished bridge/writer. Trusted qualification and
wave enforcement must be completed before publication.

SQLite tests exercise the decision rules; PostgreSQL tests exercise actual
overlapping assignment transactions and rollback. They cover different zone IDs
at one location, exact binding, offline/spare devices, paused and uncertain
cancelled reservations, old-image competition, Hikvision isolation, resumable
offers and the unchanged refusal of an unqualified 2.7.0 candidate.
