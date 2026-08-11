#include "setup_portal_client_auth.h"

#include <string.h>

void setup_portal_client_auth_reset(setup_portal_client_auth_t *state)
{
    memset(state, 0, sizeof(*state));
}

static void advance_generation(setup_portal_client_auth_t *state)
{
    uint32_t generation = state->association_generation + 1;
    if (generation == 0) generation = 1;
    setup_portal_client_auth_reset(state);
    state->association_generation = generation;
}

static void begin_association(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    bool connect_event_seen)
{
    advance_generation(state);
    memcpy(state->mac, mac, sizeof(state->mac));
    state->associated = true;
    state->connect_event_seen = connect_event_seen;
}

void setup_portal_client_auth_connected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    bool currently_associated)
{
    // A queued connect callback can run after the station has already left.
    // Only the driver's current association snapshot may establish state.
    if (!currently_associated) return;

    bool same_mac = state->associated &&
                    memcmp(state->mac, mac, sizeof(state->mac)) == 0;
    if (!same_mac || state->connect_event_seen) {
        // Every unpaired connect callback begins a new association generation,
        // including a fast same-MAC reconnect whose disconnect callback has not
        // run yet. Never carry a prior generation's lease across that boundary.
        begin_association(state, mac, true);
        return;
    }

    // DHCP can be delivered before the queued connect callback. In that one
    // case the IP handler already began this generation; pair the callback
    // with it and preserve only the lease tagged with the same generation.
    state->connect_event_seen = true;
    if (state->lease_generation != state->association_generation) {
        state->ip_addr = 0;
        state->ip_assigned = false;
        state->lease_generation = 0;
    }
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
        begin_association(state, mac, false);
    }
    state->ip_addr = ip_addr;
    state->lease_generation = state->association_generation;
    state->ip_assigned = true;
    return true;
}

bool setup_portal_client_auth_disconnected(
    setup_portal_client_auth_t *state,
    const uint8_t mac[6],
    bool currently_associated)
{
    bool same_mac = state->associated &&
                    memcmp(state->mac, mac, sizeof(state->mac)) == 0;
    if (!same_mac && !currently_associated) {
        return false;
    }

    // A disconnect is always a generation boundary. If the driver already
    // shows the same MAC re-associated, represent the new generation as
    // awaiting its connect callback/DHCP lease instead of retaining old IP
    // authorization. Wi-Fi disconnect/connect callbacks share one FIFO event
    // base, so the queued connect event pairs with this pending generation.
    advance_generation(state);
    if (currently_associated) {
        memcpy(state->mac, mac, sizeof(state->mac));
        state->associated = true;
    }
    return true;
}

bool setup_portal_client_auth_allows(
    const setup_portal_client_auth_t *state,
    uint32_t ip_addr)
{
    return state->associated && state->ip_assigned &&
           state->lease_generation == state->association_generation &&
           ip_addr != 0 &&
           state->ip_addr == ip_addr;
}
