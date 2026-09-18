#include "hikvision_commands.h"
#include "zone_config.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static zone_config_t config = {.hik_expected_serial="TEST-SERIAL", .hik_profile="ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1"};
const zone_config_t *zone_config_get(void){return &config;}
static cJSON *person;
static unsigned writes, sequence;
static bool timeout_write, ignore_write, delete_pending, corrupt_write;
uint32_t esp_random(void){return ++sequence;}
hik_result_t hik_http_verify_identity(void){return HIK_OK;}
/* Hash stand-in exercises precondition handling, not the pinned crypto library. */
int mbedtls_sha256(const unsigned char *p,size_t n,unsigned char out[32],int mode){
 (void)mode;uint32_t h=2166136261u;
 for(size_t i=0;i<n;i++)h=(h^p[i])*16777619u;
 for(unsigned i=0;i<32;i++){h=(h^i)*16777619u;out[i]=(unsigned char)(h>>24);}
 return 0;
}
static void hash(const char *p,char out[65]){
 unsigned char h[32];mbedtls_sha256((const unsigned char *)p,strlen(p),h,0);
 for(unsigned i=0;i<32;i++)snprintf(out+2*i,3,"%02x",h[i]);
}
static void field(cJSON *o,const char *k,cJSON *v){
 if(cJSON_HasObjectItem(o,k))assert(cJSON_ReplaceItemInObjectCaseSensitive(o,k,v));
 else assert(cJSON_AddItemToObject(o,k,v));
}
hik_result_t hik_http_request(esp_http_client_method_t method,const char *path,
 const char *body,char *response,size_t capacity,size_t *length){
 cJSON *req=cJSON_Parse(body),*out=cJSON_CreateObject();assert(req && out);
 bool search=strstr(path,"/Search")!=NULL;
 if(search){
  cJSON *cond=cJSON_GetObjectItem(req,"UserInfoSearchCond");
  const char *employee=cJSON_GetObjectItem(cJSON_GetArrayItem(cJSON_GetObjectItem(cond,"EmployeeNoList"),0),"employeeNo")->valuestring;
  assert(!strcmp(employee,"000123"));
  cJSON *page=cJSON_AddObjectToObject(out,"UserInfoSearch");
  cJSON_AddStringToObject(page,"searchID",cJSON_GetObjectItem(cond,"searchID")->valuestring);
  cJSON_AddNumberToObject(page,"numOfMatches",person?1:0);
  cJSON_AddNumberToObject(page,"totalMatches",person?1:0);
  cJSON *rows=cJSON_AddArrayToObject(page,"UserInfo");
  if(person)cJSON_AddItemToArray(rows,cJSON_Duplicate(person,true));
 }else{
  writes++;
  if(!ignore_write){
   if(strstr(path,"/Record")){
    assert(method==HTTP_METHOD_POST && !person);
    person=cJSON_Duplicate(cJSON_GetObjectItem(req,"UserInfo"),true);
    cJSON *validity=cJSON_GetObjectItem(person,"Valid");
    field(validity,"beginTime",cJSON_CreateString("1970-01-01T00:00:00"));
    field(validity,"endTime",cJSON_CreateString("1970-01-01T00:00:00"));
    cJSON_AddBoolToObject(person,"localUIRight",false);
    cJSON_AddNumberToObject(person,"numOfFace",0);cJSON_AddNumberToObject(person,"numOfFP",0);cJSON_AddNumberToObject(person,"numOfCard",0);
   }else if(strstr(path,"/Modify")){
    assert(method==HTTP_METHOD_PUT && person);
    cJSON *u=cJSON_GetObjectItem(req,"UserInfo"),*v;
    cJSON_ArrayForEach(v,u){
     assert(!strcmp(v->string,"employeeNo") || !strcmp(v->string,"name") || !strcmp(v->string,"localUIRight"));
     field(person,v->string,cJSON_Duplicate(v,true));
    }
    if(corrupt_write)field(person,"doorRight",cJSON_CreateString("2"));
   }else if(strstr(path,"/Delete")){
    assert(method==HTTP_METHOD_PUT);
    cJSON *list=cJSON_GetObjectItem(cJSON_GetObjectItem(req,"UserInfoDetail"),"EmployeeNoList");
    assert(cJSON_GetArraySize(list)==1);
    assert(!strcmp(cJSON_GetObjectItem(cJSON_GetArrayItem(list,0),"employeeNo")->valuestring,"000123"));
    if(!delete_pending){cJSON_Delete(person);person=NULL;}
   }else assert(false);
  }
  cJSON_AddNumberToObject(out,"statusCode",1);
 }
 char *raw=cJSON_PrintUnformatted(out);assert(strlen(raw)<capacity);
 strcpy(response,raw);*length=strlen(raw);free(raw);cJSON_Delete(out);cJSON_Delete(req);
 return !search && timeout_write?HIK_NETWORK:HIK_OK;
}
static add_command_t create_command(void){
 add_command_t c={.has_name=true,.has_privilege=true,.privilege=0};
 strcpy(c.command_type,"CREATE_USER");strcpy(c.uid,"000123");strcpy(c.user_id,"000123");
 strcpy(c.expected_serial,"TEST-SERIAL");strcpy(c.name,"Test-1234512345671");return c;
}
static add_command_t existing_command(const char *type){
 add_command_t c=create_command();strcpy(c.command_type,type);
 c.has_expected_name=c.has_expected_privilege=true;
 c.has_expected_terminal_identity_fingerprint=c.has_expected_terminal_state_fingerprint=true;
 strcpy(c.expected_name,cJSON_GetObjectItem(person,"name")->valuestring);
 c.expected_privilege=cJSON_IsTrue(cJSON_GetObjectItem(person,"localUIRight"))?14:0;c.privilege=c.expected_privilege;
 hash("TEST-SERIAL\n000123",c.expected_terminal_identity_fingerprint);
 char *raw=cJSON_PrintUnformatted(person);hash(raw,c.expected_terminal_state_fingerprint);free(raw);return c;
}
int main(void){
 cJSON *receipt=NULL;add_command_t c=create_command();
 ignore_write=true;assert(hik_profile_command(&c,&receipt)!=HIK_OK && !receipt && !person);
 ignore_write=false;timeout_write=true;
 assert(hik_profile_command(&c,&receipt)==HIK_OK && receipt);cJSON_Delete(receipt);
 cJSON *desired=cJSON_Parse("{\"Valid\":{\"enable\":false,\"beginTime\":\"2026-01-01T00:00:00\",\"endTime\":\"2036-01-01T00:00:00\",\"timeType\":\"local\"}}");
 assert(hik_user_matches_created_profile(person,desired));
 cJSON *actual=cJSON_Duplicate(person,true),*v=cJSON_GetObjectItem(actual,"Valid");
 field(v,"enable",cJSON_CreateBool(true));assert(!hik_user_matches_created_profile(actual,desired));
 field(v,"enable",cJSON_CreateBool(false));field(v,"endTime",cJSON_CreateString("1971-01-01T00:00:00"));
 assert(!hik_user_matches_created_profile(actual,desired));cJSON_Delete(actual);cJSON_Delete(desired);
 unsigned prior=writes;assert(hik_profile_command(&c,&receipt)==HIK_OK && writes==prior);cJSON_Delete(receipt);
 field(person,"numOfFace",cJSON_CreateNumber(1));
 assert(hik_profile_command(&c,&receipt)==HIK_BINDING && !receipt && writes==prior);
 c=existing_command("UPDATE_USER");strcpy(c.name,"Renamed-1234512345671");c.privilege=14;
 assert(hik_profile_command(&c,&receipt)==HIK_OK && receipt);cJSON_Delete(receipt);
 assert(cJSON_IsTrue(cJSON_GetObjectItem(person,"localUIRight")));
 assert(cJSON_GetObjectItem(person,"numOfFace")->valueint==1);
 prior=writes;assert(hik_profile_command(&c,&receipt)==HIK_OK && writes==prior);cJSON_Delete(receipt);
 field(person,"doorRight",cJSON_CreateString("2"));
 assert(hik_profile_command(&c,&receipt)==HIK_BINDING && !receipt && writes==prior);
 c=existing_command("UPDATE_USER");c.privilege=0;
 assert(hik_profile_command(&c,&receipt)==HIK_OK);cJSON_Delete(receipt);
 /* Editing a profile preserves every enrolled credential; deletion removes
  * the complete profile regardless of modality or optional count fields. */
 field(person,"numOfFace",cJSON_CreateNumber(2));
 field(person,"numOfFP",cJSON_CreateNumber(2));
 field(person,"numOfCard",cJSON_CreateNumber(1));
 cJSON_AddStringToObject(person,"password","fixture-pin");
 c=existing_command("UPDATE_USER");strcpy(c.name,"Mixed credentials");
 assert(hik_profile_command(&c,&receipt)==HIK_OK);cJSON_Delete(receipt);
 assert(cJSON_GetObjectItem(person,"numOfFace")->valueint==2);
 assert(cJSON_GetObjectItem(person,"numOfFP")->valueint==2);
 assert(cJSON_GetObjectItem(person,"numOfCard")->valueint==1);
 assert(!strcmp(cJSON_GetObjectItem(person,"password")->valuestring,"fixture-pin"));
 c=existing_command("DELETE_USER");prior=writes;
 field(person,"numOfFP",cJSON_CreateNumber(3));
 assert(hik_profile_command(&c,&receipt)==HIK_BINDING && !receipt && writes==prior);
 c=existing_command("DELETE_USER");delete_pending=true;
 assert(hik_profile_command(&c,&receipt)!=HIK_OK && !receipt && person);
 delete_pending=false;assert(hik_profile_command(&c,&receipt)==HIK_OK && !person);cJSON_Delete(receipt);
 prior=writes;assert(hik_profile_command(&c,&receipt)==HIK_OK && writes==prior);cJSON_Delete(receipt);
 const char *credential_types[]={"numOfFP","numOfCard","numOfFace","password","missing_counts"};
 for(unsigned i=0;i<5;i++){
  c=create_command();assert(hik_profile_command(&c,&receipt)==HIK_OK);cJSON_Delete(receipt);
  if(i<3)field(person,credential_types[i],cJSON_CreateNumber(2));
  else if(i==3)cJSON_AddStringToObject(person,"password","fixture-pin");
  else {cJSON_DeleteItemFromObject(person,"numOfFP");cJSON_DeleteItemFromObject(person,"numOfCard");cJSON_DeleteItemFromObject(person,"numOfFace");}
  c=existing_command("DELETE_USER");assert(hik_profile_command(&c,&receipt)==HIK_OK && !person);
  assert(cJSON_IsTrue(cJSON_GetObjectItem(receipt,"user_absent")));cJSON_Delete(receipt);
 }
 c=create_command();assert(hik_profile_command(&c,&receipt)==HIK_OK);cJSON_Delete(receipt);
 c=existing_command("UPDATE_USER");strcpy(c.name,"Changed");corrupt_write=true;
 assert(hik_profile_command(&c,&receipt)!=HIK_OK && !receipt);
 cJSON_Delete(person);
 puts("Verified CRUD, role preservation, stale state, lost ACK and pending deletion passed");return 0;
}
