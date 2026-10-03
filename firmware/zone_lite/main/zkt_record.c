#include "zkt_record.h"
#include <stdio.h>
#include <string.h>

static uint32_t le32(const uint8_t *p)
{ return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }

const char *zkt_model_profile(const char *model)
{
    static const struct { const char *model, *profile; } models[] = {
        {"G3", "zkt-g3-v1"}, {"SilkBio-101TC/ID", "zkt-silkbio-101tc-id-v1"},
        {"MB40-VL/ID", "zkt-mb40-vl-id-v1"}, {"uFace800", "zkt-uface800-v1"},
        {"uFace800/ID", "zkt-uface800-id-v1"}, {"uFace800 Plus/ID", "zkt-uface800-plus-id-v1"},
    };
    if (model) for (size_t i=0; i<sizeof(models)/sizeof(*models); ++i)
        if (!strcmp(model, models[i].model)) return models[i].profile;
    return NULL; /* A model name is a profile selector, not qualification proof. */
}

bool zkt_record_decode(const uint8_t *raw, size_t length, zkt_record_t *out)
{
    if (!raw || !out || (length != 8 && length != 16 && length != 40)) return false;
    memset(out, 0, sizeof(*out));
    if (length == 16) {
        snprintf(out->user_id, sizeof(out->user_id), "%lu", (unsigned long)le32(raw));
        out->encoded_time=le32(raw+4); out->status=raw[8]; out->punch=raw[9];
    } else {
        out->attendance_uid=(uint16_t)((uint16_t)raw[0] | (uint16_t)raw[1] << 8);
        if (length == 8) {
            out->encoded_time=le32(raw+3); out->status=raw[2]; out->punch=raw[7];
        } else {
            size_t n=0;
            while (n<24 && raw[2+n]) {
                if (raw[2+n]<32 || raw[2+n]>126) return false;
                out->user_id[n]=(char)raw[2+n]; ++n;
            }
            while (n && out->user_id[n-1]==' ') out->user_id[--n]=0;
            out->encoded_time=le32(raw+27); out->status=raw[26]; out->punch=raw[31];
        }
    }
    return true;
}

uint32_t zkt_record_size(uint32_t bytes, uint32_t count,
                         const uint32_t *sizes, size_t size_count)
{
    if (!bytes || !sizes) return 0;
    uint32_t selected=0;
    for (size_t i=0; i<size_count; ++i) {
        uint32_t size=sizes[i];
        if (!size || bytes%size || (count && bytes/size != count)) continue;
        if (selected && selected!=size) return 0;
        selected=size;
    }
    return selected;
}

bool zkt_record_range(uint32_t prepared_bytes, uint32_t size,
                       uint32_t first, uint32_t end, uint32_t *offset, uint32_t *length)
{
    if (!offset || !length || prepared_bytes<4 || !size || end<=first ||
        (prepared_bytes-4)%size || end>(prepared_bytes-4)/size) return false;
    *offset=4+first*size; *length=(end-first)*size;
    return true;
}
