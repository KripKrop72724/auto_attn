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
