#include "zkt_catalog_client.h"
#include <errno.h>
#include <string.h>

static void collected(zc_client_t *client)
{
    if (client->pending_operation == ZC_ACTIVATE || client->pending_operation == ZC_RECOVER)
        client->active_may_have_changed = true;
    client->pending_ticket = 0;
    memset(&client->scratch, 0, sizeof(client->scratch));
}
bool zc_client_drain(zc_client_t *client, zc_client_port_t port)
{
    if (!client || !port.poll) return false;
    if (!client->pending_ticket) return true;
    bool complete = false;
    if (!port.poll(port.context, client->pending_ticket, &client->scratch.reply, &complete) || !complete) return false;
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
    memset(&client->scratch, 0, sizeof(client->scratch));
    client->scratch.request.operation = client->commands ? ZJ_COMMANDS : ZJ_CATALOG;
    client->scratch.request.input.catalog = *request;
    if (!port.submit(port.context, &client->scratch.request, &client->pending_ticket)) {
        memset(&client->scratch, 0, sizeof(client->scratch));
        reply->error = EBUSY;
        reply->operation = client->commands ? "commands_owner_admission" : "catalog_owner_admission";
        return ZJ_IO;
    }
    client->pending_operation = request->operation;
    for (;;) {
        bool complete = false;
        if (port.poll(port.context, client->pending_ticket, &client->scratch.reply, &complete) && complete) {
            *reply = client->scratch.reply.catalog;
            zj_result_t result = client->scratch.reply.result;
            collected(client);
            return result;
        }
        if (port.now_us(port.context) >= request->deadline_us) {
            reply->error = ETIMEDOUT;
            reply->operation = client->commands ? "commands_owner_wait" : "catalog_owner_wait";
            return ZJ_UNCERTAIN;
        }
        port.wait(port.context);
    }
}
