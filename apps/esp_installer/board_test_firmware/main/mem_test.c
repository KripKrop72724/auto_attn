/*
 * PSRAM and internal RAM pattern tests.
 *
 * Every word of the tested block is written and read back (IDF's own boot
 * test touches one word in eight with one pattern, and missed a faulty
 * board). Tests, in order (test= names in ERR lines):
 *   zeros  ones  x55  xaa  addr (address in address)  naddr (inverse address)
 *   lfsr1  lfsr2 (pseudo-random, two seeds)  bytes (byte writes, word reads)
 *   dual (both cores at once: each core writes one half, then each core
 *         checks the half the other core wrote)
 * PSRAM: 2 rounds over the largest free PSRAM block (must be >= 7.5 MiB).
 * Internal RAM: 1 round over the largest free internal block.
 */
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "board_test.h"
#include "esp_cache.h"
#include "esp_heap_caps.h"
#include "esp_psram.h"

#define MIB (1024u * 1024u)
#define PSRAM_EXPECTED (8u * MIB)
#define PSRAM_MIN_BLOCK (7u * MIB + MIB / 2u)
#define CHUNK_WORDS 4096u /* 16 KiB between progress ticks */
#define ALIGN 64u         /* cache line */
#define IRAM_SPARE (24u * 1024u)

enum kind { K_ZEROS, K_ONES, K_X55, K_XAA, K_ADDR, K_NADDR, K_LFSR1, K_LFSR2, K_BYTES, K_DUAL };
#define TESTS_PER_ROUND 10

static const char *const NAMES[TESTS_PER_ROUND] = {
    "zeros", "ones", "x55", "xaa", "addr", "naddr", "lfsr1", "lfsr2", "bytes", "dual",
};

#define SEED1 0x2545F491u
#define SEED2 0x9E3779B9u
#define SEED_DUAL0 0x1B873593u
#define SEED_DUAL1 0xCC9E2D51u

static inline uint32_t lfsr_next(uint32_t s)
{
    /* 32-bit Galois LFSR, taps 32,31,29,1 (maximal length). */
    return (s >> 1) ^ (-(s & 1u) & 0xD0000001u);
}

static inline uint8_t byte_at(uint32_t i)
{
    return (uint8_t)(i ^ (i >> 8) ^ (i >> 16) ^ 0xA5u);
}

typedef struct {
    volatile uint32_t *base;
    uint32_t words;
    bool external; /* PSRAM: sync the cache before reading back */
} region_t;

#define SYNC_CHUNK (256u * 1024u)

static void sync_cache(const region_t *r)
{
    if (!r->external) {
        return;
    }
    /* Write back and drop cached lines so the check reads the chip. In
     * chunks: each call runs in a critical section. */
    uintptr_t addr = (uintptr_t)r->base;
    uintptr_t end = addr + r->words * 4u;
    while (addr < end) {
        size_t n = end - addr < SYNC_CHUNK ? end - addr : SYNC_CHUNK;
        (void)esp_cache_msync((void *)addr, n,
                              ESP_CACHE_MSYNC_FLAG_DIR_C2M | ESP_CACHE_MSYNC_FLAG_INVALIDATE);
        addr += n;
        sl_tick((uint32_t)addr);
        sl_yield();
    }
}

/* The largest free block, or a little less if the exact size is refused. */
static void *alloc_largest(uint32_t caps, size_t spare, size_t *size_out)
{
    size_t largest = heap_caps_get_largest_free_block(caps);
    size_t want = largest > spare ? largest - spare : 0;
    for (int i = 0; i < 8 && want > 0; i++) {
        void *block = heap_caps_malloc(want, caps);
        if (block) {
            *size_out = want;
            return block;
        }
        want = want > 4096u ? want - 4096u : 0;
    }
    *size_out = largest;
    return NULL;
}

static inline uint32_t fixed_value(enum kind k, volatile uint32_t *p)
{
    switch (k) {
    case K_ZEROS: return 0x00000000u;
    case K_ONES: return 0xFFFFFFFFu;
    case K_X55: return 0x55555555u;
    case K_XAA: return 0xAAAAAAAAu;
    case K_ADDR: return (uint32_t)(uintptr_t)p;
    default: return ~(uint32_t)(uintptr_t)p;
    }
}

