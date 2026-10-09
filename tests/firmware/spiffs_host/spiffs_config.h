/* Host build of ESP-IDF 5.5.3's SPIFFS with the Zone Lite configuration
 * (components/spiffs/include/spiffs_config.h with CONFIG_SPIFFS_PAGE_SIZE 256,
 * OBJ_NAME_LEN 32, META_LENGTH 4, USE_MAGIC, USE_MAGIC_LENGTH, PAGE_CHECK,
 * CACHE, CACHE_WR, GC_MAX_RUNS 10), so the on-flash layout is identical. */
#ifndef SPIFFS_CONFIG_H_
#define SPIFFS_CONFIG_H_

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <unistd.h>
#include <inttypes.h>
#include <assert.h>

#define SPIFFS_DBG(...)
#define SPIFFS_API_DBG(...)
#define SPIFFS_GC_DBG(...)
#define SPIFFS_CACHE_DBG(...)
#define SPIFFS_CHECK_DBG(...)

typedef int32_t s32_t;
typedef uint32_t u32_t;
typedef int16_t s16_t;
typedef uint16_t u16_t;
typedef int8_t s8_t;
typedef uint8_t u8_t;

#define _SPIPRIi   "%" PRIdMAX
#define _SPIPRIad  "%08x"
#define _SPIPRIbl  "%04x"
#define _SPIPRIpg  "%04x"
#define _SPIPRIsp  "%04x"
#define _SPIPRIfd  "%d"
#define _SPIPRIid  "%04x"
#define _SPIPRIfl  "%02x"

#define SPIFFS_BUFFER_HELP              0
#define SPIFFS_CACHE                    (1)
#define SPIFFS_CACHE_WR                 (1)
#define SPIFFS_CACHE_STATS              (0)
#define SPIFFS_PAGE_CHECK               (1)
#define SPIFFS_GC_MAX_RUNS              10
#define SPIFFS_GC_STATS                 (0)
#define SPIFFS_GC_HEUR_W_DELET          (5)
#define SPIFFS_GC_HEUR_W_USED           (-1)
#define SPIFFS_GC_HEUR_W_ERASE_AGE      (50)
#define SPIFFS_OBJ_NAME_LEN             (32)
#define SPIFFS_OBJ_META_LEN             (4)
#define SPIFFS_PAGE_EXTRA_SIZE          (64)
#define SPIFFS_COPY_BUFFER_STACK        (256)
#define SPIFFS_USE_MAGIC                (1)
#define SPIFFS_USE_MAGIC_LENGTH         (1)
#define SPIFFS_LOCK(fs)
#define SPIFFS_UNLOCK(fs)
#define SPIFFS_SINGLETON 0
#define SPIFFS_ALIGNED_OBJECT_INDEX_TABLES      0
#define SPIFFS_HAL_CALLBACK_EXTRA               1
#define SPIFFS_FILEHDL_OFFSET                   0
#define SPIFFS_READ_ONLY                        0
#define SPIFFS_TEMPORAL_FD_CACHE                1
#define SPIFFS_TEMPORAL_CACHE_HIT_SCORE         4
#define SPIFFS_IX_MAP                           1
#define SPIFFS_TEST_VISUALISATION               0

typedef u16_t spiffs_block_ix;
typedef u16_t spiffs_page_ix;
typedef u16_t spiffs_obj_id;
typedef u16_t spiffs_span_ix;

#endif /* SPIFFS_CONFIG_H_ */
