#pragma once
/* Forced into the firmware's legacy_queue.c, file_transaction.c and
 * reliability.c for the SPIFFS repair harness: their stdio calls reach the
 * harness's mounted ESP-IDF SPIFFS instead of the host filesystem, so the
 * real drains, transaction recovery and append check run on the repaired
 * image. A path is the SPIFFS object name ("/pending.jsonl"). */
#include <errno.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

typedef struct shim_file shim_file_t;
shim_file_t *shim_fopen(const char *path, const char *mode);
int shim_fclose(shim_file_t *file);
size_t shim_fread(void *buffer, size_t size, size_t count, shim_file_t *file);
size_t shim_fwrite(const void *buffer, size_t size, size_t count, shim_file_t *file);
int shim_fgetc(shim_file_t *file);
char *shim_fgets(char *buffer, int size, shim_file_t *file);
int shim_fprintf(shim_file_t *file, const char *format, ...);
int shim_fseek(shim_file_t *file, long offset, int whence);
long shim_ftell(shim_file_t *file);
int shim_ferror(shim_file_t *file);
int shim_fflush(shim_file_t *file);
int shim_fileno(shim_file_t *file);
int shim_fsync(int fd);
int shim_fstat(int fd, struct stat *status);
int shim_stat(const char *path, struct stat *status);
int shim_remove(const char *path);
int shim_rename(const char *from, const char *to);

#define FILE shim_file_t
#define fopen shim_fopen
#define fclose shim_fclose
#define fread shim_fread
#define fwrite shim_fwrite
#define fgetc shim_fgetc
#define fgets shim_fgets
#define fprintf shim_fprintf
#define fseek shim_fseek
#define fseeko shim_fseek
#define ftell shim_ftell
#define ferror shim_ferror
#define fflush shim_fflush
#define fileno shim_fileno
#define fsync shim_fsync
#define fstat(fd, status) shim_fstat(fd, status)
#define stat(path, status) shim_stat(path, status)
#define remove shim_remove
#define rename shim_rename
