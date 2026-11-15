import asyncio
import json

import pytest

from app.catalog import Catalog
from app.capability_catalog import CapabilityCatalog
from app.config import Settings
from app.hermes import TaskManager
from app.realtime import VoiceBridge
from app.store import Store


class Browser:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


@pytest.fixture
def bridge_context(tmp_path):
    store = Store(tmp_path / "voice.db")
    sid = store.create_session()
    settings = Settings(db_path=store.path, hermes_key="")
    manager = TaskManager(store, CapabilityCatalog(store), settings)
    bridge = VoiceBridge(Browser(), sid, settings, store, manager)
    sent = []
    async def send(message):
        sent.append(message)
    bridge.send = send
    bridge.gate.ready = True
    return bridge, store, sid, manager, sent


async def delivery_tick(bridge):
    worker = asyncio.create_task(bridge.deliver_notifications())
    try:
        await asyncio.sleep(0.15)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_notifications_wait_then_inject_and_require_playback_completion(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    task, _ = store.create_task(sid, "空调 23 度", "voice-test", "fixture")
    store.finish(task["id"], "generated", "调用结构已通过校验，已模拟下发成功。")
    bridge.gate.user_speaking = True
    await delivery_tick(bridge)
    assert sent == []
    bridge.gate.user_speaking = False
    await delivery_tick(bridge)
    assert [e["type"] for e in sent] == ["conversation.item.create", "response.create"]
    payload = json.loads(sent[0]["item"]["content"][0]["text"])
    assert payload["background_task_event"]["executed"] is False
    assert payload["background_task_event"]["kind"] == "task.generated"
    bridge.gate.model_busy = False
    bridge.notification_response_done = True
    bridge.gate.playback_busy = True
    await delivery_tick(bridge)
    assert store.events(sid, pending=True)
    bridge.gate.playback_busy = False
    await delivery_tick(bridge)
    assert store.events(sid, pending=True) == []
    await manager.close()


async def test_interruption_keeps_notification_pending_and_does_not_cancel_task(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    task, _ = store.create_task(sid, "调整悬架", "question")
    store.ask(task["id"], "请问要升高还是降低？")
    await delivery_tick(bridge)
    bridge.interrupted()
    assert store.task(task["id"])["status"] == "awaiting_input"
    assert store.events(sid, pending=True)
    bridge.gate.model_busy = False
    bridge.gate.last_activity = 0
    await delivery_tick(bridge)
    assert len([m for m in sent if m["type"] == "conversation.item.create"]) == 2
    await manager.close()


async def test_fast_completion_does_not_overtake_acceptance_reply(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    task, _ = store.create_task(sid, "快速预览", "fast-notice")
    store.finish(task["id"], "generated", "已模拟下发成功")
    bridge.pending_tool_reply = True
    await delivery_tick(bridge)
    assert sent == [{"type": "response.create"}]
    assert bridge.inflight is None and store.events(sid, pending=True)
    bridge.gate.model_busy = False
    bridge.gate.last_activity = 0
    await delivery_tick(bridge)
    assert [e["type"] for e in sent] == ["response.create", "conversation.item.create", "response.create"]
    await manager.close()


async def test_s2s_tool_callback_is_idempotent_and_reports_acceptance_only(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    event = {"call_id": "call_test", "name": "create_vehicle_task", "arguments": json.dumps({"goal": "主驾座椅加热二档"})}
    await bridge.handle_tool(event)
    await bridge.handle_tool(event)
    assert len(store.tasks(sid)) == 1
    outputs = [x for x in sent if x.get("item", {}).get("type") == "function_call_output"]
    assert len(outputs) == 1
    result = json.loads(outputs[0]["item"]["output"])
    assert result["status"] == "accepted"
    assert "成功" not in result["message"]
    await manager.close()


async def test_s2s_cannot_cancel_a_different_session(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    other = store.create_session()
    task, _ = store.create_task(other, "车窗开一半", "foreign")
    await bridge.handle_tool({"call_id": "call_foreign", "name": "cancel_vehicle_task", "arguments": json.dumps({"task_id": task["id"]})})
    assert store.task(task["id"])["status"] == "running"
    assert json.loads(sent[0]["item"]["output"])["error"] == "invalid_task_request"
    await manager.close()


def test_audio_url_rejects_unexpected_host_components():
    with pytest.raises(ValueError):
        Settings(workspace="workspace@evil.example", dashscope_key="test").audio_url()
    with pytest.raises(ValueError):
        Settings(workspace="space", region="unknown", dashscope_key="test").audio_url()
    assert "space.cn-beijing.maas.aliyuncs.com" in Settings(workspace="space").audio_url()


def test_audio_shared_endpoint_does_not_require_workspace():
    settings=Settings(workspace="",dashscope_key="test",region="cn-beijing")
    assert settings.audio_ready
    assert settings.audio_url().startswith("wss://dashscope.aliyuncs.com/api-ws/v1/realtime?")
    assert "dashscope-intl.aliyuncs.com" in Settings(workspace="",region="ap-southeast-1").audio_url()


async def test_model_access_denied_is_visible_without_raw_provider_message(bridge_context):
    bridge, store, sid, manager, sent = bridge_context
    async def rejected():
        yield json.dumps({'code':'Model.AccessDenied','message':'private provider detail'})
    bridge.upstream = rejected()
    await bridge.receive_model()
    assert not bridge.gate.ready
    assert bridge.browser.messages[0]['code'] == 'Model.AccessDenied'
    assert '业务空间' in bridge.browser.messages[0]['message']
    assert 'private provider detail' not in str(bridge.browser.messages)
    await manager.close()


async def test_new_voice_connection_does_not_replay_completed_text_tasks(bridge_context):
    bridge,store,sid,manager,_=bridge_context
    task,_=store.create_task(sid,'已结束的文字任务','old-text','fixture')
    store.finish(task['id'],'generated','已模拟下发成功')
    fresh=VoiceBridge(Browser(),sid,Settings(),store,manager)
    sent=[]
    async def send(e): sent.append(e)
    fresh.send=send;fresh.gate.ready=True
    await delivery_tick(fresh)
    assert sent==[]
    assert store.events(sid,pending=True)  # Ignored, not falsely acknowledged as spoken.
    new,_=store.create_task(sid,'新任务','new-text','fixture')
    store.finish(new['id'],'generated','新任务模拟成功')
    await delivery_tick(fresh)
    payload=json.loads(sent[0]['item']['content'][0]['text'])
    assert payload['background_task_event']['task_id']==new['id']
    await manager.close()


async def test_task_already_running_can_finish_during_new_voice_connection(bridge_context):
    _,store,sid,manager,_=bridge_context
    task,_=store.create_task(sid,'连接前仍在执行','pending','fixture')
    fresh=VoiceBridge(Browser(),sid,Settings(),store,manager)
    sent=[]
    async def send(e): sent.append(e)
    fresh.send=send;fresh.gate.ready=True
    store.finish(task['id'],'generated','本次连接之后完成')
    await delivery_tick(fresh)
    assert json.loads(sent[0]['item']['content'][0]['text'])['background_task_event']['task_id']==task['id']
    assert fresh.browser.messages[0]['type']=='voice.process'
    assert fresh.browser.messages[0]['step']['task_id']==task['id']
    await manager.close()


async def test_s2s_progress_records_transcript_and_transport_steps(bridge_context):
    bridge,store,sid,manager,_=bridge_context
    async def incoming():
        for event in [{'type':'session.updated'}, {'type':'input_audio_buffer.speech_started'},
                      {'type':'input_audio_buffer.speech_stopped'},
                      {'type':'conversation.item.input_audio_transcription.completed','transcript':'空调增加一度'},
                      {'type':'response.created','response':{'id':'r1'}},
                      {'type':'response.done','response':{'id':'r1','status':'completed'}}]:
            yield json.dumps(event)
    bridge.upstream=incoming()
    await bridge.receive_model()
    steps=[e['step'] for e in bridge.browser.messages if e['type']=='voice.process']
    assert any(s['title']=='取得用户语音转写' and s['detail']=='空调增加一度' for s in steps)
    assert any(s['title']=='S2S 本轮响应结束' and s['duration_ms']>=0 for s in steps)
    assert len({s['connection_id'] for s in steps})==1
    await manager.close()
