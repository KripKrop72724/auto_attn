#pragma once
#include "zkt_journal_delivery.h"

typedef struct {
    bool started;
    uint32_t sampled_ms;
    zj_delivery_health_t delivery;
} zj_transport_health_t;
/* Start only through the qualified bridge/writer gate, after the storage owner
 * exists. A live task and a custody receipt are not Oracle completion proof. */
bool zj_transport_start(void);
bool zj_transport_health(zj_transport_health_t *health);
