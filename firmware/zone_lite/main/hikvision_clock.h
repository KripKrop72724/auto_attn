#pragma once
#include <stdbool.h>
#include <stdint.h>
/* Strict offset-bearing ISO time; never guess a terminal timezone.
 * hik_clock_parse is for clock samples and accepts 2020..2099 only. */
bool hik_clock_parse(const char *text, int64_t *epoch);
/* The same syntax for recorded event times, from 1970: a terminal whose clock
 * restarts at 2015 on power-up dates its boot records 2015. */
bool hik_clock_parse_event(const char *text, int64_t *epoch);
