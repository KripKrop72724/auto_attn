#pragma once
#include "add_connector.h"
#include "hikvision_api.h"

/* Caller serializes ISAPI access. Success means terminal readback, not HTTP ACK.
 * The ADD command remains durable until its verified result is acknowledged. */
hik_result_t hik_profile_command(const add_command_t *command, cJSON **receipt);
