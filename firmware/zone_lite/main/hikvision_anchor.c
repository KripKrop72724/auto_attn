#include "hikvision_anchor.h"
#include "hikvision_clock.h"
#include "cJSON.h"
#include "mbedtls/sha256.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>
static bool number(const cJSON *root,const char *key,uint32_t maximum,uint32_t *out)
{
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(root,key);
    if (!cJSON_IsNumber(v) || v->valuedouble<0 || v->valuedouble>maximum ||
        (double)(uint32_t)v->valuedouble!=v->valuedouble) return false;
    *out=(uint32_t)v->valuedouble;return true;
}
bool hik_anchor_digest(const char *body,size_t length,unsigned char digest[32])
{
    if (!body || !length || length>8192 || memchr(body,0,length)) return false;
    cJSON *row=cJSON_ParseWithLength(body,length);
    uint32_t serial=0,major=0,minor=0;int64_t epoch=0;
    const cJSON *time=cJSON_GetObjectItemCaseSensitive(row,"time");
    const cJSON *employee=cJSON_GetObjectItemCaseSensitive(row,"employeeNoString");
    const char *id=cJSON_IsString(employee)?employee->valuestring:"";
    bool valid=cJSON_IsObject(row) && number(row,"serialNo",3000000000U,&serial) && serial &&
        number(row,"major",65535,&major) && number(row,"minor",65535,&minor) &&
        cJSON_IsString(time) && hik_clock_parse(time->valuestring,&epoch) &&
        (!employee || cJSON_IsNull(employee) || cJSON_IsString(employee)) &&
        strlen(id)<=32 && strspn(id,"0123456789")==strlen(id);
    const char *keys[]={"serialNo","major","minor","time","employeeNoString"};
    for (unsigned i=0;valid && i<5;i++) {
        unsigned count=0;const cJSON *field;
        cJSON_ArrayForEach(field,row) if(field->string && !strcmp(field->string,keys[i])) count++;
        if(count>1)valid=false;
    }
    char facts[160];
    int n=valid?snprintf(facts,sizeof(facts),"HIK_ANCHOR_V2\n%lu\n%lu\n%lu\n%s\n%lld",
        (unsigned long)serial,(unsigned long)major,(unsigned long)minor,id,(long long)epoch):-1;
    if(n<0 || (size_t)n>=sizeof(facts))valid=false;
    if(valid && mbedtls_sha256((const unsigned char *)facts,(size_t)n,digest,0)!=0)valid=false;
    cJSON_Delete(row);return valid;
}
