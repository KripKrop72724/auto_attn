/*
 * Flash test.
 *   1. The JEDEC ID read again must equal the one BEGIN printed.
 *   2. The chip's own (JEDEC) size must be 16 MiB.
 *   3. The whole "fltest" partition (2..16 MiB) is read twice; the two CRCs
 *      must match (unstable reads = bad chip or wiring).
 *   4. For MB n = 2..15 one 64 KiB block at n MiB is erased (must read 0xFF),
 *      written with a pseudo-random pattern (must read back the same), and
 *      erased again (must read 0xFF). ERR test= names: erase, write, erase2.
 * After END, sl_flash_clean() re-checks every block this test touched and
 * erases any that does not read 0xFF.
 */
#include <inttypes.h>
#include <string.h>

#include "board_test.h"
#include "esp_flash.h"
#include "esp_partition.h"
#include "esp_rom_crc.h"

#define MIB (1024u * 1024u)
#define BLOCK (64u * 1024u)
#define BUF 4096u
#define FIRST_MB 2
#define LAST_MB 15
#define FLTEST_SUBTYPE 0x99

static uint32_t s_touched; /* bit n = block at n MiB was erased or written */
static uint32_t s_buf[BUF / 4u];

static inline uint32_t lfsr_next(uint32_t s)
{
    return (s >> 1) ^ (-(s & 1u) & 0xD0000001u);
}

static const esp_partition_t *fltest(void)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_DATA, FLTEST_SUBTYPE, "fltest");
}

static void api_error(const char *test, uint32_t addr, esp_err_t err)
{
    sl_err(test, addr, 0, (uint32_t)err);
}

static bool erase_block(uint32_t addr, const char *test)
{
    sl_busy(3000); /* one 64 KiB block erase can take a few hundred ms */
    esp_err_t err = esp_flash_erase_region(NULL, addr, BLOCK);
    sl_tick(addr);
    if (err != ESP_OK) {
        api_error(test, addr, err);
        return false;
    }
    return true;
}

/* Returns the number of words that did not read 0xFFFFFFFF. */
static uint32_t check_erased(uint32_t addr, const char *test, bool report)
{
    uint32_t bad = 0;
    for (uint32_t off = 0; off < BLOCK; off += BUF) {
        esp_err_t err = esp_flash_read(NULL, s_buf, addr + off, BUF);
        if (err != ESP_OK) {
            if (report) {
                api_error(test, addr + off, err);
            }
            bad++;
            continue;
        }
        for (uint32_t i = 0; i < BUF / 4u; i++) {
            if (s_buf[i] != 0xFFFFFFFFu) {
                if (report) {
                    sl_err(test, addr + off + i * 4u, 0xFFFFFFFFu, s_buf[i]);
                }
                bad++;
            }
        }
        sl_tick(addr + off);
        sl_yield();
    }
    return bad;
}

static void write_and_check(uint32_t addr, uint32_t seed)
{
    uint32_t s = seed;
    for (uint32_t off = 0; off < BLOCK; off += BUF) {
        for (uint32_t i = 0; i < BUF / 4u; i++) {
            s_buf[i] = s;
            s = lfsr_next(s);
        }
        esp_err_t err = esp_flash_write(NULL, s_buf, addr + off, BUF);
        if (err != ESP_OK) {
            api_error("write", addr + off, err);
        }
        sl_tick(addr + off);
        sl_yield();
    }
    s = seed;
    for (uint32_t off = 0; off < BLOCK; off += BUF) {
        esp_err_t err = esp_flash_read(NULL, s_buf, addr + off, BUF);
        if (err != ESP_OK) {
            api_error("write", addr + off, err);
            for (uint32_t i = 0; i < BUF / 4u; i++) {
                s = lfsr_next(s);
            }
            continue;
        }
        for (uint32_t i = 0; i < BUF / 4u; i++) {
            if (s_buf[i] != s) {
                sl_err("write", addr + off + i * 4u, s, s_buf[i]);
            }
            s = lfsr_next(s);
        }
        sl_tick(addr + off);
        sl_yield();
    }
}

