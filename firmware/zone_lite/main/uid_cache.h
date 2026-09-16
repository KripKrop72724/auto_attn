#ifndef UID_CACHE_H
#define UID_CACHE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Volatile optimization only. Callers insert only after durable acceptance and
 * serialize access. Exhaustion may cause replay, never a false duplicate. */
typedef struct {
    uint8_t *keys;
    uint8_t *occupied;
    size_t capacity;
    size_t count;
} uid_cache_t;
bool uid_cache_contains(const uid_cache_t *cache, const char *uid);
bool uid_cache_add(uid_cache_t *cache, const char *uid);
#endif
