"""Drive the real VoiceBridge callback, substituting only its network sinks."""
import asyncio
import json

from app.realtime import VoiceBridge


class CaptureBrowser:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


def capture_bridge(sid, settings, store, manager):
    bridge = VoiceBridge(CaptureBrowser(), sid, settings, store, manager)
    sent = []

    async def send(value):
        sent.append(value)

    bridge.send = send
    # VoiceBridge.run normally installs this listener while its socket is open.
    manager.change_listeners.add(bridge.invalidate_task)
    return bridge, sent


async def revise_callback(bridge, sent, tid, revision, text, call_id="revision-1"):
    event = {"name": "revise_vehicle_task", "call_id": call_id,
             "arguments": json.dumps({"task_id": tid, "task_revision": revision, "text": text})}
    await bridge.handle_tool(event)
    await bridge.handle_tool(event)
    outputs = [m["item"] for m in sent if m.get("item", {}).get("type") == "function_call_output"
               and m["item"]["call_id"] == call_id]
    if len(outputs) != 1:
        raise AssertionError("Voice callback was not deduplicated")
    result = json.loads(outputs[0]["output"])
    if result.get("error"):
        raise AssertionError("Voice revision callback rejected the request")
    return result


async def generated_notification(bridge, sent, timeout=5):
    # Acceptance replies are tested by revise_callback. This checks the separate
    # result path without fabricating a provider audio response.
    bridge.pending_tool_reply = False
    bridge.gate.ready = True
    bridge.gate.model_busy = False
    bridge.gate.last_activity = 0
    job = asyncio.create_task(bridge.deliver_notifications())
    try:
        async with asyncio.timeout(timeout):
            while True:
                for item in sent:
                    payload = item.get("item", {})
                    if payload.get("type") != "message":
                        continue
                    event = json.loads(payload["content"][0]["text"]).get("background_task_event", {})
                    if event.get("kind") == "task.generated":
                        return event
                # Acknowledge nonterminal notices as consumed by the fake sink.
                if bridge.inflight is not None:
                    bridge.notification_response_done = True
                    bridge.gate.model_busy = False
                await asyncio.sleep(.02)
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
