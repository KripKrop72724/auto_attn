#include "evidence_receipt.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void)
{
    evidence_receipt_t expected={.version=1};
    strcpy(expected.connector_id,"connector");strcpy(expected.queue,"blocked");
    strcpy(expected.generation,"storage-instance");strcpy(expected.record_id,"segment:offset");
    memset(expected.digest,'a',64);
    evidence_receipt_t receipt=expected;
    strcpy(receipt.receipt_id,"12345678-1234-1234-1234-123456789abc");
    strcpy(receipt.disposition,"PRESERVED_UNRESOLVED");
    assert(evidence_receipt_matches(&expected,&receipt));
    char *fields[]={receipt.connector_id,receipt.queue,receipt.generation,receipt.record_id,receipt.digest};
    for(unsigned i=0;i<5;++i) {
        char original=fields[i][0]; fields[i][0]='X';
        assert(!evidence_receipt_matches(&expected,&receipt));fields[i][0]=original;
    }
    receipt.version=0;assert(!evidence_receipt_matches(&expected,&receipt));receipt.version=1;
    receipt.receipt_id[0]=0;assert(!evidence_receipt_matches(&expected,&receipt));
    receipt.receipt_id[0]='1';strcpy(receipt.disposition,"RESOLVED");
    assert(!evidence_receipt_matches(&expected,&receipt));
    memset(&receipt,0,sizeof(receipt));assert(!evidence_receipt_matches(&expected,&receipt));
    puts("evidence receipt regression tests passed");
}
