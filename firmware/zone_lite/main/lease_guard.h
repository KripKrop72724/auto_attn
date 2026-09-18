#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum { LG_OK, LG_TIME, LG_STORAGE, LG_TERMINAL, LG_EXPIRED } lg_result_t;
typedef struct {
    int64_t (*now)(void *);
    bool (*persist)(void *, uint16_t uid, int64_t deadline, bool active);
    bool (*elevate)(void *, uint16_t uid);
    bool (*verify)(void *, uint16_t uid, int privilege);
    bool (*revoke)(void *, uint16_t uid);
    void *context;
} lg_port_t;
/* A durable bounded revocation obligation precedes any terminal elevation.
 * The normal duration begins after verification, with another checked commit. */
lg_result_t lg_grant(lg_port_t port, uint16_t uid, unsigned duration_seconds, int64_t absolute_deadline);
