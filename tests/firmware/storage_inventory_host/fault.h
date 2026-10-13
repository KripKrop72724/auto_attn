#pragma once
/* Forced into storage_inventory_core.c: the harness decides which bytes the
 * "filesystem" cannot read back. */
#include <stdio.h>
FILE *si_test_fopen(const char *path, const char *mode);
size_t si_test_fread(void *buffer, size_t size, size_t count, FILE *stream);
int si_test_fseek(FILE *stream, long offset, int whence);
int si_test_ferror(FILE *stream);
void si_test_clearerr(FILE *stream);
int si_test_fclose(FILE *stream);
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
