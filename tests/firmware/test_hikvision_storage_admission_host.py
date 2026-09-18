"""Hikvision source admission cannot activate ZKT's segmented writers."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('hikvision', [0, 1])
def test_source_writer_requires_family_and_validated_storage(tmp_path, hikvision):
    firmware = ROOT / 'firmware/zone_lite/main'
    source = (firmware / 'queue_store.c').read_text()
    body = source[source.index('dq_result_t qs_append_with_policy('):source.index('dq_result_t qs_peek(')]
    harness = r'''
#include "queue_store.h"
#include <assert.h>
#include <errno.h>
#define pdTRUE 1
#define pdMS_TO_TICKS(x) (x)
typedef struct {durable_queue_t queue; int mutex; qs_admission_t admission;} lane_t;
static lane_t lanes[QS_COUNT];
static qs_health_t health;
static int budget_lock=1;
static bool ready,segmented;
static unsigned appended;
static bool storage_upgrade_segmented_writes(void){return segmented;}
bool storage_upgrade_ready(void){return ready;}
static bool lock(qs_lane_t lane){return (unsigned)lane<QS_COUNT;}
static int xSemaphoreTake(int lock,int timeout){(void)lock;(void)timeout;return 1;}
static void xSemaphoreGive(int lock){(void)lock;}
static bool ensure_storage_generation(void){return true;}
static dq_result_t reopen(lane_t *lane){(void)lane;return DQ_OK;}
dq_result_t dq_append(durable_queue_t *q,const void *data,size_t len){(void)q;(void)data;(void)len;++appended;return DQ_OK;}
static void record_queue_result(dq_result_t r,const char *op,bool writing){(void)r;(void)op;(void)writing;}
/* PRODUCTION */
int main(void){
 for(unsigned lane=0;lane<QS_COUNT;lane++)assert(qs_append((qs_lane_t)lane,"x",1)==DQ_IO);
 ready=true;
 for(unsigned lane=0;lane<QS_COUNT;lane++){
  dq_result_t expected=ZONE_LITE_HIKVISION && lane==QS_HIK_SOURCE?DQ_OK:DQ_IO;
  assert(qs_append_with_policy((qs_lane_t)lane,"x",1,QS_ADMIT_LIVE)==expected);
 }
 assert(appended==(ZONE_LITE_HIKVISION?1U:0U));
 assert(health.persistence_verified==(bool)ZONE_LITE_HIKVISION);
 assert(qs_append_with_policy(QS_HIK_SOURCE,"x",1,(qs_admission_t)99)==DQ_IO);
 ready=false;
 assert(qs_append_with_policy(QS_HIK_SOURCE,"x",1,QS_ADMIT_LIVE)==DQ_IO);
 segmented=true;
 assert(qs_append_with_policy(QS_LIVE,"x",1,QS_ADMIT_LIVE)==DQ_OK);
 return 0;
}
'''
    # Exercise the real adapter entry point; this wrapper matches its public default.
    harness = harness.replace('/* PRODUCTION */', body + '\ndq_result_t qs_append(qs_lane_t lane,const void *data,size_t n){return qs_append_with_policy(lane,data,n,QS_ADMIT_LIVE);}\n')
    unit = tmp_path / 'admission.c'
    unit.write_text(harness)
    exe = tmp_path / 'admission'
    subprocess.run([shutil.which('cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', f'-DZONE_LITE_HIKVISION={hikvision}',
                    '-I', str(firmware), str(unit), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