static bool read_crc(const esp_partition_t *part, uint32_t *crc_out)
{
    uint32_t crc = 0;
    for (uint32_t off = 0; off < part->size; off += BUF) {
        esp_err_t err = esp_partition_read(part, off, s_buf, BUF);
        if (err != ESP_OK) {
            api_error("read", part->address + off, err);
            return false;
        }
        crc = esp_rom_crc32_le(crc, (const uint8_t *)s_buf, BUF);
        if ((off & 0xFFFFu) == 0) {
            sl_tick(part->address + off);
            sl_yield();
        }
    }
    *crc_out = crc;
    return true;
}

static void result(const char *res, const char *code, const char *detail, int64_t t0,
                   uint32_t id, uint32_t size)
{
    sl_line("RESULT stage=flash result=%s code=%s detail=%s errors=%lu ms=%ld id=%06lx size=%lu "
            "blocks=%d",
            res, code, detail, (unsigned long)sl_errors(), (long)((sl_now_us() - t0) / 1000),
            (unsigned long)id, (unsigned long)size, LAST_MB - FIRST_MB + 1);
}

bool sl_test_flash(uint32_t begin_flash_id)
{
    const int blocks = LAST_MB - FIRST_MB + 1;
    int64_t t0 = sl_now_us();
    /* steps: id, size, read 1, read 2, then one per block */
    sl_stage("flash", 4 + blocks);
    sl_errors_reset();

    sl_step(1);
    uint32_t id = 0;
    esp_err_t err = esp_flash_read_id(NULL, &id);
    if (err != ESP_OK || id != begin_flash_id) {
        sl_err("id", 0, begin_flash_id, err != ESP_OK ? (uint32_t)err : id);
        result("fail", "FLASH_ID_CHANGED", "id_read_differs", t0, id, 0);
        return false;
    }

    sl_step(2);
    uint32_t size = 0;
    if (esp_flash_get_physical_size(NULL, &size) != ESP_OK) {
        (void)esp_flash_get_size(NULL, &size);
    }
    if (size != 16u * MIB) {
        sl_err("size", 0, 16u * MIB, size);
        result("fail", "FLASH_WRONG_SIZE", "size_not_16mb", t0, id, size);
        return false;
    }

    const esp_partition_t *part = fltest();
    if (part == NULL) {
        result("fail", "FLASH_TEST_AREA_MISSING", "no_fltest_partition", t0, id, size);
        return false;
    }

    sl_step(3);
    uint32_t crc1 = 0, crc2 = 0;
    bool read_ok = read_crc(part, &crc1);
    sl_step(4);
    read_ok = read_crc(part, &crc2) && read_ok;
    if (!read_ok || crc1 != crc2) {
        if (read_ok) {
            sl_err("read", part->address, crc1, crc2);
        }
        result("fail", "FLASH_READ_UNSTABLE", read_ok ? "crc_differs" : "read_error", t0, id,
               size);
        return false;
    }

    for (int n = FIRST_MB; n <= LAST_MB; n++) {
        sl_step(5 + (n - FIRST_MB));
        uint32_t addr = (uint32_t)n * MIB;
        s_touched |= 1u << n;
        if (erase_block(addr, "erase")) {
            check_erased(addr, "erase", true);
        }
        write_and_check(addr, 0xF1A50000u | (uint32_t)n);
        if (erase_block(addr, "erase2")) {
            check_erased(addr, "erase2", true);
        }
    }
    if (sl_errors()) {
        result("fail", "FLASH_WRITE_ERRORS", sl_first_failed_test(), t0, id, size);
        return false;
    }
    result("ok", "OK", "all_blocks_ok", t0, id, size);
    return true;
}

bool sl_flash_clean(void)
{
    bool ok = true;
    for (int n = FIRST_MB; n <= LAST_MB; n++) {
        if (!(s_touched & (1u << n))) {
            continue;
        }
        uint32_t addr = (uint32_t)n * MIB;
        if (check_erased(addr, "clean", false) == 0) {
            continue;
        }
        if (esp_flash_erase_region(NULL, addr, BLOCK) != ESP_OK ||
            check_erased(addr, "clean", false) != 0) {
            ok = false;
        }
    }
    return ok;
}
