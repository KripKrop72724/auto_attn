#pragma once
#include <stdbool.h>
#include <stdint.h>
/* Strict offset-bearing ISO time; never guess a terminal timezone. */
bool hik_clock_parse(const char *text, int64_t *epoch);