static void write_words(const region_t *r, enum kind k, uint32_t seed)
{
    volatile uint32_t *p = r->base;
    uint32_t s = seed;
    for (uint32_t done = 0; done < r->words; done += CHUNK_WORDS) {
        uint32_t n = r->words - done < CHUNK_WORDS ? r->words - done : CHUNK_WORDS;
        if (k == K_LFSR1 || k == K_LFSR2 || k == K_DUAL) {
            for (uint32_t i = 0; i < n; i++) {
                p[i] = s;
                s = lfsr_next(s);
            }
        } else if (k == K_BYTES) {
            volatile uint8_t *b = (volatile uint8_t *)p;
            uint32_t first = done * 4u;
            for (uint32_t i = 0; i < n * 4u; i++) {
                b[i] = byte_at(first + i);
            }
        } else {
            for (uint32_t i = 0; i < n; i++) {
                p[i] = fixed_value(k, &p[i]);
            }
        }
        p += n;
        sl_tick((uint32_t)(uintptr_t)p);
        sl_yield();
    }
}

static void check_words(const region_t *r, enum kind k, uint32_t seed, const char *test)
{
    volatile uint32_t *p = r->base;
    uint32_t s = seed;
    for (uint32_t done = 0; done < r->words; done += CHUNK_WORDS) {
        uint32_t n = r->words - done < CHUNK_WORDS ? r->words - done : CHUNK_WORDS;
        for (uint32_t i = 0; i < n; i++) {
            uint32_t want;
            if (k == K_LFSR1 || k == K_LFSR2 || k == K_DUAL) {
                want = s;
                s = lfsr_next(s);
            } else if (k == K_BYTES) {
                uint32_t a = (done + i) * 4u;
                want = (uint32_t)byte_at(a) | ((uint32_t)byte_at(a + 1) << 8) |
                       ((uint32_t)byte_at(a + 2) << 16) | ((uint32_t)byte_at(a + 3) << 24);
            } else {
                want = fixed_value(k, &p[i]);
            }
            uint32_t got = p[i];
            if (got != want) {
                sl_err(test, (uint32_t)(uintptr_t)&p[i], want, got);
            }
        }
        p += n;
        sl_tick((uint32_t)(uintptr_t)p);
        sl_yield();
    }
}

/* ---- dual-core test: jobs run by the two pinned workers ---- */

typedef struct {
    region_t half;
    uint32_t seed;
} half_job_t;

static void job_write(void *arg)
{
    half_job_t *j = arg;
    write_words(&j->half, K_DUAL, j->seed);
}

static void job_check(void *arg)
{
    half_job_t *j = arg;
    check_words(&j->half, K_DUAL, j->seed, "dual");
}

static void run_dual(const region_t *r)
{
    uint32_t half_words = (r->words / 2u) & ~(ALIGN / 4u - 1u);
    half_job_t a = {{r->base, half_words, r->external}, SEED_DUAL0};
    half_job_t b = {{r->base + half_words, r->words - half_words, r->external}, SEED_DUAL1};
    /* Core 0 writes the first half while core 1 writes the second ... */
    sl_run_on_both(job_write, &a, job_write, &b);
    sync_cache(r);
    /* ... then each core checks the half the other core wrote. */
    sl_run_on_both(job_check, &b, job_check, &a);
}

/* One full set of tests; returns the bytes moved (written + read). */
static uint64_t run_round(const region_t *r, int round, int rounds)
{
    for (int t = 0; t < TESTS_PER_ROUND; t++) {
        sl_step(round * TESTS_PER_ROUND + t + 1);
        enum kind k = (enum kind)t;
        if (k == K_DUAL) {
            run_dual(r);
            continue;
        }
        uint32_t seed = k == K_LFSR1 ? SEED1 : SEED2;
        write_words(r, k, seed);
        sync_cache(r);
        check_words(r, k, seed, NAMES[t]);
    }
    (void)rounds;
    return (uint64_t)TESTS_PER_ROUND * r->words * 4u * 2u;
}

/* Allocate `bytes` and test the cache-line aligned part of it. */
static bool make_region(region_t *r, void *block, size_t bytes, bool external)
{
    uintptr_t start = ((uintptr_t)block + ALIGN - 1u) & ~(uintptr_t)(ALIGN - 1u);
    uintptr_t end = ((uintptr_t)block + bytes) & ~(uintptr_t)(ALIGN - 1u);
    if (end <= start) {
        return false;
    }
    r->base = (volatile uint32_t *)start;
    r->words = (uint32_t)((end - start) / 4u);
    r->external = external;
    return true;
}

