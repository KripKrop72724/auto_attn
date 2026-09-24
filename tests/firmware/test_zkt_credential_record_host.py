from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_zkt_user_record_clear_preserves_all_other_fields(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "credential_record.c"
    unit.write_text(r'''#include "zkt_credential_record.h"
#include <assert.h>
#include <stdint.h>
#include <string.h>

static void check(size_t size, size_t pin_end, size_t card_start) {
    uint8_t original[72], cleared[72], readback[72];
    for (size_t i=0; i<size; ++i) original[i]=(uint8_t)(i+1);
    memcpy(readback, original, size);
    zkt_credential_fields_t fields={0};
    assert(zkt_credential_record_clear(original,size,cleared,&fields));
    assert(fields.pin_present && fields.card_present);
    assert(zkt_credential_record_matches_clear(original,cleared,size));
    for (size_t i=0; i<size; ++i) {
        if ((i>=3 && i<pin_end) || (i>=card_start && i<card_start+4))
            assert(cleared[i]==0);
        else assert(cleared[i]==original[i]);
    }
    memcpy(readback,cleared,size);
    readback[card_start]=1;
    assert(!zkt_credential_record_matches_clear(original,readback,size));
    memcpy(readback,cleared,size);
    readback[pin_end]=0;
    assert(!zkt_credential_record_matches_clear(original,readback,size));
    memset(original+3,0,pin_end-3);
    memset(original+card_start,0,4);
    assert(zkt_credential_record_clear(original,size,cleared,&fields));
    assert(!fields.pin_present && !fields.card_present);
    assert(memcmp(original,cleared,size)==0);
}

int main(void) {
    check(28,8,16);
    check(72,11,35);
    uint8_t record[72]={0};
    zkt_credential_fields_t fields={0};
    assert(!zkt_credential_record_clear(record,64,record,&fields));
    assert(!zkt_credential_record_matches_clear(record,record,64));
    return 0;
}''')
    exe = tmp_path / "zkt-credential-record"
    subprocess.run(
        [shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-I", str(main), str(unit),
         str(main / "zkt_credential_record.c"), "-o", str(exe)],
        check=True,
    )
    subprocess.run([str(exe)], check=True)
