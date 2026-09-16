#pragma once
#include "durable_queue.h"
/* Replacement generations preserve the old file until the new prefix and the
 * committed generation are durable. Owners serialize all access to these paths. */
typedef struct {
    uint32_t version, generation, phase, length, digest, crc;
} ft_checkpoint_t;
typedef struct {
    int (*load)(void *, ft_checkpoint_t *);
    bool (*commit)(void *, const ft_checkpoint_t *);
    void *context;
} ft_port_t;
bool ft_recover(const char *active, const char *stage, const char *backup, ft_port_t port);
bool ft_replace(const char *active, const char *stage, const char *backup, size_t limit, ft_port_t port);
