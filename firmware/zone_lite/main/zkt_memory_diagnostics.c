#include "zkt_memory_diagnostics.h"

#if !defined(ZONE_LITE_HIKVISION) || !ZONE_LITE_HIKVISION
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#if defined(__XTENSA__)
#include "xtensa/config/core-isa.h"
#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !CONFIG_IDF_TARGET_ESP32S3 || !XCHAL_HAVE_S32C1I
#error "Native diagnostic atomics require the qualified ESP32-S3 internal-RAM CAS"
#endif
#endif

/* A failure callback must remain usable when allocation itself has failed.
 * Never call the timer, logger, heap, scheduler, or string functions here.
 * Counter updates and slot claims each have a fixed attempt bound, including
 * under concurrent failures on both cores. No fetch-add retry loop is used. */
#define ZMD_SLOTS 4U
#define ZMD_COUNTER_ATTEMPTS 4U
#define ZMD_FAILURE_INTERVAL_US UINT64_C(60000000)
_Static_assert(sizeof(unsigned) == sizeof(uint32_t), "32-bit diagnostic counters");
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "diagnostics require lock-free atomics");

typedef struct {
    uint32_t sequence, bytes, caps, size_clamped, sequence_exact;
} failure_t;
typedef struct {
    atomic_uint state; /* 0 free, 1 callback owns, 2 ready, 3 reporter owns */
    failure_t value;
} failure_slot_t;
static DRAM_ATTR failure_slot_t slots[ZMD_SLOTS];
/* Explicit modulo-2^32 counters: wrapping does not imply incidents cleared. */
static DRAM_ATTR atomic_uint failures_mod32, dropped_mod32;
static DRAM_ATTR atomic_uint count_incomplete;
static DRAM_ATTR atomic_uint init_state, reporting;
static int hook_result;
/* Owned exclusively by the nonblocking reporter gate. */
static uint32_t stages_reported;
static bool failure_reported;
static uint64_t last_failure_us;

/* Only called with the explicit internal DRAM counters above. Native C11 CAS
 * is enabled for this translation unit only; PSRAM must never be passed here.
 * Contention can make counts incomplete, but cannot make this callback spin. */
static bool IRAM_ATTR increment_bounded(atomic_uint *counter, unsigned *previous)
{
    *previous = atomic_load_explicit(counter, memory_order_relaxed);
    for (unsigned attempt = 0; attempt < ZMD_COUNTER_ATTEMPTS; ++attempt) {
        if (atomic_compare_exchange_strong_explicit(counter, previous, *previous + 1U,
                memory_order_relaxed, memory_order_relaxed)) return true;
    }
    atomic_store_explicit(&count_incomplete, 1U, memory_order_relaxed);
    return false;
}

static void IRAM_ATTR allocation_failed(size_t size, uint32_t caps, const char *function_name)
{
    (void)function_name; /* Never copy arbitrary strings into console evidence. */
    unsigned sequence;
    bool sequence_exact = increment_bounded(&failures_mod32, &sequence);
    for (unsigned index = 0; index < ZMD_SLOTS; ++index) {
        unsigned expected = 0;
        if (!atomic_compare_exchange_strong_explicit(&slots[index].state, &expected, 1U,
                memory_order_acquire, memory_order_relaxed)) continue;
        slots[index].value = (failure_t){
            .sequence = sequence,
            .bytes = size > UINT32_MAX ? UINT32_MAX : (uint32_t)size,
            .caps = caps,
            .size_clamped = size > UINT32_MAX,
            .sequence_exact = sequence_exact,
        };
        atomic_store_explicit(&slots[index].state, 2U, memory_order_release);
        return;
    }
    unsigned previous;
    (void)increment_bounded(&dropped_mod32, &previous);
}

void zkt_memory_diag_init(void)
{
    unsigned expected = 0;
    if (!atomic_compare_exchange_strong_explicit(&init_state, &expected, 1U,
            memory_order_acquire, memory_order_relaxed)) return;
    /* IDF has a setter but no callback getter. This is the sole registration
     * in the firmware; a repository regression guards that ownership. Never
     * re-register from a reconnect or replace this hook during recovery. */
    hook_result = heap_caps_register_failed_alloc_callback(allocation_failed);
    atomic_store_explicit(&init_state, 2U, memory_order_release);
}

void zkt_memory_diag_report(zmd_stage_t stage, int error)
{
    if ((unsigned)stage >= ZMD_STAGE_COUNT ||
        atomic_load_explicit(&init_state, memory_order_acquire) != 2U) return;
    unsigned expected = 0;
    if (!atomic_compare_exchange_strong_explicit(&reporting, &expected, 1U,
            memory_order_acquire, memory_order_relaxed)) return;

    bool transport_failure = stage >= ZMD_ADD_TRANSPORT_FAILURE;
    if (transport_failure) {
        int64_t now = esp_timer_get_time();
        if (now < 0 || (failure_reported && ((uint64_t)now < last_failure_us ||
            (uint64_t)now - last_failure_us < ZMD_FAILURE_INTERVAL_US))) goto done;
        last_failure_us = (uint64_t)now;
        failure_reported = true;
    } else {
        uint32_t bit = UINT32_C(1) << (unsigned)stage;
        if (stages_reported & bit) goto done;
        stages_reported |= bit;
    }

    failure_t observed[ZMD_SLOTS];
    unsigned count = 0;
    for (unsigned index = 0; index < ZMD_SLOTS; ++index) {
        unsigned ready = 2U;
        if (!atomic_compare_exchange_strong_explicit(&slots[index].state, &ready, 3U,
                memory_order_acquire, memory_order_relaxed)) continue;
        observed[count++] = slots[index].value;
        atomic_store_explicit(&slots[index].state, 0U, memory_order_release);
    }
    /* These are independently sampled capability pools, not an atomic heap
     * snapshot or a sum of disjoint memory regions. Never query inside hook. */
    uint32_t dma_free = heap_caps_get_free_size(MALLOC_CAP_DMA);
    uint32_t dma_largest = heap_caps_get_largest_free_block(MALLOC_CAP_DMA);
    uint32_t internal_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint32_t internal_largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint32_t psram_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uint32_t psram_largest = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    ESP_LOGI("zkt_mem", "ZMD1 stage=%u err=%d hook=%d fail_mod32=%u drop_mod32=%u samples=%u dma_free=%u dma_largest=%u int8_free=%u int8_largest=%u psram8_free=%u psram8_largest=%u count_incomplete=%u",
        (unsigned)stage, error, hook_result,
        atomic_load_explicit(&failures_mod32, memory_order_relaxed),
        atomic_load_explicit(&dropped_mod32, memory_order_relaxed), count,
        (unsigned)dma_free, (unsigned)dma_largest, (unsigned)internal_free,
        (unsigned)internal_largest, (unsigned)psram_free, (unsigned)psram_largest,
        atomic_load_explicit(&count_incomplete, memory_order_relaxed));
    for (unsigned index = 0; index < count; ++index) {
        ESP_LOGI("zkt_mem", "ZMD1 sample_seq=%u bytes=%u caps=%u size_clamped=%u sequence_exact=%u",
            (unsigned)observed[index].sequence, (unsigned)observed[index].bytes,
            (unsigned)observed[index].caps, (unsigned)observed[index].size_clamped,
            (unsigned)observed[index].sequence_exact);
    }
done:
    atomic_store_explicit(&reporting, 0U, memory_order_release);
}
#endif
