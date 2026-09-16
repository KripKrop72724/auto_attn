#include "evidence_receipt.h"
#include <string.h>

bool evidence_receipt_matches(const evidence_receipt_t *expected, const evidence_receipt_t *receipt)
{
    if (!expected || !receipt || expected->version != 1 || receipt->version != 1 ||
        strlen(receipt->receipt_id) != 36 || strcmp(receipt->disposition, "PRESERVED_UNRESOLVED")) return false;
    return expected->connector_id[0] && expected->queue[0] && expected->generation[0] && expected->record_id[0] &&
        strlen(expected->digest) == 64 && !strcmp(expected->connector_id, receipt->connector_id) &&
        !strcmp(expected->queue, receipt->queue) && !strcmp(expected->generation, receipt->generation) &&
        !strcmp(expected->record_id, receipt->record_id) && !strcmp(expected->digest, receipt->digest);
}
