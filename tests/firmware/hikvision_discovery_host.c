#include "hikvision_discovery.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static const char *uuid="c118402f-abcd-4012-a345-112233445566";
static char xml[HIK_DISCOVERY_PACKET_MAX+2], output[16];
static void packet(const char *id,const char *serial,const char *ip) {
    snprintf(xml,sizeof(xml),"<?xml version=\"1.0\"?><ProbeMatch><Uuid>%s</Uuid><DeviceSN>%s</DeviceSN><IPv4Address>%s</IPv4Address></ProbeMatch>",id,serial,ip);
}
static bool candidate(void) {
    return hik_discovery_candidate(xml,strlen(xml),uuid,"G04600476",0xc0a80164,0xffffff00,0xc0a80101,output);
}
int main(void) {
    uint32_t ip=0;
    assert(hik_discovery_ipv4("192.168.1.201",&ip)&&ip==0xc0a801c9);
    const char *invalid[]={"","1.2.3","1.2.3.4.5","256.1.2.3","01.2.3.4","1.2.3.4 ","-1.2.3.4","1..2.3","1111.2.3.4",NULL};
    for(unsigned i=0;invalid[i];i++)assert(!hik_discovery_ipv4(invalid[i],&ip));
    assert(!hik_discovery_ipv4(NULL,&ip));
    packet(uuid,"G04600476","192.168.1.201");assert(candidate()&&!strcmp(output,"192.168.1.201"));
    packet(uuid,"DS-K1T341AMF20210406G04600476","192.168.1.202");assert(candidate());
    packet("C118402F-ABCD-4012-A345-112233445566","G04600476","192.168.1.202");assert(candidate());
    packet("old-probe","G04600476","192.168.1.201");assert(!candidate());
    packet(uuid,"G04600477","192.168.1.201");assert(!candidate());
    const char *bad_ips[]={"192.168.2.201","8.8.8.8","192.168.1.0","192.168.1.255","192.168.1.100","192.168.1.1","127.0.0.1","224.0.0.1",NULL};
    for(unsigned i=0;bad_ips[i];i++){packet(uuid,"G04600476",bad_ips[i]);assert(!candidate());}
    assert(hik_discovery_local_address(0x0a010203,0x0a010204,0xffffff00,0x0a010201));
    assert(hik_discovery_local_address(0xac1f0203,0xac1f0204,0xffffff00,0xac1f0201));
    assert(!hik_discovery_local_address(0xc0a801c9,0xc0a80164,0xff00ff00,0));
    assert(!hik_discovery_local_address(0xc0a801c9,0xc0a80164,0xfffffffe,0));
    packet(uuid,"G04600476","192.168.1.201</IPv4Address><IPv4Address>192.168.1.202");assert(!candidate());
    packet(uuid,"G04600476</DeviceSN><DeviceSN>G04600476","192.168.1.201");assert(!candidate());
    packet(uuid,"G04600476","192.168.1.201&#32;");assert(!candidate());
    packet(uuid,"G04600476","192.168.1.201");strcat(xml,"<evil/>");assert(!candidate());
    packet(uuid,"G04600476","192.168.1.201");strcat(xml,"<!DOCTYPE evil>");assert(!candidate());
    packet(uuid,"G04600476","192.168.1.201");size_t n=strlen(xml);xml[10]=0;
    assert(!hik_discovery_candidate(xml,n,uuid,"G04600476",0xc0a80164,0xffffff00,0xc0a80101,output));
    packet(uuid,"G04600476","192.168.1.201");n=strlen(xml);xml[n]='x';xml[n+1]=0;
    assert(!hik_discovery_candidate(xml,n,uuid,"G04600476",0xc0a80164,0xffffff00,0xc0a80101,output));
    memset(xml,'x',sizeof(xml));xml[HIK_DISCOVERY_PACKET_MAX+1]=0;
    assert(!hik_discovery_candidate(xml,HIK_DISCOVERY_PACKET_MAX+1,uuid,"G04600476",0,0,0,output));
    /* Truncation and malformed datagrams never read outside the bounded buffer. */
    packet(uuid,"G04600476","192.168.1.201");n=strlen(xml);
    for(size_t i=0;i<n;i++){char saved=xml[i];xml[i]=0;assert(!candidate());xml[i]=saved;}
    unsigned random=1234;
    for(unsigned round=0;round<5000;round++){
        size_t length=round%sizeof(xml);if(length>HIK_DISCOVERY_PACKET_MAX)length=HIK_DISCOVERY_PACKET_MAX;
        for(size_t i=0;i<length;i++){random=random*1664525+1013904223;xml[i]=(char)(1+(random%126));}
        xml[length]=0;(void)hik_discovery_candidate(xml,length,uuid,"G04600476",0xc0a80164,0xffffff00,0xc0a80101,output);
    }
    return 0;
}
