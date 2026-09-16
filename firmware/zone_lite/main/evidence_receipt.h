#pragma once
#include <stdbool.h>

typedef struct {
    unsigned version;
    char connector_id[101], queue[41], generation[81], record_id[121], digest[65];
    char receipt_id[37], disposition[40];
} evidence_receipt_t;
bool evidence_receipt_matches(const evidence_receipt_t *expected, const evidence_receipt_t *receipt);
