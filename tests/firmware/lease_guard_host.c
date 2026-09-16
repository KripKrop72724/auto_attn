#include "lease_guard.h"
#include <assert.h>
#include <stdio.h>

typedef struct {int64_t now,deadline;unsigned writes,fail_write;bool active,uncertain,elevate_fail,verify_fail,revoke_fail;int privilege;unsigned mutations;} state_t;
static int64_t now(void *p){return ((state_t *)p)->now;}
static bool persist(void *arg,uint16_t uid,int64_t deadline,bool active){
 state_t *s=arg;assert(uid==7);s->writes++;
 if(s->writes==s->fail_write && !s->uncertain)return false;
 s->active=active;s->deadline=deadline;return s->writes!=s->fail_write;
}
static bool elevate(void *arg,uint16_t uid){state_t *s=arg;assert(uid==7 && s->active && s->deadline>s->now);s->privilege=14;s->mutations++;s->now+=2;return !s->elevate_fail;}
static bool verify(void *arg,uint16_t uid,int privilege){state_t *s=arg;assert(uid==7);return !(s->verify_fail && privilege==14) && s->privilege==privilege;}
static bool revoke(void *arg,uint16_t uid){state_t *s=arg;assert(uid==7);if(s->revoke_fail)return false;s->privilege=0;return true;}
int main(void){
 state_t s={.now=1800000000};lg_port_t p={now,persist,elevate,verify,revoke,&s};
 assert(lg_grant(p,7,600,0)==LG_OK);assert(s.deadline==1800000602 && s.privilege==14 && s.active);
 for(unsigned boundary=1;boundary<=2;boundary++)for(unsigned uncertain=0;uncertain<2;uncertain++){
  s=(state_t){.now=1800000000,.fail_write=boundary,.uncertain=uncertain};
  assert(lg_grant(p,7,600,0)==LG_STORAGE);assert(s.privilege==0);
  if(boundary==1)assert(!s.mutations);else assert(!s.active);
 }
 s=(state_t){.now=1800000000,.verify_fail=true,.revoke_fail=true};
 assert(lg_grant(p,7,600,0)==LG_TERMINAL);assert(s.active && s.deadline==1800000600 && s.privilege==14);
 s=(state_t){.now=1800000000,.elevate_fail=true};
 assert(lg_grant(p,7,600,0)==LG_TERMINAL);assert(!s.active && !s.privilege);
 s=(state_t){.now=1800000000,.verify_fail=true,.fail_write=2};
 assert(lg_grant(p,7,600,0)==LG_TERMINAL);assert(s.active && !s.privilege);
 s=(state_t){.now=1800000000};assert(lg_grant(p,7,600,1800000001)==LG_EXPIRED);assert(!s.active && !s.privilege);
 s=(state_t){.now=1};assert(lg_grant(p,7,600,0)==LG_TIME && !s.writes && !s.mutations);
 s=(state_t){.now=1800000000};assert(lg_grant(p,7,600,1800000601)==LG_TIME && !s.writes);
 puts("lease guard regression tests passed");
}
