#include "zkt_command_ids.h"
#include "zkt_storage_owner.h"
#include "zkt_runtime_checkpoint.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdatomic.h>

typedef struct { atomic_flag busy; uint64_t pending_ticket; } zi_client_t;
static zi_client_t clients[2] = {{.busy = ATOMIC_FLAG_INIT}, {.busy = ATOMIC_FLAG_INIT}};
static bool collect(zi_client_t *client, uint64_t deadline, zj_reply_t *reply)
{
    do {
        bool complete = false;
        if (zj_owner_poll(client->pending_ticket, reply, &complete) && complete) {
            client->pending_ticket = 0; return true;
        }
        if ((uint64_t)esp_timer_get_time() >= deadline) return false;
        vTaskDelay(pdMS_TO_TICKS(10));
    } while (true);
}
static rel_id_result_t call(zi_kind_t kind, const char *id, bool remember)
{
    if ((unsigned)kind > ZI_CANCELLED || !id || !*id || strlen(id) >= ZI_ID_BYTES ||
        !zj_runtime_checkpoint_required()) return REL_ID_ERROR;
    uint64_t now = (uint64_t)esp_timer_get_time();
    if (now > UINT64_MAX - 5000000ULL) return REL_ID_ERROR;
    uint64_t deadline = now + 5000000ULL;
    zj_request_t request = {.operation = ZJ_COMMAND_IDS,
        .input.command_ids = {.kind = (uint8_t)kind, .remember = remember, .deadline_us = deadline}};
    strcpy(request.input.command_ids.id, id);
    if (!zi_request_valid(&request.input.command_ids)) return REL_ID_ERROR;
    zi_client_t *client = &clients[kind];
    if (atomic_flag_test_and_set_explicit(&client->busy, memory_order_acquire)) return REL_ID_ERROR;
    rel_id_result_t result = REL_ID_ERROR;
    zj_reply_t reply;
    /* Finishing a previous operation never supplies an answer for a new ID.
     * Every retry performs its own authoritative check after collecting it. */
    if (client->pending_ticket && !collect(client, deadline, &reply)) goto done;
    if ((uint64_t)esp_timer_get_time() >= deadline || !zj_owner_submit(&request, &client->pending_ticket)) goto done;
    if (collect(client, deadline, &reply) && reply.result == ZJ_OK)
        result = reply.command_ids.present ? REL_ID_PRESENT : REL_ID_ABSENT;
done:
    atomic_flag_clear_explicit(&client->busy, memory_order_release);
    return result;
}
rel_id_result_t zi_cache_contains(zi_kind_t kind, const char *id) { return call(kind, id, false); }
bool zi_cache_remember(zi_kind_t kind, const char *id) { return call(kind, id, true) == REL_ID_PRESENT; }
