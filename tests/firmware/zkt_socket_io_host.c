#include "zkt_socket_io.h"
#include <assert.h>
#include <errno.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>

static int64_t now, step = 1000, expected_deadline;
static unsigned fault, waits, reads, writes;
static size_t progress, fragment = 2;
static uint8_t payload[ZK_IO_MAX_BYTES], captured[ZK_IO_MAX_BYTES];
static int64_t clock_us(void) { return now; }
int select(int nfds, fd_set *read_set, fd_set *write_set, fd_set *errors, struct timeval *timeout)
{
    assert(nfds == 4 && !errors && (read_set != NULL) != (write_set != NULL));
    assert(FD_ISSET(3, read_set ? read_set : write_set));
    assert(timeout->tv_sec * 1000000LL + timeout->tv_usec == expected_deadline - now);
    ++waits;
    now += step;
    if (fault == 1) { errno = EINTR; return -1; }
    if (fault == 5) { errno = EBADF; return -1; }
    if (fault == 7) return 0;
    return 1;
}
static ssize_t transfer(void *out, const void *in, size_t length)
{
    if (fault == 2) { errno = EAGAIN; return -1; }
    if (fault == 3) { errno = EINTR; return -1; }
    if (fault == 4 || (fault == 9 && progress)) return 0;
    if (fault == 6) { errno = EPIPE; return -1; }
    size_t take = length < fragment ? length : fragment;
    memcpy(out, in, take);
    progress += take;
    if (fault == 8) now = expected_deadline;
    return (ssize_t)take;
}
ssize_t recv(int socket, void *bytes, size_t length, int flags)
{
    assert(socket == 3 && (flags & MSG_DONTWAIT));
    ++reads;
    return transfer(bytes, payload + progress, length);
}
ssize_t send(int socket, const void *bytes, size_t length, int flags)
{
    assert(socket == 3 && (flags & MSG_DONTWAIT));
#ifdef MSG_NOSIGNAL
    assert(flags & MSG_NOSIGNAL);
#endif
    ++writes;
    return transfer(captured + progress, bytes, length);
}
static void reset(unsigned scenario, int64_t budget)
{
    now = waits = reads = writes = progress = 0;
    step = 1000; fragment = 2; fault = scenario; expected_deadline = budget;
    memset(payload, 77, sizeof(payload)); memset(captured, 0, sizeof(captured));
}
int main(void)
{
    reset(0, 10000);
    assert(zk_io_read_until(3, captured, 10, expected_deadline, clock_us));
    assert(!memcmp(payload, captured, 10) && reads == 5 && waits == 5 && !writes);
    reset(0, 10000);
    assert(zk_io_write_until(3, payload, 10, expected_deadline, clock_us));
    assert(!memcmp(payload, captured, 10) && writes == 5 && !reads);
    reset(0, 1000000); fragment = 777;
    assert(zk_io_read_until(3, captured, sizeof(captured), expected_deadline, clock_us));
    assert(!memcmp(payload, captured, sizeof(captured)));
    for (unsigned scenario = 0; scenario <= 9; ++scenario) {
        reset(scenario, 5000);
        assert(!zk_io_read_until(3, captured, 10, expected_deadline, clock_us));
        assert(waits <= 5 && reads <= 4 && progress < 10);
        if (scenario <= 3 || scenario == 7 || scenario == 8) assert(errno == ETIMEDOUT);
        if (scenario == 4 || scenario == 9) assert(errno == ECONNRESET);
        if (scenario == 5) assert(errno == EBADF);
        if (scenario == 6) assert(errno == EPIPE);
        reset(scenario, 5000);
        assert(!zk_io_write_until(3, payload, 10, expected_deadline, clock_us));
        assert(waits <= 5 && writes <= 4 && progress < 10);
    }
    reset(8, 5000); fragment = 10;
    assert(!zk_io_read_until(3, captured, 10, expected_deadline, clock_us) && errno == ETIMEDOUT);
    reset(0, 10000);
    assert(!zk_io_read_until(-1, captured, 10, expected_deadline, clock_us) && errno == EINVAL);
    assert(!zk_io_read_until(FD_SETSIZE, captured, 10, expected_deadline, clock_us) && errno == EINVAL);
    assert(!zk_io_read_until(3, NULL, 1, expected_deadline, clock_us) && errno == EINVAL);
    assert(!zk_io_read_until(3, captured, SIZE_MAX, expected_deadline, clock_us) && errno == EINVAL);
    assert(!zk_io_read_until(3, captured, 1, expected_deadline, NULL) && errno == EINVAL);
    assert(!zk_io_read_until(3, captured, 1, INT64_MAX, clock_us) && errno == EINVAL);
    assert(!zk_io_read_until(3, captured, 1, 0, clock_us) && errno == ETIMEDOUT);
    assert(zk_io_read_until(3, NULL, 0, expected_deadline, clock_us));
    assert(!waits && !reads && !writes);
    now = INT64_MAX - 10000; expected_deadline = INT64_MAX - 1;
    assert(zk_io_read_until(3, captured, 10, expected_deadline, clock_us));
    return 0;
}
