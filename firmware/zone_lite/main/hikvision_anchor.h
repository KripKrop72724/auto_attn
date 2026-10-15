#pragma once
#include <stdbool.h>
#include <stddef.h>
/* Immutable event facts. Names and picture URLs are presentation metadata. */
bool hik_anchor_digest(const char *body, size_t length, unsigned char digest[32]);
