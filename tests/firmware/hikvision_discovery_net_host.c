#include "hikvision_discovery.h"
#include "hikvision_http.h"
#include "zone_config.h"
#include "add_connector.h"
#include "esp_netif.h"
#include "lwip/sockets.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static int64_t now;
static unsigned sockets,closed,auth,saves,logs,received;
static int scenario;
static char uuid[40];
static zone_config_t cfg={.provisioned=true,.firmware_family="hikvision",.hik_host="192.168.1.200",.hik_expected_serial="G04600476"};
const zone_config_t *zone_config_get(void){return &cfg;}
void zone_config_copy_hik_host(char *out,size_t size){snprintf(out,size,"%s",cfg.hik_host);}
esp_err_t zone_config_save_hik_host(const char *ip){saves++;assert(!strcmp(ip,"192.168.1.201"));if(scenario==7)return -1;snprintf(cfg.hik_host,sizeof(cfg.hik_host),"%s",ip);return 0;}
bool add_connector_log(const char *level,const char *subsystem,const char *code,const char *message){
    (void)level;assert(!strcmp(subsystem,"hikvision"));assert(!strstr(message,"password"));
    assert(!strcmp(code,scenario==7?"HIK_IP_SAVE_PENDING":"HIK_IP_REDISCOVERED"));logs++;return true;
}
int64_t esp_timer_get_time(void){now+=100000;return now;}
uint32_t esp_random(void){static uint32_t r=42;return r=r*1664525+1013904223;}
esp_netif_t *esp_netif_get_handle_from_ifkey(const char *key){static int station;assert(!strcmp(key,"WIFI_STA_DEF"));return &station;}
esp_err_t esp_netif_get_ip_info(esp_netif_t *station,esp_netif_ip_info_t *info){assert(station);info->ip.addr=inet_addr("192.168.1.100");info->netmask.addr=inet_addr("255.255.255.0");info->gw.addr=inet_addr("192.168.1.1");return 0;}
int mock_socket(int family,int type,int protocol){assert(family==AF_INET&&type==SOCK_DGRAM&&protocol==IPPROTO_UDP);sockets++;return 77;}
int mock_setsockopt(int fd,int level,int option,const void *value,socklen_t size){
    (void)level;(void)size;assert(fd==77&&value);
    if(option==IP_MULTICAST_TTL)assert(*(const unsigned char *)value==1);
    return scenario==8?-1:0;
}
int mock_bind(int fd,const struct sockaddr *address,socklen_t length){(void)length;assert(fd==77&&((const struct sockaddr_in *)address)->sin_port==htons(37020));return 0;}
ssize_t mock_sendto(int fd,const void *buffer,size_t size,int flags,const struct sockaddr *address,socklen_t length){
    (void)flags;(void)length;assert(fd==77);
    const struct sockaddr_in *target=(const struct sockaddr_in *)address;
    assert(target->sin_addr.s_addr==inet_addr("239.255.255.250")&&target->sin_port==htons(37020));
    const char *probe=buffer,*start=strstr(probe,"<Uuid>");assert(start&&strstr(probe,"<Types>inquiry</Types>")&&!strstr(probe,"Password"));
    memcpy(uuid,start+6,36);uuid[36]=0;assert(uuid[14]=='4');received=0;return (ssize_t)size;
}
ssize_t mock_recvfrom(int fd,void *buffer,size_t capacity,int flags,struct sockaddr *address,socklen_t *length){
    (void)flags;assert(fd==77&&*length>=sizeof(struct sockaddr_in));if(received++)return -1;
    const char *serial=scenario==1?"OTHER0001":"DS-K1T341AMF20210406G04600476";
    const char *ip=scenario==3?"8.8.8.8":"192.168.1.201";
    struct sockaddr_in *sender=(struct sockaddr_in *)address;sender->sin_family=AF_INET;
    sender->sin_port=htons(scenario==5?1234:37020);
    sender->sin_addr.s_addr=inet_addr(scenario==4?"192.168.1.202":ip);
    int n=snprintf(buffer,capacity,"<ProbeMatch><Uuid>%s</Uuid><DeviceSN>%s</DeviceSN><IPv4Address>%s</IPv4Address></ProbeMatch>",scenario==2?"stale":uuid,serial,ip);
    assert(n>0&&(size_t)n<capacity);return n;
}
int mock_close(int fd){assert(fd==77);closed++;return 0;}
hik_result_t hik_http_verify_identity_at(const char *ip,int timeout){
    assert(closed==sockets);assert(!strcmp(ip,"192.168.1.201")&&timeout==3000);
    assert(!strcmp(cfg.hik_host,"192.168.1.200"));auth++;
    return scenario==6?HIK_BINDING:HIK_OK;
}
static bool run(int mode){scenario=mode;now+=31000000;snprintf(cfg.hik_host,sizeof(cfg.hik_host),"192.168.1.200");return hikvision_discovery_step();}
int main(void){
    for(int mode=1;mode<=5;mode++){assert(!run(mode));assert(auth==0&&saves==0&&logs==0&&closed==sockets);}
    assert(!run(6)&&auth==1&&saves==0&&logs==0&&!strcmp(cfg.hik_host,"192.168.1.200"));
    assert(!run(7)&&auth==2&&saves==1&&logs==1&&!strcmp(cfg.hik_host,"192.168.1.200"));
    assert(!run(8)&&auth==2&&saves==1&&closed==sockets);
    assert(run(0)&&auth==3&&saves==2&&logs==2&&!strcmp(cfg.hik_host,"192.168.1.201"));
    unsigned previous=sockets;assert(!hikvision_discovery_step()&&sockets==previous); /* rate limited */
    cfg.provisioned=false;assert(!run(0)&&sockets==previous);
    assert(closed==sockets);return 0;
}
