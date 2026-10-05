#include "zkt_source_boundary_client.h"
#include "zkt_storage_owner.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdatomic.h>

#define BOUNDARY_WAIT_US 5000000ULL
static atomic_flag caller_busy = ATOMIC_FLAG_INIT;
static uint64_t pending_ticket;

bool zsb_runtime_required(void)
{
    const esp_app_desc_t *app = esp_app_get_description();
    return app && !strcmp(app->project_name, "zone_lite") && !strcmp(app->version, ZJ_WRITER_VERSION);
}

static zj_result_t collect(uint64_t deadline, zsb_record_t *out)
{
    do {
        zj_reply_t reply;
        bool complete = false;
        if (zj_owner_poll(pending_ticket, &reply, &complete) && complete) {
            pending_ticket = 0;
            if (reply.result == ZJ_OK) *out = reply.source_boundary;
            return reply.result;
        }
        if ((uint64_t)esp_timer_get_time() >= deadline) break;
        vTaskDelay(pdMS_TO_TICKS(20));
    } while ((uint64_t)esp_timer_get_time() < deadline);
    /* The owner may already be committing. Retain its one slot and observe
     * that result before admitting any later terminal snapshot. */
    return ZJ_UNCERTAIN;
}

static zj_result_t access_boundary(const zsb_facts_t *facts, zsb_record_t *out)
{
    if (!out) return ZJ_INVALID;
    memset(out, 0, sizeof(*out));
    if (!zsb_runtime_required() || (facts && !zsb_facts_valid(facts))) return ZJ_INVALID;
    if (atomic_flag_test_and_set_explicit(&caller_busy, memory_order_acquire)) return ZJ_IO;
    zj_result_t result = ZJ_UNCERTAIN;
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - BOUNDARY_WAIT_US) goto done;
    uint64_t deadline = now + BOUNDARY_WAIT_US;
    if (pending_ticket) {
        result = collect(deadline, out);
        if (result != ZJ_EMPTY || !facts) goto done;
    }
    if ((uint64_t)esp_timer_get_time() >= deadline) { result = ZJ_STALE; goto done; }
    zj_request_t request = {.operation = ZJ_SOURCE_BOUNDARY,
        .input.source_boundary = {.deadline_us = deadline, .create = facts != NULL}};
    if (facts) request.input.source_boundary.facts = *facts;
    if (!zj_owner_submit(&request, &pending_ticket)) { pending_ticket = 0; result = ZJ_IO; goto done; }
    result = collect(deadline, out);
done:
    atomic_flag_clear_explicit(&caller_busy, memory_order_release);
    return result;
}

zj_result_t zsb_runtime_read(zsb_record_t *out) { return access_boundary(NULL, out); }
zj_result_t zsb_runtime_create(const zsb_facts_t *facts, zsb_record_t *out)
{
    if (facts) return access_boundary(facts, out);
    if (out) memset(out, 0, sizeof(*out));
    return ZJ_INVALID;
}
