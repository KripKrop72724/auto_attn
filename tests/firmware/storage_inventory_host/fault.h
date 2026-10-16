#pragma once
/* Forced into storage_inventory_core.c: the harness decides which bytes the
 * "filesystem" cannot read back and the size it reports for a file.
 *
 * Built with SI_DEVICE_OFF_T, the core also gets the ESP32-S3's widths: newlib
 * there has a 32-bit signed off_t and st_size, the host a 64-bit one. 2.7.4
 * compared st_size with (off_t)UINT32_MAX, which is -1 on the device, and
 * reported every file as 4294967295 bytes; the host build could not see it. */
#include <dirent.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
FILE *si_test_fopen(const char *path, const char *mode);
size_t si_test_fread(void *buffer, size_t size, size_t count, FILE *stream);
int si_test_fseek(FILE *stream, long offset, int whence);
int si_test_ferror(FILE *stream);
void si_test_clearerr(FILE *stream);
int si_test_fclose(FILE *stream);
long long si_test_size(const char *path, long long bytes);
#undef fopen
#undef fread
#undef fseek
#undef ferror
#undef clearerr
#undef fclose
#define fopen si_test_fopen
#define fread si_test_fread
#define fseek si_test_fseek
#define ferror si_test_ferror
#define clearerr si_test_clearerr
#define fclose si_test_fclose

#ifdef SI_DEVICE_OFF_T
typedef int32_t si_test_off_t;
#else
typedef off_t si_test_off_t;
#endif
struct si_test_stat {
    mode_t st_mode;
    si_test_off_t st_size;
};
/* As the device's stat fills st_size: the size, held in this build's off_t. */
static inline int si_test_stat(const char *path, struct si_test_stat *status)
{
    struct stat host;
    if (stat(path, &host) != 0) return -1;
    status->st_mode = host.st_mode;
    status->st_size = (si_test_off_t)si_test_size(path, (long long)host.st_size);
    return 0;
}
#define off_t si_test_off_t
#define stat si_test_stat
