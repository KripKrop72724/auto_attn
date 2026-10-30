/* Host harness for zkt_secure.c, driven by tools/check_secure_pull.py, which
 * checks every printed value against an independent Python implementation and
 * plays the terminal's side of the handshake. */
#include "zkt_secure.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fixed_random(void *context, unsigned char *output, size_t length)
{
    unsigned *state = context;
    for (size_t i = 0; i < length; ++i) {
        *state = *state * 1103515245u + 12345u;
        output[i] = (unsigned char)(*state >> 16);
    }
    return 0;
}

static void print_hex(const char *name, const uint8_t *data, size_t length)
{
    printf("%s=", name);
    for (size_t i = 0; i < length; ++i) printf("%02x", data[i]);
    printf("\n");
}

static size_t read_hex(const char *name, uint8_t *output, size_t capacity)
{
    static char line[8192];
    size_t prefix = strlen(name);
    assert(fgets(line, sizeof(line), stdin));
    assert(!strncmp(line, name, prefix) && line[prefix] == '=');
    size_t length = 0;
    for (const char *p = line + prefix + 1; p[0] && p[1] && p[0] != '\n'; p += 2) {
        unsigned value = 0;
        assert(sscanf(p, "%2x", &value) == 1 && length < capacity);
        output[length++] = (uint8_t)value;
    }
    return length;
}

static void check_framing(void)
{
    uint8_t key[ZKT_SECURE_KEY_BYTES];
    uint8_t zk[1100];
    uint8_t frame[1200];
    for (size_t i = 0; i < sizeof(key); ++i) key[i] = (uint8_t)i;
    for (size_t i = 0; i < sizeof(zk); ++i) zk[i] = (uint8_t)(i * 7 + 3);
    for (size_t length = 8; length <= 1024; length += 13) {
        size_t frame_length = 0;
        size_t offset = 0;
        size_t inner = 0;
        assert(zkt_secure_seal(key, zk, length, frame, sizeof(frame), &frame_length) == ZKT_SECURE_OK);
        assert(frame_length == ZKT_SECURE_TCP_HEADER_BYTES + zkt_secure_sealed_length(length));
        assert(frame[0] == 0x50 && frame[1] == 0x50 && frame[2] == 0x83 && frame[3] == 0x7C);
        assert(zkt_secure_open(key, frame + 8, frame_length - 8, &offset, &inner) == ZKT_SECURE_OK);
        assert(inner == length && !memcmp(frame + 8 + offset, zk, length));
    }
    /* The frame may be built in place: zk already at frame + 8. */
    size_t frame_length = 0;
    size_t offset = 0;
    size_t inner = 0;
    memcpy(frame + 8, zk, 40);
    assert(zkt_secure_seal(key, frame + 8, 40, frame, sizeof(frame), &frame_length) == ZKT_SECURE_OK);
    assert(zkt_secure_open(key, frame + 8, frame_length - 8, &offset, &inner) == ZKT_SECURE_OK);
    assert(inner == 40 && !memcmp(frame + 8 + offset, zk, 40));
    /* Too small a buffer, a short header, a tampered byte, a ragged length. */
    assert(zkt_secure_seal(key, zk, 40, frame, 8 + 48, &frame_length) == ZKT_SECURE_ERR_ARGUMENT);
    assert(zkt_secure_seal(key, zk, 7, frame, sizeof(frame), &frame_length) == ZKT_SECURE_ERR_ARGUMENT);
    assert(zkt_secure_seal(key, zk, 40, frame, sizeof(frame), &frame_length) == ZKT_SECURE_OK);
    frame[8 + 20] ^= 0x01;
    assert(zkt_secure_open(key, frame + 8, frame_length - 8, &offset, &inner) == ZKT_SECURE_ERR_FRAME);
    assert(zkt_secure_open(key, frame + 8, 15, &offset, &inner) == ZKT_SECURE_ERR_FRAME);
    assert(zkt_secure_open(key, frame + 8, 0, &offset, &inner) == ZKT_SECURE_ERR_FRAME);

    uint8_t plain[64];
    size_t plain_length = 0;
    uint8_t type = 0;
    const uint8_t *body = NULL;
    size_t body_length = 0;
    assert(zkt_secure_dmc_build(3, (const uint8_t *)"abc", 3, plain, sizeof(plain), &plain_length));
    assert(plain_length == 12);
    assert(zkt_secure_dmc_parse(plain, plain_length, &type, &body, &body_length));
    assert(type == 3 && body_length == 3 && !memcmp(body, "abc", 3));
    plain[6] ^= 1;
    assert(!zkt_secure_dmc_parse(plain, plain_length, &type, &body, &body_length));
    assert(!zkt_secure_dmc_parse(plain, 8, &type, &body, &body_length));
    assert(!zkt_secure_dmc_build(3, (const uint8_t *)"abc", 3, plain, 11, &plain_length));
    uint8_t long_buffer[4096] = {0};
    assert(!zkt_secure_keystream_apply(long_buffer, zkt_secure_keystream_length() + 1));
}

