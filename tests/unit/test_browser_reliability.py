import asyncio

from zk_add.realtime import BROWSER_TOPICS, BrowserEventHub, sse_encode
from zk_add.rejection import rejection_category


def test_every_wire_envelope_invalidates_a_browser_topic():
    async def run():
        hub = BrowserEventHub()
        for wire, topic in BROWSER_TOPICS.items():
            event = await hub.publish(wire, {"connector_id": "test"})
            assert event.event_type == topic
            assert f"event: {topic}\n" in sse_encode(event)
            assert f"id: {event.generation}:{event.event_id}" in sse_encode(event)
    asyncio.run(run())


def test_reconnect_overflow_and_server_restart_require_resynchronization():
    async def run():
        hub = BrowserEventHub(history_size=2)
        first = await hub.publish("heartbeat", {})
        stream = hub.subscribe(f"{first.generation}:{first.event_id}")
        assert (await anext(stream)).event_type == "resync"
        for _ in range(501):
            await hub.publish("heartbeat", {})
        assert (await anext(stream)).event_type == "resync"
        assert (await anext(stream)).event_type == "device"
        await stream.aclose()
        restarted = BrowserEventHub()
        stream = restarted.subscribe(f"{first.generation}:{first.event_id}")
        reset = await anext(stream)
        assert reset.event_type == "resync"
        assert reset.data["generation"] != first.generation
        await stream.aclose()
    asyncio.run(run())


def test_error_category_never_contains_private_exception_input():
    assert rejection_category(ValueError("private attendance or roster bytes")) == "EVIDENCE_INVALID"
    assert rejection_category(RuntimeError("private queue evidence")) == "INTERNAL_ERROR"
