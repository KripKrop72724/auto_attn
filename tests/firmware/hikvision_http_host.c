#include "hikvision_http.h"
#include "zone_config.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
size_t hik_test_strlcpy(char *dst,const char *src,size_t cap){size_t n=strlen(src);if(cap){size_t copy=n<cap-1?n:cap-1;memcpy(dst,src,copy);dst[copy]=0;}return n;}
struct client {esp_http_client_config_t cfg; unsigned attempt; char cache[20000]; size_t bytes; bool stream;};
static unsigned challenges, auth_calls, closes;
static bool basic, oversized, incomplete, retry_transport_failure;
static const char *identity_body;
static char requested_url[300];
static int requested_timeout;
static esp_http_client_method_t requested_method;
static unsigned stream_opens, stream_closes, stream_records;
static bool accept_stream=true;
static bool stream_record(void *context,const char *body,size_t size){assert(context==(void*)42);assert(size==7&&!memcmp(body,"{\"a\":1}",7));stream_records++;return accept_stream;}
static void stream_state(bool open){if(open)stream_opens++;else stream_closes++;}
static zone_config_t config={.provisioned=true,.hik_port=80,.hik_http_digest_allowed=true,
 .hik_host="192.0.2.1",.hik_username="test",.hik_password="test",.hik_expected_serial="test",
 .firmware_family="hikvision"};
const zone_config_t *zone_config_get(void){return &config;}
void zone_config_copy_hik_host(char *host,size_t capacity){hik_test_strlcpy(host,config.hik_host,capacity);}
int64_t esp_timer_get_time(void){return 0;}
int esp_crt_bundle_attach(void *x){(void)x;return 0;}
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *cfg){
 struct client*c=calloc(1,sizeof(*c));c->cfg=*cfg;
 c->stream=strstr(cfg->url,"alertStream")!=NULL;
 hik_test_strlcpy(requested_url,cfg->url,sizeof(requested_url));requested_timeout=cfg->timeout_ms;requested_method=cfg->method;
 assert(!strcmp(cfg->username,config.hik_username)&&!strcmp(cfg->password,config.hik_password));
 assert(cfg->auth_type==HTTP_AUTH_TYPE_DIGEST&&cfg->disable_auto_redirect);return c;}
int esp_http_client_set_header(esp_http_client_handle_t c,const char*k,const char*v){(void)c;(void)k;(void)v;return 0;}
int esp_http_client_open(esp_http_client_handle_t c,int n){(void)n;c->attempt++;return retry_transport_failure&&c->attempt>1?-1:0;}
int esp_http_client_write(esp_http_client_handle_t c,const char*b,int n){(void)c;(void)b;return n;}
int esp_http_client_fetch_headers(esp_http_client_handle_t c){
 bool challenge=c->attempt<=challenges;
 esp_http_client_event_t e={.event_id=HTTP_EVENT_ON_HEADER,.user_data=c->cfg.user_data,
 .header_key=challenge?"WWW-Authenticate":"Content-Type",
 .header_value=challenge?(basic?"Basic realm=\"test\"":"Digest realm=\"test\""):c->stream?"multipart/mixed; boundary=probe":"application/json"};
 c->cfg.event_handler(&e);
 const char *body=challenge?"<ResponseStatus>Unauthorized</ResponseStatus>":c->stream?"--probe\r\nContent-Type: application/json\r\n\r\n{\"a\":1}\r\n--probe--\r\n":identity_body?identity_body:"{\"AcsEvent\":{\"numOfMatches\":0}}";
 size_t n=challenge&&oversized?18000:strlen(body);
 if(challenge&&oversized)memset(c->cache+c->bytes,'x',n);else memcpy(c->cache+c->bytes,body,n);
 c->bytes+=n;return (int)n;
}
int esp_http_client_get_status_code(esp_http_client_handle_t c){return c->attempt<=challenges?401:200;}
int esp_http_client_add_auth(esp_http_client_handle_t c){(void)c;auth_calls++;return 0;}
int esp_http_client_read(esp_http_client_handle_t c,char*b,int n){
 if ((size_t)n > c->bytes) { n = (int)c->bytes; }
 memcpy(b,c->cache,n);memmove(c->cache,c->cache+n,c->bytes-n);c->bytes-=n;return n;}
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
 incomplete=true;assert(request()==HIK_NETWORK);incomplete=false;
 retry_transport_failure=true;assert(request()==HIK_NETWORK&&hik_http_last_status()==0);retry_transport_failure=false;
 identity_body="<DeviceInfo><serialNumber>test</serialNumber></DeviceInfo>";
 assert(hik_http_verify_identity_at("192.168.1.201",3000)==HIK_OK);
 assert(!strcmp(requested_url,"http://192.168.1.201:80/ISAPI/System/deviceInfo"));
 assert(requested_timeout==3000&&requested_method==HTTP_METHOD_GET&&!strcmp(config.hik_host,"192.0.2.1"));
 config.hik_https=true;config.hik_port=443;
 assert(hik_http_verify_identity_at("192.168.1.202",3000)==HIK_OK);
 assert(!strcmp(requested_url,"https://192.168.1.202:443/ISAPI/System/deviceInfo"));
 identity_body="<DeviceInfo><serialNumber>wrong</serialNumber></DeviceInfo>";
 assert(hik_http_verify_identity_at("192.168.1.201",3000)==HIK_BINDING);
 identity_body="<DeviceInfo><serialNumber>test</serialNumber><serialNumber>wrong</serialNumber></DeviceInfo>";
 assert(hik_http_verify_identity_at("192.168.1.201",3000)==HIK_BINDING);
 identity_body="<DeviceInfo><serialNumber>test";
 assert(hik_http_verify_identity_at("192.168.1.201",3000)==HIK_BINDING);
 identity_body="<DeviceInfo><serialNumber>test</serialNumber></DeviceInfo>";challenges=0;
 assert(hik_http_verify_identity_at("192.168.1.201",3000)==HIK_AUTH); /* Public metadata is not authenticated proof. */
 assert(hik_http_verify_identity_at("192.168.1.201/evil",3000)==HIK_CONFIGURATION);
 assert(hik_http_verify_identity_at("192.168.1.201",1)==HIK_CONFIGURATION);
 assert(!strcmp(config.hik_host,"192.0.2.1"));
 challenges=1;retry_transport_failure=true;assert(request()==HIK_NETWORK&&hik_http_last_status()==0);retry_transport_failure=false;
 assert(hik_http_stream_with_state(stream_record,(void*)42,stream_state)==HIK_NETWORK);
 assert(stream_opens==1&&stream_closes==2&&stream_records==1&&hik_http_last_status()==0);
 accept_stream=false;assert(hik_http_stream_with_state(stream_record,(void*)42,stream_state)==HIK_CUSTODY);
 assert(stream_opens==2&&stream_closes==4&&stream_records==2&&hik_http_last_status()==0);
 identity_body="<DeviceInfo><serialNumber>wrong</serialNumber></DeviceInfo>";
 assert(hik_http_stream_with_state(stream_record,(void*)42,stream_state)==HIK_BINDING);
 assert(stream_opens==2&&stream_records==2&&stream_closes==5);return 0;
}
