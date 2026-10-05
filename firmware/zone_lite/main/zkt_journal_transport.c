#include "zkt_journal_transport.h"
#include "zkt_storage_owner.h"
#include "add_connector.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

static zj_delivery_t *delivery;
static zj_transport_health_t snapshot;
static SemaphoreHandle_t health_lock;
static TaskHandle_t transport_task;
/* Reserve the complete internal stack before Wi-Fi/TLS fragments the heap.
 * The 3FL canary had 35 KiB free but no 12 KiB contiguous internal block.
 * Keep the measured stack size; PSRAM is only for the bounded delivery state. */
#define ZJ_TRANSPORT_STACK_BYTES 12288U
static DRAM_ATTR StackType_t transport_stack[ZJ_TRANSPORT_STACK_BYTES / sizeof(StackType_t)]
    __attribute__((aligned(16)));
static DRAM_ATTR StaticTask_t transport_control;
static DRAM_ATTR StaticSemaphore_t health_mutex;
_Static_assert(sizeof(transport_stack) == ZJ_TRANSPORT_STACK_BYTES, "journal transport stack size");
static uint32_t now_ms(void *context) { (void)context; return (uint32_t)(esp_timer_get_time() / 1000); }
static uint32_t random_value(void *context) { (void)context; return esp_random(); }
static bool connected(void *context) { (void)context; return add_connector_is_connected(); }
static bool submit(void *context, const zj_request_t *request, uint64_t *ticket)
{ (void)context; return zj_owner_submit(request, ticket); }
static bool poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{ (void)context; return zj_owner_poll(ticket, reply, complete); }
static bool abandon(void *context, uint64_t ticket)
{ (void)context; return zj_owner_abandon(ticket); }
static bool send_custody(void *context, const char *payload, uint32_t timeout, uint8_t receipt[32])
{ (void)context; return add_connector_send_zkt_custody_acknowledged(payload, timeout, receipt); }
static bool digest(void *context, const uint8_t *bytes, size_t length, uint8_t out[32])
{ (void)context; return mbedtls_sha256(bytes, length, out, 0) == 0; }

static void publish_snapshot(const zj_delivery_t *state, void *context)
{
    (void)context;
    if (xSemaphoreTake(health_lock, pdMS_TO_TICKS(100)) != pdTRUE) return;
    snapshot.started = true;
    snapshot.sampled_ms = now_ms(NULL);
    snapshot.delivery = state->health;
    xSemaphoreGive(health_lock);
}
static void task(void *context)
{
    zj_delivery_t *state = context;
    for (;;) {
        /* The snapshot mutex is released before an owner/network request.
         * Telemetry can distinguish a blocked SEND from healthy idle state. */
        uint32_t delay = zj_delivery_pump(state, publish_snapshot, NULL);
        TickType_t ticks = pdMS_TO_TICKS(delay);
        vTaskDelay(ticks ? ticks : 1);
    }
}
bool zj_transport_start(void)
{
    zj_owner_health_t owner;
    if (delivery || !zj_owner_health(&owner) || !owner.started) return false;
    if (!health_lock) health_lock = xSemaphoreCreateMutexStatic(&health_mutex);
    if (!health_lock) return false;
    delivery = heap_caps_calloc(1, sizeof(*delivery), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    zj_delivery_port_t port = {now_ms, random_value, connected, submit, poll, abandon, send_custody,
        NULL, {.digest = digest}};
    if (!delivery || !zj_delivery_init(delivery, port)) {
        heap_caps_free(delivery);
        delivery = NULL;
        return false;
    }
    transport_task = xTaskCreateStatic(task, "zkt_journal_tx", ZJ_TRANSPORT_STACK_BYTES,
        delivery, 3, transport_stack, &transport_control);
    if (!transport_task) {
        heap_caps_free(delivery);
        delivery = NULL;
        return false;
    }
    return true;
}
bool zj_transport_health(zj_transport_health_t *health)
{
    if (!health || !health_lock || xSemaphoreTake(health_lock, pdMS_TO_TICKS(100)) != pdTRUE) return false;
    *health = snapshot;
    xSemaphoreGive(health_lock);
    return true;
}
