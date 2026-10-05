#include "zkt_storage_owner_platform.h"
#include "zkt_storage_owner.h"
#include "zkt_source_boundary_client.h"
#include <assert.h>

static int64_t clock_us = 1000000;
static esp_app_desc_t app = {.project_name = "zone_lite", .version = "2.7.0"};
static uint64_t active_ticket;
static unsigned submissions;
static bool occupied, stall, finished, refuse, nested;
static zj_request_t accepted;
static zj_reply_t response;
static uint8_t durable[ZSB_BYTES];
static bool present;
static zj_reader_identity_t identity = {.capture_epoch={1}, .image_digest={2}, .terminal_digest={3}};

const esp_app_desc_t *esp_app_get_description(void) { return &app; }
int64_t esp_timer_get_time(void) { return clock_us; }
void vTaskDelay(unsigned ms) { clock_us += (int64_t)ms * 1000; }
static int read_blob(void *unused, const char *key, uint8_t *out, size_t length)
{
    (void)unused; assert(!strcmp(key, "source_v1") && length == ZSB_BYTES);
    if (!present) return 0;
    memcpy(out, durable, length); return 1;
}
static bool write_blob(void *unused, const char *key, const uint8_t *in, size_t length)
{
    (void)unused; assert(!strcmp(key, "source_v1") && length == ZSB_BYTES);
    memcpy(durable, in, length); present = true; return true;
}
static void complete(void)
{
    assert(occupied && !finished);
    response = (zj_reply_t){0};
    response.result = (uint64_t)clock_us >= accepted.input.source_boundary.deadline_us ? ZJ_STALE :
        zsb_open((zj_state_port_t){.read=read_blob, .write=write_blob}, &identity,
            accepted.input.source_boundary.create ? &accepted.input.source_boundary.facts : NULL,
            &response.source_boundary);
    finished = true;
}
bool zj_owner_submit(const zj_request_t *request, uint64_t *ticket)
{
    assert(!occupied && request->operation == ZJ_SOURCE_BOUNDARY);
    if (refuse) return false;
    accepted = *request; occupied = true; finished = false;
    *ticket = ++active_ticket; ++submissions;
    /* Simulate a commit whose reply becomes inaccessible until a later call. */
    if (stall) complete();
    return true;
}
bool zj_owner_poll(uint64_t ticket, zj_reply_t *reply, bool *done)
{
    assert(occupied && ticket == active_ticket); *done = false;
    if (nested) {
        nested = false;
        zsb_record_t out;
        assert(zsb_runtime_read(&out) == ZJ_IO && !out.facts.next_ordinal);
    }
    if (stall) return false;
    if (!finished) complete();
    *reply = response; *done = true; occupied = false; return true;
}

int main(void)
{
    zsb_record_t out;
    zsb_facts_t facts = {.next_ordinal=200000, .record_size=40, .anchor_digest={8}};
    assert(zsb_runtime_required());
    strcpy(app.version, "2.6.16");
    assert(!zsb_runtime_required() && zsb_runtime_create(&facts, &out) == ZJ_INVALID && !submissions);
    strcpy(app.version, "2.7.0"); strcpy(app.project_name, "zone_lite_hikvision");
    assert(!zsb_runtime_required()); strcpy(app.project_name, "zone_lite");
    assert(zsb_runtime_read(&out) == ZJ_EMPTY);
    facts.record_size = 28;
    assert(zsb_runtime_create(&facts, &out) == ZJ_INVALID);
    facts.record_size = 40; refuse = true;
    assert(zsb_runtime_create(&facts, &out) == ZJ_IO && !occupied);
    refuse = false; nested = true; stall = true;
    unsigned before = submissions;
    assert(zsb_runtime_create(&facts, &out) == ZJ_UNCERTAIN && occupied && present);
    assert(submissions == before + 1 && !out.facts.next_ordinal);
    facts.next_ordinal = 210000;
    assert(zsb_runtime_create(&facts, &out) == ZJ_UNCERTAIN && submissions == before + 1);
    stall = false;
    assert(zsb_runtime_create(&facts, &out) == ZJ_OK && out.facts.next_ordinal == 200000);
    assert(!occupied && submissions == before + 1);
    assert(zsb_runtime_read(&out) == ZJ_OK && out.facts.next_ordinal == 200000);
    /* An earlier timed-out read of an empty store may be followed by the
     * requested create, after releasing the earlier slot. */
    present = false; stall = true;
    assert(zsb_runtime_read(&out) == ZJ_UNCERTAIN && occupied);
    stall = false;
    assert(zsb_runtime_create(&facts, &out) == ZJ_OK && out.facts.next_ordinal == 210000);
    durable[42] ^= 1;
    assert(zsb_runtime_read(&out) == ZJ_CORRUPT && !out.facts.next_ordinal);
    return 0;
}
