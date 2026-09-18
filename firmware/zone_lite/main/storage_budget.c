#include "storage_budget.h"
#include <stdint.h>

bool storage_budget_admit(storage_budget_t *budget, size_t total, size_t used,
                          size_t bytes, sb_class_t kind)
{
    if (!budget || !total || used > total || (unsigned)kind > SB_RECOVERY) return false;
    uint64_t total64 = total, used64 = used;
    if (used64 * 100 >= total64 * 60) budget->bulk_paused = true;
    else if (used64 * 100 < total64 * 55) budget->bulk_paused = false;
    budget->admission_ceiling = (size_t)(total64 * 75 / 100);
    budget->live_reserve = 512U * 1024U;
    budget->recovery_reserve = 512U * 1024U;
    size_t ceiling = budget->admission_ceiling;
    size_t reserve = kind == SB_HISTORICAL ? budget->live_reserve + budget->recovery_reserve :
        kind == SB_LIVE ? budget->recovery_reserve : 0;
    if (kind == SB_HISTORICAL && (budget->bulk_paused || used64 * 100 >= total64 * 70)) return false;
    if (ceiling < reserve) return false;
    ceiling -= reserve;
    /* SPIFFS allocates pages and may need another block for metadata/GC.
     * Charge at least one 4 KiB unit; measured usage is refreshed every call. */
    if (bytes > SIZE_MAX - 4095) return false;
    size_t charged = bytes ? ((bytes + 4095) / 4096) * 4096 : 0;
    return used <= ceiling && charged <= ceiling - used;
}
