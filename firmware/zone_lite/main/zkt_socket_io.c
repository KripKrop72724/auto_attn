#include "zkt_socket_io.h"
#include <errno.h>
#include <sys/select.h>
#include <sys/socket.h>

static bool transfer(int socket, void *bytes, size_t length, int64_t deadline, zk_io_clock_t clock, bool sending)
{
    if (!clock || socket < 0 || socket >= FD_SETSIZE || (!bytes && length) || length > ZK_IO_MAX_BYTES) {
        errno = EINVAL;
        return false;
    }
    size_t offset = 0;
    while (offset < length) {
        int64_t now = clock();
        if (now < 0 || deadline <= now) { errno = ETIMEDOUT; return false; }
        int64_t remaining = deadline - now;
        if (remaining > ZK_IO_MAX_WAIT_US) { errno = EINVAL; return false; }
        fd_set ready;
        FD_ZERO(&ready);
        FD_SET(socket, &ready);
        struct timeval timeout = {.tv_sec = remaining / 1000000, .tv_usec = remaining % 1000000};
        int selected = select(socket + 1, sending ? NULL : &ready, sending ? &ready : NULL, NULL, &timeout);
        if (selected < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (!selected || clock() >= deadline) { errno = ETIMEDOUT; return false; }
        if (!FD_ISSET(socket, &ready)) continue;
        int flags = MSG_DONTWAIT;
#ifdef MSG_NOSIGNAL
        if (sending) flags |= MSG_NOSIGNAL;
#endif
        ssize_t count = sending ? send(socket, (const uint8_t *)bytes + offset, length - offset, flags) :
            recv(socket, (uint8_t *)bytes + offset, length - offset, flags);
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            return false;
        }
        if (!count) { errno = ECONNRESET; return false; }
        offset += (size_t)count;
    }
    if (clock() >= deadline) { errno = ETIMEDOUT; return false; }
    return true;
}
bool zk_io_read_until(int socket, void *bytes, size_t length, int64_t deadline, zk_io_clock_t clock)
{ return transfer(socket, bytes, length, deadline, clock, false); }
bool zk_io_write_until(int socket, const void *bytes, size_t length, int64_t deadline, zk_io_clock_t clock)
{ return transfer(socket, (void *)bytes, length, deadline, clock, true); }
