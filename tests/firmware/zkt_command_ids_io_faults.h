/* Load libc's declarations and fortified wrappers BEFORE interposition.
 * Defining -Dfread=zi_fread on the compiler command line can rename glibc's
 * inline wrapper itself: it then calls __fread_chk instead of our injector. */
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

FILE *zi_fopen(const char *, const char *);
size_t zi_fread(void *, size_t, size_t, FILE *);
size_t zi_fwrite(const void *, size_t, size_t, FILE *);
int zi_fflush(FILE *);
int zi_fsync(int);
int zi_fclose(FILE *);
int zi_fstat(int, struct stat *);
int zi_fseek(FILE *, long, int);

#define fopen zi_fopen
#define fread zi_fread
#define fwrite zi_fwrite
#define fflush zi_fflush
#define fsync zi_fsync
#define fclose zi_fclose
#define fstat zi_fstat
#define fseek zi_fseek
