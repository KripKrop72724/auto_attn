"""Sanitized protocol fixtures; these do not certify an installed terminal."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_layout_boundaries_and_occurrence_fields(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "records.c"
    unit.write_text(r'''
#include "zkt_record.h"
#include <assert.h>
#include <string.h>
int main(void) {
    const uint32_t sizes[]={40,16,8};
    assert(zkt_record_size(8000000,200000,sizes,3)==40);
    assert(zkt_record_size(160,0,sizes,3)==0); /* Three possible layouts. */
    assert(zkt_record_size(160,5,sizes,3)==0); /* No count/size agreement. */
    assert(zkt_record_size(160,4,sizes,3)==40);
    uint32_t offset,length;
    assert(zkt_record_range(8000004,40,199999,200000,&offset,&length));
    assert(offset==7999964 && length==40);
    assert(!zkt_record_range(8000004,40,199999,200001,&offset,&length));
    assert(!zkt_record_range(8000004,40,UINT32_MAX-1,UINT32_MAX,&offset,&length));
    assert(!zkt_record_range(8000003,40,0,1,&offset,&length));
    zkt_record_t record;
    uint8_t raw[40]={7,0,'1','2','3'};
    raw[27]=42;raw[26]=3;raw[31]=4;
    assert(zkt_record_decode(raw,40,&record));
    assert(record.attendance_uid==7 && !strcmp(record.user_id,"123"));
    assert(record.encoded_time==42 && record.status==3 && record.punch==4);
    assert(!zkt_record_decode(raw,39,&record));
    raw[3]=1;assert(!zkt_record_decode(raw,40,&record));
    memset(raw,0,sizeof(raw));raw[0]=7;raw[3]=42;
    assert(zkt_record_decode(raw,8,&record) && record.attendance_uid==7);
    assert(record.encoded_time==42 && !record.user_id[0]);
    /* Zero timestamps observed in source exceptions stay zero. */
    memset(raw,0,sizeof(raw));raw[2]='7';
    assert(zkt_record_decode(raw,40,&record) && record.encoded_time==0);
    const char *models[]={"G3","SilkBio-101TC/ID","MB40-VL/ID","uFace800","uFace800/ID","uFace800 Plus/ID"};
    for(unsigned i=0;i<6;++i)assert(zkt_model_profile(models[i]));
    assert(!zkt_model_profile("unqualified variant"));
    return 0;
}
''')
    executable = tmp_path / "records"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra",
                    "-Werror", "-fsanitize=address,undefined", "-I", str(main),
                    str(unit), str(main / "zkt_record.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


def test_tailed_49_byte_records_decode_only_their_40_byte_prefix(tmp_path):
    """Synthetic MB40-VL/ID (ZAM180_TFT) layout: 40 bytes plus a 9-byte tail."""
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "records49.c"
    unit.write_text(r'''
#include "zkt_record.h"
#include <assert.h>
#include <string.h>
int main(void) {
    const uint32_t sizes[]={40,16,8,ZKT_RECORD_TAILED_BYTES};
    /* LHRCENTRAL-01 on 4 Nov 2026: payload=651357 count=13293. */
    assert(zkt_record_size(651357,13293,sizes,4)==49);
    assert(zkt_record_size(651357,13294,sizes,4)==0);
    assert(zkt_record_size(651357,0,sizes,4)==49); /* No other width divides it. */
    /* A count always names one width; without one, 40 and 49 stay ambiguous. */
    assert(zkt_record_size(1960,49,sizes,4)==40 && zkt_record_size(1960,40,sizes,4)==49);
    assert(zkt_record_size(1960,0,sizes,4)==0);
    assert(zkt_record_size(8000000,200000,sizes,4)==40 && zkt_record_size(160,4,sizes,4)==40);
    assert(zkt_record_size(3200000,200000,sizes,4)==16 && zkt_record_size(1600000,200000,sizes,4)==8);
    uint32_t offset,length;
    assert(zkt_record_range(651361,49,13292,13293,&offset,&length));
    assert(offset==4+13292*49 && length==49);
    assert(!zkt_record_range(651361,49,13292,13294,&offset,&length));
    assert(!zkt_record_range(651361,40,0,1,&offset,&length));
    static const uint8_t tails[2][9]={{0xff,'2','5','5'},{0x00,'2','5','5'}};
    uint8_t raw[ZKT_RECORD_MAX_BYTES]={0x2a,0x05,'1','2','3','4'};
    raw[26]=15;raw[27]=0x6e;raw[28]=0x23;raw[29]=0x42;raw[30]=0x33;
    zkt_record_t prefix,record;
    assert(zkt_record_decode(raw,40,&prefix));
    for(unsigned i=0;i<2;++i){
        memcpy(raw+40,tails[i],9);
        assert(zkt_record_decode(raw,49,&record) && !memcmp(&record,&prefix,sizeof(record)));
        assert(record.attendance_uid==0x052a && !strcmp(record.user_id,"1234"));
        assert(record.encoded_time==859972462U && record.status==15 && !record.punch);
        assert(!zkt_record_identity_missing(raw,49));
    }
    /* The tail is never read as identity, time or status. */
    memset(raw+40,0xff,9);
    assert(zkt_record_decode(raw,49,&record) && !memcmp(&record,&prefix,sizeof(record)));
    for(unsigned size=41;size<=64;++size)
        if(size!=49) assert(!zkt_record_decode(raw,size,&record) && !zkt_record_identity_missing(raw,size));
    memset(raw+2,' ',24);
    assert(!zkt_record_decode(raw,49,&record) && zkt_record_identity_missing(raw,49));
    memset(raw+2,0,24);
    assert(!zkt_record_decode(raw,49,&record) && zkt_record_identity_missing(raw,49));
    raw[2]=1;
    assert(!zkt_record_decode(raw,49,&record) && !zkt_record_identity_missing(raw,49));
    return 0;
}
''')
    executable = tmp_path / "records49"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra",
                    "-Werror", "-fsanitize=address,undefined", "-I", str(main),
                    str(unit), str(main / "zkt_record.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
