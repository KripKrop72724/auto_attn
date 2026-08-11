#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint8_t mac[6];
    uint32_t ip_addr;
    bool associated;
    bool ip_assigned;
} setup_portal_client_auth_t;

void setup_portal_client_auth_reset(setup_portal_client_auth_t *state);
void setup_portal_client_auth_connected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6]);
bool setup_portal_client_auth_ip_assigned(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    uint32_t ip_addr);
bool setup_portal_client_auth_disconnected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6]);
bool setup_portal_client_auth_allows(
    const setup_portal_client_auth_t *state,
    uint32_t ip_addr);
