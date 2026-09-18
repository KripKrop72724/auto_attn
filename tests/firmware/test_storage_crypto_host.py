"""Fault-test production encrypted storage wrappers, not cryptographic primitives."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_storage_crypto_allocation_and_crypto_failures(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
    wrappers = source[source.index("static char *encrypt_storage_json("):
                      source.index("static int catalog_transaction_load(")]
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static unsigned calls,fail_at,crypto_fail;
static void *allocate(size_t n){if(++calls==fail_at)return NULL;return malloc(n);}
static void *allocate_zero(size_t count,size_t size){if(++calls==fail_at)return NULL;return calloc(count,size);}
static char *duplicate(const char *s){char *p=allocate(strlen(s)+1);if(p)strcpy(p,s);return p;}
#define malloc allocate
#define calloc allocate_zero
#define strdup duplicate
#define MBEDTLS_CIPHER_ID_AES 1
#define MBEDTLS_GCM_ENCRYPT 1
typedef struct {int unused;} mbedtls_gcm_context;
static const char *storage_key_material(void){return "test-key";}
static uint32_t esp_random(void){return 1;}
static int mbedtls_sha256(const unsigned char *in,size_t n,unsigned char *out,int mode){
 assert(in && n && out && !mode);memset(out,0,32);return crypto_fail==1?-1:0;
}
static void mbedtls_gcm_init(mbedtls_gcm_context *c){(void)c;}
static void mbedtls_gcm_free(mbedtls_gcm_context *c){(void)c;}
static int mbedtls_gcm_setkey(mbedtls_gcm_context *c,int type,const unsigned char *key,int bits){
 (void)c;assert(type==1 && key && bits==256);return crypto_fail==2?-1:0;
}
static int mbedtls_gcm_crypt_and_tag(mbedtls_gcm_context *c,int mode,size_t n,const unsigned char *iv,
 size_t ivn,const unsigned char *aad,size_t aadn,const unsigned char *in,unsigned char *out,size_t tn,unsigned char *tag){
 (void)c;assert(mode==1 && iv && ivn==12 && !aad && !aadn && in && out && tn==16 && tag);
 memcpy(out,in,n);memset(tag,0,tn);return crypto_fail==3?-1:0;
}
static int mbedtls_gcm_auth_decrypt(mbedtls_gcm_context *c,size_t n,const unsigned char *iv,
 size_t ivn,const unsigned char *aad,size_t aadn,const unsigned char *tag,size_t tn,const unsigned char *in,unsigned char *out){
 (void)c;assert(iv && ivn==12 && !aad && !aadn && tag && tn==16 && in && out);
 memcpy(out,in,n);return crypto_fail==3?-1:0;
}
static int mbedtls_base64_encode(unsigned char *out,size_t size,size_t *written,const unsigned char *in,size_t n){
 assert(out && size>=n && written && in);memcpy(out,in,n);*written=n;return crypto_fail==4?-1:0;
}
static int mbedtls_base64_decode(unsigned char *out,size_t size,size_t *written,const unsigned char *in,size_t n){
 (void)in;(void)n;assert(out && size>=32 && written);memset(out,0,32);out[0]=1;memcpy(out+29,"abc",3);*written=32;return crypto_fail==4?-1:0;
}
''' + wrappers + r'''
int main(void){
 char *(*functions[])(const char *)={encrypt_storage_json,decrypt_storage_line};
 const char *inputs[]={"abc","AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"};
 for(unsigned f=0;f<2;f++){
  calls=fail_at=crypto_fail=0;char *result=functions[f](inputs[f]);assert(result);free(result);unsigned total=calls;
  for(unsigned i=1;i<=total;i++){
   calls=0;fail_at=i;assert(!functions[f](inputs[f]));
  }
  fail_at=0;
  for(crypto_fail=1;crypto_fail<=4;crypto_fail++)assert(!functions[f](inputs[f]));
 }
 crypto_fail=0;calls=0;fail_at=1;assert(!decrypt_storage_line("{}"));
 fail_at=0;char *plain=decrypt_storage_line("{}");assert(plain && !strcmp(plain,"{}"));free(plain);
 return 0;
}
'''
    unit = tmp_path / "crypto_wrappers.c"
    unit.write_text(program)
    executable = tmp_path / "crypto_wrappers"
    subprocess.run([shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
