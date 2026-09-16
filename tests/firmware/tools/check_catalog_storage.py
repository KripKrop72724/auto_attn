"""Actual bounded catalog writers with real cJSON and the shared capacity policy."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
firmware = ROOT / "firmware/zone_lite/main"
source = (firmware / "add_connector.c").read_text()
functions = source[source.index("static FILE *create_catalog_stage("):
                   source.index("static void recover_identity_catalog_backup_if_active_missing(")]
program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include "cJSON.h"
#include "storage_budget.h"
#define DQ_MAX_RECORD_BYTES 8192
#define ADD_IDENTITY_CATALOG_MAX_BYTES (2U * 1024U * 1024U)
#define QS_ADMIT_HISTORICAL 1
#define LED_STATUS_LOCAL_FAILURE 1
static size_t used, calls, fail_at;
static storage_budget_t budget;
static bool held;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
#define malloc allocate
static bool qs_local_begin(int policy,size_t bytes){assert(policy==1 && !held);held=storage_budget_admit(&budget,8U*1024U*1024U,used,bytes,SB_HISTORICAL);return held;}
static void qs_local_end(bool ok,int error){(void)ok;(void)error;assert(held);held=false;}
static void led_status_fault(int state){assert(state==1);}
static char *encrypt_storage_json(const char *plain){if(!plain)return NULL;char *out=malloc(strlen(plain)+1);if(out)strcpy(out,plain);return out;}
''' + functions + r'''
int main(void){
 cJSON_Hooks hooks={allocate,free};cJSON_InitHooks(&hooks);
 cJSON *row=cJSON_Parse("{\"user_id\":\"test\"}");assert(row);
 FILE *file=create_catalog_stage("catalog");assert(file);calls=0;
 assert(write_encrypted_json_line(file,row));size_t total=calls;assert(!held && !fclose(file));
 for(size_t i=1;i<=total;i++){
  fail_at=0;file=create_catalog_stage("catalog");assert(file);
  calls=0;fail_at=i;assert(!write_encrypted_json_line(file,row));assert(ftell(file)==0 && !held);
  fail_at=0;assert(write_encrypted_json_line(file,row));assert(!fclose(file));
 }
 used=8U*1024U*1024U*60U/100U+1;assert(!create_catalog_stage("pressure"));
 used=8U*1024U*1024U*58U/100U;assert(!create_catalog_stage("pressure"));
 used=8U*1024U*1024U*54U/100U;file=create_catalog_stage("pressure");assert(file);assert(!fclose(file));
 used=8U*1024U*1024U*70U/100U+1;assert(!create_catalog_stage("pressure"));
 used=0;file=create_catalog_stage("catalog");assert(file);
 assert(!ftruncate(fileno(file),ADD_IDENTITY_CATALOG_MAX_BYTES));
 assert(!write_encrypted_json_line(file,row));assert(ftell(file)==ADD_IDENTITY_CATALOG_MAX_BYTES && !held);
 assert(!fclose(file));cJSON_Delete(row);
 puts("catalog allocation and capacity regressions passed");
}
'''
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    unit = temporary / "catalog.c"
    unit.write_text(program)
    executable = temporary / "catalog-test"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(cjson), "-I", str(firmware),
                    str(unit), str(cjson / "cJSON.c"), str(firmware / "storage_budget.c"), "-lm", "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True)