int main(void)
{
    check_framing();

    uint8_t stream[4096] = {0};
    size_t stream_length = zkt_secure_keystream_length();
    assert(zkt_secure_keystream_apply(stream, stream_length));
    print_hex("keystream", stream, stream_length);

    uint8_t bytes[256];
    for (unsigned i = 0; i < sizeof(bytes); ++i) bytes[i] = (uint8_t)i;
    printf("crc=%08x\n", zkt_secure_crc32(bytes, sizeof(bytes)));

    static const uint32_t secrets[][2] = {
        {1761811200u, 3735928559u}, {1u, 1u}, {4294967295u, 0u}, {0u, 4294967295u}};
    for (size_t i = 0; i < sizeof(secrets) / sizeof(secrets[0]); ++i) {
        uint8_t key[ZKT_SECURE_KEY_BYTES];
        zkt_secure_derive_aes_key(secrets[i][0], secrets[i][1], key);
        print_hex("derive", key, sizeof(key));
    }
    static const uint32_t comm_keys[] = {197911u, 0u, 1979u, 4294967295u};
    for (size_t i = 0; i < sizeof(comm_keys) / sizeof(comm_keys[0]); ++i) {
        uint8_t native[4];
        uint8_t extended[ZKT_SECURE_AUTH_EXT_BYTES];
        zkt_secure_commkey_native(comm_keys[i], (uint16_t)(22761u + i * 9000u), (uint8_t)(0x5Au + i), native);
        print_hex("native", native, sizeof(native));
        assert(zkt_secure_auth_ext_payload(comm_keys[i], extended) == ZKT_SECURE_OK);
        print_hex("auth_ext", extended, sizeof(extended));
    }
    uint8_t key[ZKT_SECURE_KEY_BYTES];
    uint8_t zk[24] = {0xe8, 0x03, 0, 0, 0x11, 0x22, 0x01, 0};
    uint8_t frame[128];
    size_t frame_length = 0;
    for (size_t i = 0; i < sizeof(key); ++i) key[i] = (uint8_t)(0xA0 + i);
    memcpy(zk + 8, "SDKBuild=1\0options", 16);
    assert(zkt_secure_seal(key, zk, sizeof(zk), frame, sizeof(frame), &frame_length) == ZKT_SECURE_OK);
    print_hex("sealed", frame, frame_length);
    fflush(stdout);

    /* The terminal's frames as the checker builds them: AA BB inner magic and
     * a zero CRC, then a malformed one. */
    uint8_t incoming[256];
    size_t incoming_length = read_hex("device_frame", incoming, sizeof(incoming));
    size_t offset = 0;
    size_t inner = 0;
    assert(zkt_secure_open(key, incoming + 8, incoming_length - 8, &offset, &inner) == ZKT_SECURE_OK);
    print_hex("opened", incoming + 8 + offset, inner);
    fflush(stdout);
    incoming_length = read_hex("bad_frame", incoming, sizeof(incoming));
    assert(zkt_secure_open(key, incoming + 8, incoming_length - 8, &offset, &inner) == ZKT_SECURE_ERR_FRAME);

    unsigned seed = 7;
    zkt_secure_identity_t identity;
    assert(zkt_secure_identity_generate(&identity, 2048, fixed_random, &seed) == ZKT_SECURE_OK);
    assert(identity.pem_length == strlen(identity.pem));
    uint8_t message[ZKT_SECURE_MESSAGE_MAX];
    size_t message_length = 0;
    assert(zkt_secure_dmc_exchange_payload(&identity, message, sizeof(message), &message_length) == ZKT_SECURE_OK);
    printf("client_dmc=%.*s\n", (int)message_length, (const char *)message);
    fflush(stdout);

    static uint8_t reply[ZKT_SECURE_MESSAGE_MAX];
    for (int round = 0; round < 2; ++round) {
        /* Round 0: a PKCS#1 terminal key; round 1: SubjectPublicKeyInfo. */
        size_t reply_length = read_hex("device_reply", reply, sizeof(reply));
        assert(zkt_secure_key_exchange_payload(reply, reply_length, 0x01020304u + (uint32_t)round, fixed_random,
                                               &seed, message, sizeof(message), &message_length) == ZKT_SECURE_OK);
        printf("key_exchange=%.*s\n", (int)message_length, (const char *)message);
        fflush(stdout);
        reply_length = read_hex("server_reply", reply, sizeof(reply));
        uint32_t server_secret = 0;
        assert(zkt_secure_server_secret(&identity, reply, reply_length, fixed_random, &seed, &server_secret) ==
               ZKT_SECURE_OK);
        printf("server_secret=%u\n", server_secret);
        fflush(stdout);
    }
    size_t reply_length = read_hex("garbage_reply", reply, sizeof(reply));
    assert(zkt_secure_key_exchange_payload(reply, reply_length, 1, fixed_random, &seed, message, sizeof(message),
                                           &message_length) != ZKT_SECURE_OK);
    assert(zkt_secure_server_secret(&identity, reply, reply_length, fixed_random, &seed, &(uint32_t){0}) !=
           ZKT_SECURE_OK);
    zkt_secure_identity_free(&identity);
    printf("done\n");
    return 0;
}
