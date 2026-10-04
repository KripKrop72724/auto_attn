#include "zkt_catalog_client.h"
#include <errno.h>
#include <string.h>

static void collected(zc_client_t *client)
{
    if (client->pending_operation == ZC_ACTIVATE || client->pending_operation == ZC_RECOVER)
        client->active_may_have_changed = true;
    client->pending_ticket = 0;
}
bool zc_client_drain(zc_client_t *client, zc_client_port_t port)
{
    if (!client || !port.poll) return false;
    if (!client->pending_ticket) return true;
    zj_reply_t reply;
    bool complete = false;
    if (!port.poll(port.context, client->pending_ticket, &reply, &complete) || !complete) return false;
    collected(client);
    return true;
}
zj_result_t zc_client_call(zc_client_t *client, zc_client_port_t port,
                          const zc_request_t *request, zc_reply_t *reply)
{
    if (!reply) return ZJ_INVALID;
    memset(reply, 0, sizeof(*reply));
    if (!client || !port.now_us || !port.wait || !port.submit || !port.poll || !zc_request_valid(request))
        return ZJ_INVALID;
    if (!zc_client_drain(client, port)) {
        reply->error = EBUSY;
        reply->operation = client->commands ? "commands_previous_operation" : "catalog_previous_operation";
        return ZJ_IO;
    }
    if (port.now_us(port.context) >= request->deadline_us) {
        reply->error = ETIMEDOUT;
        reply->operation = client->commands ? "commands_deadline" : "catalog_deadline";
        return ZJ_STALE;
    }
    zj_request_t message = {.operation = client->commands ? ZJ_COMMANDS : ZJ_CATALOG, .input.catalog = *request};
    if (!port.submit(port.context, &message, &client->pending_ticket)) {
        reply->error = EBUSY;
        reply->operation = client->commands ? "commands_owner_admission" : "catalog_owner_admission";
        return ZJ_IO;
    }
    client->pending_operation = request->operation;
    for (;;) {
        zj_reply_t response;
        bool complete = false;
        if (port.poll(port.context, client->pending_ticket, &response, &complete) && complete) {
            *reply = response.catalog;
            collected(client);
            return response.result;
        }
        if (port.now_us(port.context) >= request->deadline_us) {
            reply->error = ETIMEDOUT;
            reply->operation = client->commands ? "commands_owner_wait" : "catalog_owner_wait";
            return ZJ_UNCERTAIN;
        }
        port.wait(port.context);
    }
}
