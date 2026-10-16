#include "storage_repair_mirror.h"

#include <string.h>

size_t srm_lookup_bytes(uint32_t size, uint32_t block_size, uint32_t page_size)
{
    return block_size ? (size_t)(size / block_size) * page_size : 0;
}

size_t srm_header_bytes(uint32_t size, uint32_t page_size)
{
    return page_size ? (size_t)(size / page_size) * SRM_HEADER_BYTES : 0;
}

/* The mirrored bytes of page `page`: its whole lookup page, or its header. */
static uint8_t *mirrored(srm_mirror_t *m, uint32_t page, uint32_t *length)
{
    uint32_t per_block = m->block_size / m->page_size;
    if (page % per_block == 0) {
        *length = m->page_size;
        return m->lookup + (size_t)(page / per_block) * m->page_size;
    }
    *length = SRM_HEADER_BYTES;
    return m->headers + (size_t)page * SRM_HEADER_BYTES;
}

static bool refresh_page(srm_mirror_t *m, uint32_t page)
{
    uint32_t length;
    uint8_t *copy = mirrored(m, page, &length);
    m->refreshes++;
    if (!m->flash.read(m->flash.context, page * m->page_size, copy, length)) {
        m->valid = false;
        return false;
    }
    return true;
}

bool srm_init(srm_mirror_t *m, const srm_flash_t *flash, uint32_t size, uint32_t block_size,
              uint32_t page_size, uint8_t *lookup, uint8_t *headers, uint8_t *scratch, size_t scratch_size)
{
    if (!m) return false;
    memset(m, 0, sizeof(*m));
    if (!flash || !flash->read || !flash->write || !flash->erase || !lookup || !headers || !scratch ||
        !page_size || !block_size || block_size % page_size || size % block_size || scratch_size < block_size ||
        page_size < SRM_HEADER_BYTES)
        return false;
    m->flash = *flash;
    m->size = size;
    m->block_size = block_size;
    m->page_size = page_size;
    m->blocks = size / block_size;
    m->pages = size / page_size;
    m->lookup = lookup;
    m->headers = headers;
    uint32_t per_block = block_size / page_size;
    for (uint32_t block = 0; block < m->blocks; ++block) {
        if (!flash->read(flash->context, block * block_size, scratch, block_size)) return false;
        memcpy(lookup + (size_t)block * page_size, scratch, page_size);
        for (uint32_t i = 0; i < per_block; ++i)
            memcpy(headers + (size_t)(block * per_block + i) * SRM_HEADER_BYTES, scratch + i * page_size,
                   SRM_HEADER_BYTES);
    }
    m->valid = true;
    return true;
}

bool srm_read(srm_mirror_t *m, uint32_t address, void *buffer, uint32_t length)
{
    if (m->valid && length && (uint64_t)address + length <= m->size) {
        uint32_t page = address / m->page_size, within = address % m->page_size, mirrored_length;
        uint8_t *copy = mirrored(m, page, &mirrored_length);
        if (within + length <= mirrored_length) {
            memcpy(buffer, copy + within, length);
            m->hits++;
            return true;
        }
    }
    m->misses++;
    return m->flash.read(m->flash.context, address, buffer, length);
}

bool srm_write(srm_mirror_t *m, uint32_t address, const void *buffer, uint32_t length)
{
    bool ok = m->flash.write(m->flash.context, address, buffer, length);
    if (!m->valid || !length || (uint64_t)address + length > m->size) return ok;
    const uint8_t *source = buffer;
    for (uint32_t page = address / m->page_size; page <= (address + length - 1) / m->page_size; ++page) {
        if (!ok) {
            if (!refresh_page(m, page)) return false;
            continue;
        }
        uint32_t mirrored_length;
        uint8_t *copy = mirrored(m, page, &mirrored_length);
        uint32_t page_start = page * m->page_size;
        for (uint32_t i = 0; i < mirrored_length; ++i) {
            uint32_t at = page_start + i;
            if (at >= address && at < address + length) copy[i] &= source[at - address];
        }
    }
    return ok;
}

bool srm_erase(srm_mirror_t *m, uint32_t address, uint32_t length)
{
    bool ok = m->flash.erase(m->flash.context, address, length);
    if (!m->valid || !length || (uint64_t)address + length > m->size) return ok;
    for (uint32_t page = address / m->page_size; page <= (address + length - 1) / m->page_size; ++page) {
        if (!ok) {
            if (!refresh_page(m, page)) return false;
            continue;
        }
        uint32_t mirrored_length;
        uint8_t *copy = mirrored(m, page, &mirrored_length);
        memset(copy, 0xFF, mirrored_length);
    }
    return ok;
}
