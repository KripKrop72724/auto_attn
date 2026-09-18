#include "hikvision_clock.h"
#include <stdio.h>
#include <string.h>
#include <ctype.h>
bool hik_clock_parse(const char *s, int64_t *epoch)
{
    if (!s || !epoch) return false;
    size_t n = strlen(s);
    if (n != 20 && n != 25) return false;
    if (s[4]!='-' || s[7]!='-' || s[10]!='T' || s[13]!=':' || s[16]!=':') return false;
    for (unsigned i=0;i<19;i++) {
        if (i==4 || i==7 || i==10 || i==13 || i==16) continue;
        if (!isdigit((unsigned char)s[i])) return false;
    }
    int y,m,d,h,mi,se,oh=0,om=0;
    if (sscanf(s,"%4d-%2d-%2dT%2d:%2d:%2d",&y,&m,&d,&h,&mi,&se)!=6) return false;
    if (n==20) { if (s[19]!='Z') return false; }
    else {
        if ((s[19]!='+' && s[19]!='-') || s[22]!=':' ||
            !isdigit((unsigned char)s[20]) || !isdigit((unsigned char)s[21]) ||
            !isdigit((unsigned char)s[23]) || !isdigit((unsigned char)s[24]) ||
            sscanf(s+20,"%2d:%2d",&oh,&om)!=2 || oh>14 || om>59 || (oh==14 && om)) return false;
    }
    static const int md[]={31,28,31,30,31,30,31,31,30,31,30,31};
    bool leap=y%4==0 && (y%100!=0 || y%400==0);
    if(y<2020 || y>2099 || m<1 || m>12 || d<1 || d>md[m-1]+(m==2 && leap) || h>23 || mi>59 || se>59) return false;
    int64_t days=0;
    for(int year=1970;year<y;year++) days+=365+(year%4==0 && (year%100!=0 || year%400==0));
    for(int month=1;month<m;month++) days+=md[month-1]+(month==2 && leap);
    int offset=(oh*60+om)*60*(s[19]=='-'?-1:1);
    *epoch=(days+d-1)*86400+h*3600+mi*60+se-offset;
    return true;
}
