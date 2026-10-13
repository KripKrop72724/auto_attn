#pragma once
/* The registers of ESP-IDF's Xtensa exception frame the hook reads. */
typedef struct {
    long exit, pc, ps, a0, a1;
    long exccause, excvaddr;
} XtExcFrame;
