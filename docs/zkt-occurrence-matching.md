# ZKT live/source occurrence matching

`zk_add.zkt_occurrence_match` proposes one-to-one links between decoded live
observations and a complete, bounded range of canonical source occurrences.
It does not create attendance, resolve employee identity, write a link, or
certify Oracle delivery. Its integration with qualified model evidence and
transactional attendance creation remains a release prerequisite.

A source identity includes the terminal serial, source epoch UUID, ordinal and
raw digest. A live identity includes its immutable custody work key, record
offset and raw digest. Equal bytes at two source ordinals stay distinct, as do
two equal records at different offsets in one live packet. Receipt replay
keeps the original work key. Neither historical attendance UIDs nor current
enrollment UIDs are used to join the two formats.

The caller must establish the exact terminal and source epoch, qualified
decoder version and layouts, a complete source interval and a complete live
observation window. Completeness means evidence coverage, not a timeout or a
quiet socket. The source interval includes every ordinal, including undecoded
exceptions. Missing qualification, incomplete windows or unresolved source
facts hold new associations. The model selector alone grants no qualification.

Within a proved window, matching compares the exact textual user reference,
original encoded terminal time, status and punch code. It preserves leading
zeros. A new link is proposed only when one remaining source occurrence and
one remaining live observation have those facts. Two identical same-second
records on each side are still ambiguous; arrival order and equal counts do
not prove their correspondence. Previously proved links consume one occurrence
each and cannot be overwritten or reused. A retained link is reported as
`EXISTING`, not re-certified when qualification has been withdrawn.

Each input is bounded to 2,048 records. Fact buckets keep processing and memory
linear in the input size; repeated punches do not allocate a matrix of every
possible pairing. Every live input gets one disposition. Source occurrences
that cannot be associated remain explicitly unbound and can independently
retain their own canonical attendance/identity/delivery obligations.

Integration must load and lock the relevant evidence revision, independently
verify the profile and both windows, and commit unique links in that same
transaction. Revalidate after any raw, source-epoch, profile, cursor, receipt or
binding revision change. Do not use this pure planner's boolean inputs as an
operator approval API or accept them from a device. A proposal is never a
release or delivery certificate.

Synthetic verification covers different source/live layouts, repeated punches,
partial windows, prior-link collisions, changed terminal/epoch/profile,
clock inconsistency, unchanged replay identity, order independence, leading
zeros, packet offsets and saturated ambiguity buckets. These checks do not
qualify any of the six installed terminal profiles.
