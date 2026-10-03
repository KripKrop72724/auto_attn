#include "zkt_journal_transport.h"
#include "zkt_storage_owner.h"
#include "add_connector.h"
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

static void publish_snapshot(zj_delivery_t *state)
{
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
        publish_snapshot(state);
        /* The snapshot mutex is released before an owner/network request.
         * Telemetry can distinguish a blocked SEND from healthy idle state. */
        zj_delivery_step(state);
        publish_snapshot(state);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
bool zj_transport_start(void)
{
    zj_owner_health_t owner;
    if (delivery || !zj_owner_health(&owner) || !owner.started) return false;
    health_lock = xSemaphoreCreateMutex();
    if (!health_lock) return false;
    delivery = heap_caps_calloc(1, sizeof(*delivery), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    zj_delivery_port_t port = {now_ms, random_value, connected, submit, poll, abandon, send_custody,
        NULL, {.digest = digest}};
    if (!delivery || !zj_delivery_init(delivery, port) ||
        xTaskCreate(task, "zkt_journal_tx", 12288, delivery, 3, &transport_task) != pdPASS) {
        heap_caps_free(delivery);
        delivery = NULL;
        vSemaphoreDelete(health_lock);
        health_lock = NULL;
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
