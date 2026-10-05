/* Read-only host adapter for protected bench fixtures. Production decoders,
 * never this adapter, supply record facts. No terminal/network/database access. */
#include "zkt_record.h"
#include "zkt_clock.h"
#include "reliability.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define MAX_BYTES 65536U
static unsigned char raw[MAX_BYTES];
static char hex[MAX_BYTES * 2U + 1U];

static unsigned le16(const unsigned char *p)
{ return (unsigned)p[0] | (unsigned)p[1] << 8; }

static int digit(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static bool emit(const char *user, int attendance_uid, unsigned encoded,
                 unsigned status, unsigned punch, unsigned offset, unsigned length)
{
    unsigned value = encoded;
    struct tm local = {0};
    local.tm_sec = (int)(value % 60); value /= 60;
    local.tm_min = (int)(value % 60); value /= 60;
    local.tm_hour = (int)(value % 24); value /= 24;
    local.tm_mday = (int)(value % 31) + 1; value /= 31;
    local.tm_mon = (int)(value % 12); value /= 12;
    local.tm_year = (int)value + 100;
    time_t utc; unsigned roundtrip;
    if (!zkt_clock_pst_to_utc(&local, &utc) ||
        !zkt_clock_pack_pst(utc, &roundtrip) || roundtrip != encoded) return false;
    if (!*user) putchar('-');
    for (const unsigned char *p = (const unsigned char *)user; *p; ++p) printf("%02x", *p);
    printf(" %d %u %u %u %u %u %lld\n", attendance_uid, encoded, status, punch,
           offset, length, (long long)utc);
    return true;
}

int main(void)
{
    if (setenv("TZ", "UTC0", 1)) return 2;
    tzset();
    unsigned kind, size, expected_session, length;
    if (scanf("%u %u %u %u %131072s", &kind, &size, &expected_session, &length, hex) != 5 ||
        !length || length > MAX_BYTES || strlen(hex) != 2U * length) return 2;
    for (unsigned i = 0; i < length; ++i) {
        int high = digit(hex[2U*i]), low = digit(hex[2U*i+1U]);
        if (high < 0 || low < 0) return 2;
        raw[i] = (unsigned char)(high * 16 + low);
    }
    if (kind == 0) {
        zkt_record_t record;
        if (length != size || !zkt_record_decode(raw, length, &record) ||
            !emit(record.user_id, size == 16 ? -1 : record.attendance_uid,
                  record.encoded_time, record.status, record.punch, 0, length)) {
            puts("REJECT"); return 0;
        }
    } else if (kind == 1) {
        /* The explicit size comes from the bench case. This does not qualify
         * session establishment, TCP framing or a checksum convention. */
        size_t shape = 0;
        if (length <= 8 || le16(raw) != 500 || !le16(raw+4) ||
            le16(raw+4) != expected_session ||
            !rel_live_frame_size(raw+8, length-8, size, &shape) || shape != size) {
            puts("REJECT"); return 0;
        }
        for (unsigned offset = 8; offset < length; offset += size) {
            rel_live_record_t record;
            if (!rel_parse_live_record(raw+offset, size, &record) ||
                !emit(record.user_id, -1, record.timestamp, record.status,
                      record.punch, offset, size)) {
                puts("REJECT"); return 0;
            }
        }
    } else return 2;
    puts("ACCEPT");
    return 0;
}
