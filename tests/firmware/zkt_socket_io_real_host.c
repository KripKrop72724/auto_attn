#include "zkt_socket_io.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static int64_t clock_us(void)
{
    struct timespec now;
    assert(!clock_gettime(CLOCK_MONOTONIC, &now));
    return now.tv_sec * 1000000LL + now.tv_nsec / 1000;
}
static void *late_byte(void *context)
{
    int socket = *(int *)context;
    struct timespec delay = {.tv_nsec = 100000000};
    assert(!nanosleep(&delay, NULL));
    assert(send(socket, "b", 1, 0) == 1);
    return NULL;
}
static void pair(int sockets[2], bool tcp)
{
    if (tcp) {
        int listener = socket(AF_INET, SOCK_STREAM, 0);
        assert(listener >= 0);
        struct sockaddr_in address = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
        assert(!bind(listener, (struct sockaddr *)&address, sizeof(address)));
        socklen_t length = sizeof(address);
        assert(!getsockname(listener, (struct sockaddr *)&address, &length));
        assert(!listen(listener, 1));
        sockets[0] = socket(AF_INET, SOCK_STREAM, 0);
        assert(sockets[0] >= 0 && !connect(sockets[0], (struct sockaddr *)&address, length));
        sockets[1] = accept(listener, NULL, NULL);
        assert(sockets[1] >= 0 && !close(listener));
    } else {
        assert(!socketpair(AF_UNIX, SOCK_STREAM, 0, sockets));
    }
#ifdef __APPLE__
    /* Darwin's send-buffer wait uses SS_NBIO/MSG_NBIO, not MSG_DONTWAIT.
     * Exercise native sockets already nonblocking there. Linux and ESP lwIP
     * honor the per-call flag; the Linux run uses blocking sockets throughout. */
    for (int i = 0; i < 2; ++i) assert(!fcntl(sockets[i], F_SETFL, O_NONBLOCK));
#endif
}
static void exercise(bool tcp)
{
    int sockets[2];
    pair(sockets, tcp);
    int original_flags = fcntl(sockets[0], F_GETFL);
    char output[16] = {0};
    assert(zk_io_write_until(sockets[1], "test", 4, clock_us() + 1000000, clock_us));
    assert(zk_io_read_until(sockets[0], output, 4, clock_us() + 1000000, clock_us));
    assert(!memcmp(output, "test", 4));
    assert(send(sockets[1], "a", 1, 0) == 1);
    pthread_t thread;
    assert(!pthread_create(&thread, NULL, late_byte, &sockets[1]));
    int64_t started = clock_us();
    assert(!zk_io_read_until(sockets[0], output, 2, started + 20000, clock_us));
    assert(errno == ETIMEDOUT && clock_us() - started < 1000000 && output[0] == 'a');
    assert(!pthread_join(thread, NULL));
    assert(zk_io_read_until(sockets[0], output, 1, clock_us() + 1000000, clock_us) && output[0] == 'b');
    assert(fcntl(sockets[0], F_GETFL) == original_flags); /* No global mode change. */
    static char filling[ZK_IO_MAX_BYTES];
    int64_t fill_deadline = clock_us() + 2000000;
    while (zk_io_write_until(sockets[0], filling, sizeof(filling), fill_deadline, clock_us)) {}
    assert(errno == ETIMEDOUT);
    started = clock_us();
    assert(!zk_io_write_until(sockets[0], "x", 1, started + 20000, clock_us));
    assert(errno == ETIMEDOUT && clock_us() - started < 1000000);
    assert(!close(sockets[1]));
    assert(!zk_io_read_until(sockets[0], output, 1, clock_us() + 1000000, clock_us));
    assert(errno == ECONNRESET);
    assert(!close(sockets[0]));
}
int main(void)
{
    exercise(false);
    exercise(true);
    return 0;
}
