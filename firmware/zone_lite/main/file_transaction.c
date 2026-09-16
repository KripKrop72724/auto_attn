#include "file_transaction.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int exists(const char *path)
{
    struct stat st;
    if (stat(path, &st) == 0) return 1;
    return errno == ENOENT ? 0 : -1;
}
static uint32_t extend(uint32_t crc, const unsigned char *data, size_t length)
{
    crc = ~crc;
    while (length--) {
        crc ^= *data++;
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}
static bool digest(const char *path, uint32_t length, uint32_t *out)
{
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    unsigned char buffer[512];
    uint32_t crc = 0, remaining = length;
    bool ok = true;
    while (remaining) {
        size_t n = remaining < sizeof(buffer) ? remaining : sizeof(buffer);
        if (fread(buffer, 1, n, f) != n) { ok = false; break; }
        crc = extend(crc, buffer, n); remaining -= (uint32_t)n;
    }
    if (ferror(f)) ok = false;
    if (fclose(f) != 0) ok = false;
    if (ok) *out = crc;
    return ok;
}
static bool matches(const char *path, const ft_checkpoint_t *cp, bool exact)
{
    uint32_t crc;
    struct stat st;
    if (exact && (stat(path, &st) != 0 || (uint64_t)st.st_size != cp->length)) return false;
    return digest(path, cp->length, &crc) && crc == cp->digest;
}
static bool save(ft_port_t port, ft_checkpoint_t *cp, uint32_t phase)
{
    if (cp->generation == UINT32_MAX) return false;
    ft_checkpoint_t next = *cp;
    next.version = 1; next.generation++; next.phase = phase;
    next.crc = dq_crc32(&next, offsetof(ft_checkpoint_t, crc));
    if (!port.commit(port.context, &next)) return false;
    *cp = next;
    return true;
}
static bool load(ft_port_t port, ft_checkpoint_t *cp)
{
    if (!port.load || !port.commit) return false;
    memset(cp, 0, sizeof(*cp));
    int found = port.load(port.context, cp);
    return found == 0 || (found == 1 && cp->version == 1 && cp->generation && cp->phase <= 2 &&
        cp->crc == dq_crc32(cp, offsetof(ft_checkpoint_t, crc)));
}
bool ft_recover(const char *active, const char *stage, const char *backup, ft_port_t port)
{
    ft_checkpoint_t cp;
    if (!active || !stage || !backup || !load(port, &cp)) return false;
    int a = exists(active), b = exists(backup);
    if (a < 0 || b < 0) return false;
    if (!cp.phase) {
        // A legacy backup remains recoverable; never retire it on existence alone.
        if (!a && b) return rename(backup, active) == 0;
        return !b && (a || exists(stage) == 0);
    }
    if (cp.phase == 1) {
        if (!matches(active, &cp, true)) {
            if (!matches(stage, &cp, true)) return false;
            if (a && b) return false; // ambiguous generations remain preserved
            if (a && rename(active, backup) != 0) return false;
            if (rename(stage, active) != 0) return false;
        }
        if (!save(port, &cp, 2)) return false;
    }
    if (!matches(active, &cp, true)) return false;
    b = exists(backup);
    if (b < 0 || (b && remove(backup) != 0)) return false;
    int staged = exists(stage);
    if (staged < 0 || (staged && (!matches(stage, &cp, true) || remove(stage) != 0))) return false;
    return save(port, &cp, 0);
}
bool ft_replace(const char *active, const char *stage, const char *backup, size_t limit, ft_port_t port)
{
    if (!ft_recover(active, stage, backup, port)) return false;
    ft_checkpoint_t cp;
    struct stat st;
    if (!load(port, &cp) || stat(stage, &st) != 0 || st.st_size < 0 ||
        (uint64_t)st.st_size > limit || (uint64_t)st.st_size > UINT32_MAX) return false;
    FILE *file = fopen(stage, "r+b");
    if (!file) return false;
    bool durable = fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file) != 0) durable = false;
    if (!durable) return false;
    cp.length = (uint32_t)st.st_size;
    if (!digest(stage, cp.length, &cp.digest) || !save(port, &cp, 1)) return false;
    return ft_recover(active, stage, backup, port);
}