static uint32_t mib_per_s(uint64_t bytes, int64_t us)
{
    if (us <= 0) {
        return 0;
    }
    return (uint32_t)((bytes * 1000000ull) / ((uint64_t)us * MIB));
}

bool sl_test_psram(void)
{
    const int rounds = 2;
    int64_t t0 = sl_now_us();
    sl_stage("psram", rounds * TESTS_PER_ROUND);
    sl_errors_reset();
    if (!esp_psram_is_initialized()) {
        sl_line("RESULT stage=psram result=fail code=PSRAM_NOT_FOUND detail=not_initialised "
                "errors=0 ms=%ld size=0",
                (long)((sl_now_us() - t0) / 1000));
        return false;
    }
    size_t size = esp_psram_get_size();
    if (size != PSRAM_EXPECTED) {
        sl_line("RESULT stage=psram result=fail code=PSRAM_WRONG_SIZE detail=%u errors=0 ms=%ld "
                "size=%u",
                (unsigned)size, (long)((sl_now_us() - t0) / 1000), (unsigned)size);
        return false;
    }
    size_t largest = 0;
    void *block = alloc_largest(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT, 0, &largest);
    region_t r;
    if (block == NULL || largest < PSRAM_MIN_BLOCK || !make_region(&r, block, largest, true)) {
        free(block);
        sl_line("RESULT stage=psram result=fail code=PSRAM_TOO_SMALL detail=%u errors=0 ms=%ld "
                "size=%u tested=0",
                (unsigned)largest, (long)((sl_now_us() - t0) / 1000), (unsigned)size);
        return false;
    }
    int64_t start = sl_now_us();
    uint64_t moved = 0;
    for (int round = 0; round < rounds; round++) {
        moved += run_round(&r, round, rounds);
    }
    int64_t took = sl_now_us() - start;
    free(block);
    uint32_t errors = sl_errors();
    long ms = (long)((sl_now_us() - t0) / 1000);
    if (errors) {
        sl_line("RESULT stage=psram result=fail code=PSRAM_DATA_ERRORS detail=%s errors=%lu ms=%ld "
                "size=%u tested=%lu rounds=%d",
                sl_first_failed_test(), (unsigned long)errors, ms, (unsigned)size,
                (unsigned long)(r.words * 4u), rounds);
        return false;
    }
    sl_line("RESULT stage=psram result=ok code=OK detail=all_patterns_ok errors=0 ms=%ld size=%u "
            "tested=%lu rounds=%d mb_s=%lu",
            ms, (unsigned)size, (unsigned long)(r.words * 4u), rounds,
            (unsigned long)mib_per_s(moved, took));
    return true;
}

bool sl_test_iram(void)
{
    int64_t t0 = sl_now_us();
    sl_stage("iram", TESTS_PER_ROUND);
    sl_errors_reset();
    const uint32_t caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
    /* Leave a little of the block for anything the system allocates meanwhile. */
    size_t want = 0;
    void *block = alloc_largest(caps, IRAM_SPARE, &want);
    region_t r;
    if (block == NULL || !make_region(&r, block, want, false)) {
        free(block);
        sl_line("RESULT stage=iram result=fail code=IRAM_DATA_ERRORS detail=no_block errors=0 "
                "ms=%ld tested=0",
                (long)((sl_now_us() - t0) / 1000));
        return false;
    }
    int64_t start = sl_now_us();
    uint64_t moved = run_round(&r, 0, 1);
    int64_t took = sl_now_us() - start;
    free(block);
    uint32_t errors = sl_errors();
    long ms = (long)((sl_now_us() - t0) / 1000);
    if (errors) {
        sl_line("RESULT stage=iram result=fail code=IRAM_DATA_ERRORS detail=%s errors=%lu ms=%ld "
                "tested=%lu",
                sl_first_failed_test(), (unsigned long)errors, ms, (unsigned long)(r.words * 4u));
        return false;
    }
    sl_line("RESULT stage=iram result=ok code=OK detail=all_patterns_ok errors=0 ms=%ld tested=%lu "
            "mb_s=%lu",
            ms, (unsigned long)(r.words * 4u), (unsigned long)mib_per_s(moved, took));
    return true;
}
