/* Host tests for zkt_legacy_evidence.c: ZKT 2.7.7's OTA verdict on the
 * bridge-era journal leftovers (ZONE-SLICTOWER-3FL). Plain cc, ASan, UBSan. */
#include <stdio.h>
#include <string.h>

#include "durable_queue.h"
#include "zkt_legacy_evidence.h"

static unsigned checks;
#define EXPECT(cond)                                                                                                   \
    do {                                                                                                               \
        ++checks;                                                                                                      \
        if (!(cond)) {                                                                                                 \
            fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #cond);                                                 \
            return 1;                                                                                                  \
        }                                                                                                              \
    } while (0)

/* A root as zkt_journal_state.c builds it (zj_state_open, zj_state_enable_add). */
static void root(uint8_t out[ZLE_ROOT_BYTES], uint64_t limit, uint8_t authority)
{
    memset(out, 0, ZLE_ROOT_BYTES);
    memcpy(out, "ZJROOT01", 8);
    for (unsigned i = 0; i < 8; ++i) out[8 + i] = (uint8_t)(limit >> (8 * i));
    for (unsigned i = 16; i < 64; ++i) out[i] = (uint8_t)(i * 7 + 1);
    memcpy(out + 64, "CKL2234560123", 13);
    out[145] = authority;
    uint32_t crc = dq_crc32(out, ZLE_ROOT_BYTES - 4);
    for (unsigned i = 0; i < 4; ++i) out[ZLE_ROOT_BYTES - 4 + i] = (uint8_t)(crc >> (8 * i));
}

static void resign(uint8_t out[ZLE_ROOT_BYTES])
{
    uint32_t crc = dq_crc32(out, ZLE_ROOT_BYTES - 4);
    for (unsigned i = 0; i < 4; ++i) out[ZLE_ROOT_BYTES - 4 + i] = (uint8_t)(crc >> (8 * i));
}

/* 3FL as the 2.6.18-2.6.21 bridges should have left it: the namespace with
 * a legacy root and the bridge's reader proof, one empty segment. */
static zle_inventory_t threefl(void)
{
    zle_inventory_t in = {0};
    in.runtime_disabled = true;
    in.namespace_present = in.namespace_listed = true;
    in.root = ZLE_ROOT_LEGACY;
    zle_note_key(&in, "root");
    zle_note_key(&in, "reader_v1");
    in.store_listed = true;
    zle_note_file(&in, "zktj0000000000000001.j", 240, true, 240);
    return in;
}

static int expect_refused(zle_inventory_t in, const char *error, const char *reason)
{
    zle_verdict_t v = zle_decide(&in);
    EXPECT(v.state == ZLE_REFUSED);
    EXPECT(v.error && !strcmp(v.error, error));
    EXPECT(v.reason && !strcmp(v.reason, reason));
    return 0;
}

