#include "file_transaction.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
static ft_checkpoint_t saved;
static unsigned fail, calls;
static bool uncertain;
static unsigned fail_file_operation, file_operations;
int ft_test_rename(const char *from,const char *to)
{ if(++file_operations==fail_file_operation){errno=EIO;return -1;}return renameat(AT_FDCWD,from,AT_FDCWD,to); }
int ft_test_remove(const char *path)
{ if(++file_operations==fail_file_operation){errno=EIO;return -1;}return unlink(path); }

static int load(void *arg, ft_checkpoint_t *cp) { (void)arg;*cp=saved;return saved.version?1:0; }
static bool commit(void *arg,const ft_checkpoint_t *cp)
{ (void)arg;++calls;if(calls==fail){if(uncertain)saved=*cp;return false;}saved=*cp;return true; }
static void write_file(const char *path,const char *data)
{ FILE *f=fopen(path,"wb");assert(f);assert(fputs(data,f)>=0);assert(!fflush(f));assert(!fsync(fileno(f)));assert(!fclose(f)); }
static bool content(const char *path,const char *wanted)
{ FILE *f=fopen(path,"rb");if(!f)return false;char buf[128]={0};size_t n=fread(buf,1,sizeof(buf)-1,f);assert(!ferror(f));assert(!fclose(f));return n==strlen(wanted) && !strcmp(buf,wanted); }
int main(int argc,char **argv)
{
    assert(argc==2);(void)argv;ft_port_t port={load,commit,NULL};
    for(unsigned boundary=0;boundary<=3;boundary++) for(unsigned ambiguity=0;ambiguity<=1;ambiguity++) {
        (void)remove("active");(void)remove("stage");(void)remove("backup");
        saved=(ft_checkpoint_t){0};calls=0;fail=boundary;uncertain=ambiguity;
        write_file("active","old\n");write_file("stage","new\n");
        bool replaced=ft_replace("active","stage","backup",64,port);
        assert(replaced == (boundary==0));
        // Every failed commit leaves either the old data or the new data intact.
        assert(content("active","old\n") || content("backup","old\n") || content("active","new\n"));
        fail=0;calls=0;
        assert(ft_recover("active","stage","backup",port));
        if(!saved.generation) assert(content("active","old\n"));
        else assert(content("active","new\n"));
    }
    for(unsigned boundary=1;boundary<=3;boundary++) {
        fail_file_operation=0;(void)remove("active");(void)remove("stage");(void)remove("backup");
        saved=(ft_checkpoint_t){0};calls=fail=0;
        write_file("active","old\n");write_file("stage","new\n");
        file_operations=0;fail_file_operation=boundary;
        assert(!ft_replace("active","stage","backup",64,port));
        assert(content("active","old\n") || content("backup","old\n") || content("active","new\n"));
        fail_file_operation=0;
        assert(ft_recover("active","stage","backup",port));
        assert(content("active","new\n"));
    }
    // Shorter prefixes and an empty replacement must actually replace the old file.
    for(unsigned empty=0;empty<2;empty++) {
        saved=(ft_checkpoint_t){0};calls=fail=0;
        write_file("active","prefix\nsuffix\n");write_file("stage",empty?"":"prefix\n");
        assert(ft_replace("active","stage","backup",64,port));
        assert(content("active",empty?"":"prefix\n"));
    }
    // Legacy ambiguous backups cannot be discarded because an active file exists.
    saved=(ft_checkpoint_t){0};write_file("backup","unaccounted\n");
    assert(!ft_recover("active","stage","backup",port));
    assert(content("backup","unaccounted\n"));
    assert(!remove("active"));assert(ft_recover("active","stage","backup",port));
    assert(content("active","unaccounted\n"));
    assert(!remove("active"));write_file("stage","legacy-only\n");
    assert(!ft_recover("active","stage","backup",port));
    assert(content("stage","legacy-only\n"));
    // Lost/corrupt transaction metadata cannot authorize deleting a generation.
    saved=(ft_checkpoint_t){.version=1,.generation=1,.phase=2,.crc=123};
    assert(!ft_recover("active","stage","backup",port));
    puts("file transaction regression tests passed");
}
