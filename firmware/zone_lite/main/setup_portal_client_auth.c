#include "setup_portal_client_auth.h"

#include <string.h>

void setup_portal_client_auth_reset(setup_portal_client_auth_t *state)
{
    memset(state, 0, sizeof(*state));
}

void setup_portal_client_auth_connected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    bool currently_associated)
{
    // A queued connect callback can run after the station has already left.
    // Only the driver's current association snapshot may establish state.
    if (!currently_associated) return;

    // DHCP can be delivered before the queued association callback. Preserve
    // the already-authorized lease when that delayed callback names the same
    // physical client; a different client always invalidates the old state.
    if (state->associated && memcmp(state->mac, mac, sizeof(state->mac)) == 0) {
        return;
    }
    setup_portal_client_auth_reset(state);
    memcpy(state->mac, mac, sizeof(state->mac));
    state->associated = true;
}

bool setup_portal_client_auth_ip_assigned(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    uint32_t ip_addr,
    bool currently_associated)
{
    if (!currently_associated || ip_addr == 0) return false;

    // The driver association snapshot is authoritative when the queued Wi-Fi
    // callback has not arrived yet. It also prevents a delayed DHCP callback
    // from resurrecting a client after that station has disconnected.
    if (!state->associated || memcmp(state->mac, mac, sizeof(state->mac)) != 0) {
        setup_portal_client_auth_reset(state);
        memcpy(state->mac, mac, sizeof(state->mac));
        state->associated = true;
    }
    state->ip_addr = ip_addr;
    state->ip_assigned = true;
    return true;
}

bool setup_portal_client_auth_disconnected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    bool currently_associated)
{
    // A station may have reconnected before its queued disconnect callback
    // runs. In that case the current driver snapshot wins over the stale event.
    if (currently_associated) return false;
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
