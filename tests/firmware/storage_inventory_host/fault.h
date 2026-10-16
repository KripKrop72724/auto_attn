#pragma once
/* Forced into storage_inventory_core.c: the harness decides which bytes the
 * "filesystem" cannot read back and the size it reports for a file. The scan
 * reads with open/read/lseek, as on the device's VFS.
 *
 * Built with SI_DEVICE_OFF_T, the core also gets the ESP32-S3's widths: newlib
 * there has a 32-bit signed off_t and st_size, the host a 64-bit one. 2.7.4
 * compared st_size with (off_t)UINT32_MAX, which is -1 on the device, and
 * reported every file as 4294967295 bytes; the host build could not see it. */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
int si_test_open(const char *path, int flags, ...);
ssize_t si_test_read(int fd, void *buffer, size_t length);
off_t si_test_lseek(int fd, off_t offset, int whence);
int si_test_close(int fd);
long long si_test_size(const char *path, long long bytes);
#undef open
#undef read
#undef lseek
#undef close
#define open si_test_open
#define read si_test_read
#define lseek si_test_lseek
#define close si_test_close

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
