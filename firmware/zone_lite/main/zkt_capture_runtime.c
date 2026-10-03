#include "zkt_capture_runtime.h"
#include "zkt_storage_owner.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"

static zj_capture_t *capture;
static zj_capture_health_t snapshot;
static SemaphoreHandle_t capture_lock, health_lock;
static void publish(void)
{
    if (xSemaphoreTake(health_lock, pdMS_TO_TICKS(20)) != pdTRUE) return;
    snapshot = capture->health;
    xSemaphoreGive(health_lock);
}
static uint32_t now_ms(void *context) { (void)context; return (uint32_t)(esp_timer_get_time() / 1000); }
static void wait_ms(void *context, uint32_t milliseconds)
{ (void)context; publish(); vTaskDelay(pdMS_TO_TICKS(milliseconds)); }
static bool random_bytes(void *context, uint8_t *out, size_t length)
{ (void)context; esp_fill_random(out, length); return true; }
static bool submit(void *context, const zj_request_t *request, uint64_t *ticket)
{ (void)context; return zj_owner_submit(request, ticket); }
static bool poll(void *context, uint64_t ticket, zj_reply_t *reply, bool *complete)
{ (void)context; return zj_owner_poll(ticket, reply, complete); }
static bool abandon(void *context, uint64_t ticket)
{ (void)context; return zj_owner_abandon(ticket); }
static bool digest(void *context, const uint8_t *bytes, size_t length, uint8_t out[32])
{ (void)context; return mbedtls_sha256(bytes, length, out, 0) == 0; }
bool zj_capture_runtime_start(void)
{
    zj_owner_health_t owner;
    if (capture || !zj_owner_health(&owner) || !owner.ready) return false;
    capture_lock = xSemaphoreCreateMutex();
    health_lock = xSemaphoreCreateMutex();
    capture = heap_caps_calloc(1, sizeof(*capture), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    zj_capture_port_t port = {now_ms, wait_ms, random_bytes, submit, poll, abandon, NULL, {.digest = digest}};
    if (!capture_lock || !health_lock || !capture || !zj_capture_init(capture, port)) {
        if (capture_lock) vSemaphoreDelete(capture_lock);
        if (health_lock) vSemaphoreDelete(health_lock);
        heap_caps_free(capture);
        capture_lock = health_lock = NULL;
        capture = NULL;
        return false;
    }
    return true;
}
bool zj_capture_runtime_packet(const uint8_t *packet, size_t length, const zj_capture_facts_t *facts)
{
    if (!capture || xSemaphoreTake(capture_lock, pdMS_TO_TICKS(100)) != pdTRUE) return false;
    bool preserved = zj_capture_packet(capture, packet, length, facts);
    publish();
    xSemaphoreGive(capture_lock);
    return preserved;
}
bool zj_capture_runtime_health(zj_capture_health_t *health)
{
    if (!health || !health_lock || xSemaphoreTake(health_lock, pdMS_TO_TICKS(100)) != pdTRUE) return false;
    *health = snapshot;
    xSemaphoreGive(health_lock);
    return true;
}
