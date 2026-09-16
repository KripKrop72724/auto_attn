#include "uid_cache.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    uint8_t keys[8 * 32] = {0}, occupied[1] = {0};
    uid_cache_t cache = {keys, occupied, 8, 0};
    char uid[65];
    /* All-zero UIDs are valid; occupancy must not use a key sentinel. A tiny
     * table forces bucket collisions and exercises exact full-UID comparisons. */
    for (unsigned i = 0; i < 8; ++i) {
        snprintf(uid, sizeof(uid), "%064x", i);
        assert(!uid_cache_contains(&cache, uid));
        assert(uid_cache_add(&cache, uid));
        assert(uid_cache_contains(&cache, uid));
    }
    assert(cache.count == 8);
    for (unsigned i = 8; i < 10000; ++i) {
        snprintf(uid, sizeof(uid), "%064x", i);
        assert(!uid_cache_contains(&cache, uid));
        assert(!uid_cache_add(&cache, uid));
    }
    snprintf(uid, sizeof(uid), "%064x", 7);
    assert(uid_cache_add(&cache, uid));
    assert(cache.count == 8);
    assert(!uid_cache_add(&cache, ""));
    memset(uid, 'g', 64); uid[64] = 0;
    assert(!uid_cache_add(&cache, uid));
    assert(!uid_cache_contains(&cache, uid));
    cache.keys = NULL;
    assert(!uid_cache_add(&cache, uid));
    assert(!uid_cache_contains(&cache, uid));
    puts("UID cache regression tests passed");
    return 0;
}
