#include "uid_cache.h"
#include <string.h>

static int hex(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool decode(const char *uid, uint8_t key[32])
{
    if (!uid || strlen(uid) != 64) return false;
    for (size_t i = 0; i < 32; ++i) {
        int hi = hex(uid[2 * i]), lo = hex(uid[2 * i + 1]);
        if (hi < 0 || lo < 0) return false;
        key[i] = (uint8_t)((hi << 4) | lo);
    }
    return true;
}

static size_t bucket(const uint8_t key[32], size_t capacity)
{
    uint32_t hash = 2166136261U;
    for (size_t i = 0; i < 32; ++i) hash = (hash ^ key[i]) * 16777619U;
    return hash % capacity;
}

static bool available(const uid_cache_t *cache)
{
    return cache && cache->keys && cache->occupied && cache->capacity;
}

bool uid_cache_contains(const uid_cache_t *cache, const char *uid)
{
    uint8_t key[32];
    if (!available(cache) || !decode(uid, key)) return false;
    size_t slot = bucket(key, cache->capacity);
    /* Cap collision work under the capture lock. An uncached UID replays. */
    for (size_t i = 0; i < cache->capacity && i < 32; ++i) {
        if (!(cache->occupied[slot / 8] & (1U << (slot % 8)))) return false;
        if (memcmp(cache->keys + slot * 32, key, 32) == 0) return true;
        slot = (slot + 1) % cache->capacity;
    }
    return false;
}

bool uid_cache_add(uid_cache_t *cache, const char *uid)
{
    uint8_t key[32];
    if (!available(cache) || !decode(uid, key)) return false;
    size_t slot = bucket(key, cache->capacity);
    for (size_t i = 0; i < cache->capacity && i < 32; ++i) {
        if (!(cache->occupied[slot / 8] & (1U << (slot % 8)))) {
            memcpy(cache->keys + slot * 32, key, 32);
            cache->occupied[slot / 8] |= (uint8_t)(1U << (slot % 8));
            ++cache->count;
            return true;
        }
        if (memcmp(cache->keys + slot * 32, key, 32) == 0) return true;
        slot = (slot + 1) % cache->capacity;
    }
    return false;
}
