#include "hikvision_http.h"
#include "zone_config.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
size_t hik_test_strlcpy(char *dst,const char *src,size_t cap){size_t n=strlen(src);if(cap){size_t copy=n<cap-1?n:cap-1;memcpy(dst,src,copy);dst[copy]=0;}return n;}
struct client {esp_http_client_config_t cfg; unsigned attempt; char cache[20000]; size_t bytes;};
static unsigned challenges, auth_calls, closes;
static bool basic, oversized, incomplete;
static zone_config_t config={.provisioned=true,.hik_port=80,.hik_http_digest_allowed=true,
 .hik_host="192.0.2.1",.hik_username="test",.hik_password="test",.hik_expected_serial="test",
 .firmware_family="hikvision"};
const zone_config_t *zone_config_get(void){return &config;}
int64_t esp_timer_get_time(void){return 0;}
int esp_crt_bundle_attach(void *x){(void)x;return 0;}
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *cfg){
 struct client*c=calloc(1,sizeof(*c));c->cfg=*cfg;return c;}
int esp_http_client_set_header(esp_http_client_handle_t c,const char*k,const char*v){(void)c;(void)k;(void)v;return 0;}
int esp_http_client_open(esp_http_client_handle_t c,int n){(void)n;c->attempt++;return 0;}
int esp_http_client_write(esp_http_client_handle_t c,const char*b,int n){(void)c;(void)b;return n;}
int esp_http_client_fetch_headers(esp_http_client_handle_t c){
 bool challenge=c->attempt<=challenges;
 esp_http_client_event_t e={.event_id=HTTP_EVENT_ON_HEADER,.user_data=c->cfg.user_data,
 .header_key=challenge?"WWW-Authenticate":"Content-Type",
 .header_value=challenge?(basic?"Basic realm=\"test\"":"Digest realm=\"test\""):"application/json"};
 c->cfg.event_handler(&e);
 const char *body=challenge?"<ResponseStatus>Unauthorized</ResponseStatus>":"{\"AcsEvent\":{\"numOfMatches\":0}}";
 size_t n=challenge&&oversized?18000:strlen(body);
 if(challenge&&oversized)memset(c->cache+c->bytes,'x',n);else memcpy(c->cache+c->bytes,body,n);
 c->bytes+=n;return (int)n;
}
int esp_http_client_get_status_code(esp_http_client_handle_t c){return c->attempt<=challenges?401:200;}
int esp_http_client_add_auth(esp_http_client_handle_t c){(void)c;auth_calls++;return 0;}
int esp_http_client_read(esp_http_client_handle_t c,char*b,int n){
 if((size_t)n>c->bytes)n=(int)c->bytes;memcpy(b,c->cache,n);memmove(c->cache,c->cache+n,c->bytes-n);c->bytes-=n;return n;}
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t c){(void)c;return !incomplete;}
int esp_http_client_close(esp_http_client_handle_t c){
 /* IDF close retains cached body; retry must consume it, even when the
    complete-data flag was already true after fetch_headers. */
 assert(c->bytes==0);closes++;return 0;}
int esp_http_client_cleanup(esp_http_client_handle_t c){free(c);return 0;}
static hik_result_t request(void){char response[256];size_t n;return hik_http_request(HTTP_METHOD_POST,
 "/ISAPI/AccessControl/AcsEvent?format=json","{}",response,sizeof(response),&n);}
int main(void){
 for(challenges=1;challenges<=2;challenges++){
  auth_calls=closes=0;char response[256];size_t n;
  assert(hik_http_request(HTTP_METHOD_POST,"/ISAPI/AccessControl/AcsEvent?format=json","{}",response,sizeof(response),&n)==HIK_OK);
  assert(!strcmp(response,"{\"AcsEvent\":{\"numOfMatches\":0}}"));assert(auth_calls==challenges&&closes==challenges);
 }
 challenges=1;basic=true;auth_calls=0;assert(request()==HIK_AUTH&&auth_calls==0);basic=false;
 oversized=true;assert(request()==HIK_NETWORK);oversized=false;
 incomplete=true;assert(request()==HIK_NETWORK);return 0;
}
