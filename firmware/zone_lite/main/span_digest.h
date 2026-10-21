#pragma once
/* ZKT 2.7.7: an order-free digest of a set of SPIFFS data span indices.
 *
 * The boot repair names the spans of a file it holds on flash but SPIFFS
 * cannot read yet (its blocked spans); the read-only inventory names the
 * spans the VFS could not read back. The confirm-first exception compares the
 * two sets: their count, first, last and this digest (a sum of mixed
 * indices, so the order in which either side meets the spans does not
 * matter). A consistency check, not a cryptographic one. */
#include <stdint.h>

static inline uint32_t span_digest_add(uint32_t digest, uint32_t span)
{
    uint32_t h = span + 0x9E3779B9U;
    h ^= h >> 16;
    h *= 0x85EBCA6BU;
    h ^= h >> 13;
    h *= 0xC2B2AE35U;
    h ^= h >> 16;
    return digest + h;
}
