#include "setup_portal_client_auth.h"

#include <string.h>

void setup_portal_client_auth_reset(setup_portal_client_auth_t *state)
{
    memset(state, 0, sizeof(*state));
}

void setup_portal_client_auth_connected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6])
{
    setup_portal_client_auth_reset(state);
    memcpy(state->mac, mac, sizeof(state->mac));
    state->associated = true;
}

bool setup_portal_client_auth_ip_assigned(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    uint32_t ip_addr)
{
    if (ip_addr == 0) return false;

    // The DHCP event is authoritative even when the Wi-Fi association event
    // is still queued. If an association is already known, reject an IP event
    // for a different (stale) station instead of replacing the active client.
    if (state->associated && memcmp(state->mac, mac, sizeof(state->mac)) != 0) {
        return false;
    }
    if (!state->associated) {
        memcpy(state->mac, mac, sizeof(state->mac));
        state->associated = true;
    }
    state->ip_addr = ip_addr;
    state->ip_assigned = true;
    return true;
}

bool setup_portal_client_auth_disconnected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6])
{
    if (!state->associated || memcmp(state->mac, mac, sizeof(state->mac)) != 0) {
        return false;
    }
    setup_portal_client_auth_reset(state);
    return true;
}

bool setup_portal_client_auth_allows(
    const setup_portal_client_auth_t *state,
    uint32_t ip_addr)
{
    return state->associated && state->ip_assigned && ip_addr != 0 &&
           state->ip_addr == ip_addr;
}