int main(void)
{
    uint8_t bytes[ZLE_ROOT_BYTES + 1];

    /* ---- the root blob ---- */
    root(bytes, 1, 0);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_LEGACY);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES - 1) == ZLE_ROOT_INVALID);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES + 1) == ZLE_ROOT_INVALID);
    EXPECT(zle_root_classify(NULL, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 257, 1);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_ADD);
    root(bytes, 1, 1);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_ADD);
    /* Legacy authority with a reserved sequence cannot exist: refuse it. */
    root(bytes, 257, 0);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 0, 0);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 2);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    bytes[0] = 'X';
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    bytes[150] = 1;
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    memset(bytes + 16, 0, 32); /* no key */
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    memset(bytes + 48, 0, 16); /* no epoch */
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    memset(bytes + 64, 0, 81); /* no serial */
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    bytes[64 + 20] = 'Z'; /* bytes after the serial's NUL */
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    memset(bytes + 64, 'A', 81); /* no NUL in 81 bytes */
    resign(bytes);
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    root(bytes, 1, 0);
    bytes[ZLE_ROOT_BYTES - 1] ^= 1; /* CRC */
    EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    /* Every single-bit flip of a legacy root is either refused or still legacy
     * (never ADD by accident, never accepted with a bad CRC). */
    for (unsigned bit = 0; bit < ZLE_ROOT_BYTES * 8; ++bit) {
        root(bytes, 1, 0);
        bytes[bit / 8] ^= (uint8_t)(1U << (bit % 8));
        EXPECT(zle_root_classify(bytes, ZLE_ROOT_BYTES) == ZLE_ROOT_INVALID);
    }

    /* ---- the verdict ---- */
    zle_inventory_t none = {.runtime_disabled = true, .store_listed = true};
    zle_verdict_t v = zle_decide(&none);
    EXPECT(v.state == ZLE_NONE && !v.error && !strcmp(v.reason, "NONE"));

    /* 3FL: accepted, nothing refused. */
    zle_inventory_t in = threefl();
    v = zle_decide(&in);
    EXPECT(v.state == ZLE_ACCEPTED && !v.error && !strcmp(v.reason, "BRIDGE_LEFTOVERS"));
    EXPECT(in.files == 1 && in.bytes == 240 && in.keys == 2 && in.reader_proof && !in.unknown_keys);
    /* Only the namespace (no files), with or without its root. */
    in = threefl();
    in.files = in.bytes = 0;
    EXPECT(zle_decide(&in).state == ZLE_ACCEPTED);
    in.root = ZLE_ROOT_ABSENT;
    in.keys = 1;
    EXPECT(zle_decide(&in).state == ZLE_ACCEPTED);
    /* No namespace, no root, files: the segments cannot be explained. */
    in = threefl();
    in.namespace_present = false;
    in.root = ZLE_ROOT_ABSENT;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILES_WITHOUT_ROOT")) return 1;
    in = threefl();
    in.root = ZLE_ROOT_ABSENT;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILES_WITHOUT_ROOT")) return 1;
    /* Several segments are fine within the limit. */
    in = threefl();
    for (unsigned i = 2; i <= ZLE_FILES_MAX; ++i) {
        char name[32];
        snprintf(name, sizeof(name), "zktj%016x.j", i);
        zle_note_file(&in, name, 65536, true, 65536);
    }
    EXPECT(zle_decide(&in).state == ZLE_ACCEPTED);
    zle_note_file(&in, "zktj00000000000000ff.j", 240, false, 0);
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_LIMIT")) return 1;

    /* Each condition alone refuses. */
    in = threefl();
    in.runtime_disabled = false;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "JOURNAL_RUNTIME_ACTIVE")) return 1;
    in = threefl();
    in.owner_started = true;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "JOURNAL_OWNER_STARTED")) return 1;
    in = threefl();
    in.root = ZLE_ROOT_ADD;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "ADD_AUTHORITY")) return 1;
    in = threefl();
    in.root = ZLE_ROOT_INVALID;
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "ROOT_INVALID")) return 1;
    const char *keys[] = {"retirement", "source_v1", "factory_v1", "", NULL};
    for (unsigned i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        in = threefl();
        zle_note_key(&in, keys[i]);
        if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "NAMESPACE_KEYS")) return 1;
    }
    /* A damaged segment, an oversized one, and names the repair would not
     * keep as evidence. */
    in = threefl();
    zle_note_file(&in, "zktj0000000000000002.j", 4096, false, 1024);
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_UNREADABLE")) return 1;
    in = threefl();
    zle_note_file(&in, "zktj0000000000000002.j", 4096, true, 1024);
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_UNREADABLE")) return 1;
    in = threefl();
    zle_note_file(&in, "zktj0000000000000002.j", ZLE_FILE_BYTES_MAX + 1, false, 0);
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_OVERSIZED")) return 1;
    const char *names[] = {"zktj0000000000000002.tmp", "zktj000000000000002.j", "zktjxyz", "zktj000000000000000g.j",
                           "zktj0000000000000002.jj", "zktj", NULL};
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        in = threefl();
        zle_note_file(&in, names[i], 10, true, 10);
        if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_NOT_SEGMENT")) return 1;
    }
    /* Unreadable facts keep the pre-2.7.7 refusals. */
    in = threefl();
    in.namespace_listed = false;
    if (expect_refused(in, "JOURNAL_READER_PROOF_UNAVAILABLE", "NVS_UNREADABLE")) return 1;
    in = threefl();
    in.root = ZLE_ROOT_UNREADABLE;
    if (expect_refused(in, "JOURNAL_READER_PROOF_UNAVAILABLE", "NVS_UNREADABLE")) return 1;
    in = threefl();
    in.store_listed = false;
    if (expect_refused(in, "JOURNAL_OTA_STORAGE_UNAVAILABLE", "STORE_UNREADABLE")) return 1;
    none.store_listed = false;
    if (expect_refused(none, "JOURNAL_OTA_STORAGE_UNAVAILABLE", "STORE_UNREADABLE")) return 1;
    EXPECT(zle_decide(NULL).state == ZLE_REFUSED);

    /* Review of 26 October, D6: the guard reads the files only when their
     * contents can change the verdict; when they cannot, the verdict refuses
     * whatever they hold (so skipping the reads never accepts). */
    zle_inventory_t facts = threefl();
    facts.files = facts.bytes = 0;
    EXPECT(zle_contents_matter(&facts, 1) && zle_contents_matter(&facts, ZLE_FILES_MAX));
    EXPECT(zle_contents_matter(&facts, 0) && !zle_contents_matter(&facts, ZLE_FILES_MAX + 1));
    EXPECT(!zle_contents_matter(NULL, 1));
    for (unsigned variant = 0; variant < 7; ++variant) {
        in = facts;
        unsigned files = 1;
        switch (variant) {
        case 0: in.runtime_disabled = false; break;
        case 1: in.owner_started = true; break;
        case 2: in.root = ZLE_ROOT_ADD; break;
        case 3: in.root = ZLE_ROOT_INVALID; break;
        case 4: zle_note_key(&in, "source_v1"); break;
        case 5: in.root = ZLE_ROOT_ABSENT; break;
        default: files = ZLE_FILES_MAX + 1; break;
        }
        EXPECT(!zle_contents_matter(&in, files));
        for (unsigned i = 0; i < files; ++i) zle_note_file(&in, "zktj0000000000000001.j", 240, true, 240);
        EXPECT(zle_decide(&in).state == ZLE_REFUSED);
    }
    /* Contents that matter are still read: a damaged segment refuses. */
    in = facts;
    EXPECT(zle_contents_matter(&in, 1));
    zle_note_file(&in, "zktj0000000000000001.j", 4096, false, 1024);
    if (expect_refused(in, "JOURNAL_LEGACY_EVIDENCE_PRESENT", "FILE_UNREADABLE")) return 1;
    /* The check reads the facts, not the files noted so far. */
    in = facts;
    zle_note_file(&in, "zktj0000000000000001.j", 4096, false, 1024);
    EXPECT(zle_contents_matter(&in, 1));

    /* Names. */
    EXPECT(!strcmp(zle_root_name(ZLE_ROOT_LEGACY), "LEGACY") && !strcmp(zle_root_name(ZLE_ROOT_ADD), "ADD"));
    EXPECT(!strcmp(zle_state_name(ZLE_ACCEPTED), "ACCEPTED") && !strcmp(zle_state_name(ZLE_REFUSED), "REFUSED"));
    printf("zkt legacy evidence host tests passed (%u checks)\n", checks);
    return 0;
}
