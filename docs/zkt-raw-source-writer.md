# Raw source rows in the 2.7.0 writer

The exact `zone_lite` 2.7.0 image requires raw custody for history baseline,
tail and source-probe rows. When its journal runtime is ready, the existing
bounded source encoder emits `RAW_PRESERVED`, the original bytes and digest,
source ordinal, terminal record key and occurrence index. Its canonical chain
contains a null event UID. No roster lookup, timestamp interpretation, invalid
source classification or attendance event is performed while encoding that row.

Requiring the new contract and being ready to write it are separate checks.
A writer with missing, stale or disabled runtime evidence returns failure;
it cannot fall back to a legacy interpreted event. Failed allocation leaves
both the source array and canonical array unchanged. Unsupported record sizes
and overflowing occurrence ordinals are rejected before reading the record.
History remains streamed directly to ADD and uses its committed range replies;
this encoder does not place historical dumps in the ESP's live journal reserve.

The existing images and the 2.6.16 bridge retain their current source encoder.
This is not sufficient bridge rollback behavior: the persisted delivery
cutover, compatible new-format capture after rollback and legacy migration
remain release prerequisites. Version strings, writer activation flags,
signing, device registration and campaigns are not changed here.

ADD must have the opt-in raw source receiver and processing obligations before
this firmware path is enabled. Model qualification, derived interpretation,
identity evidence, live/history matching and Oracle delivery remain separate
from source custody. The shared session scheduler and initial roster refresh
are not refactored by this encoder component.

The production encoder is exercised against pinned cJSON with ASan/UBSan:
all supported raw sizes, zero and invalid bytes, missing roster input, runtime
refusal, exact byte forwarding and failure at every allocation. The actual
runtime adapter tests exact image selection, stale evidence and disabled writer
builds. Legacy source classifications retain their existing regression cases.
