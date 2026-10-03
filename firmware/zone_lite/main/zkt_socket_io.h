#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ZK_IO_MAX_BYTES 65536U
#define ZK_IO_MAX_WAIT_US 90000000LL
typedef int64_t (*zk_io_clock_t)(void);

/* One absolute monotonic deadline covers every short read/write, interrupted
 * wait and readiness race. The socket remains in its original blocking mode;
 * individual I/O calls use DONTWAIT after select. No allocation or task restart.
 * A partial transfer returns false and must never be parsed/acknowledged. */
bool zk_io_read_until(int socket, void *bytes, size_t length, int64_t deadline_us, zk_io_clock_t clock);
bool zk_io_write_until(int socket, const void *bytes, size_t length, int64_t deadline_us, zk_io_clock_t clock);
