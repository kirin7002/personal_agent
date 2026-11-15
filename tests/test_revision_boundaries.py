import asyncio
import json
import httpx
import pytest

from test_running_steer_validation import rig, task_for, call_args, finish, rejected
from experiments.running_steer.voice import capture_bridge
from app.hermes import HermesClient
from app.config import Settings


async def test_successive_edits_and_duplicate_ids(rig):
    task = await task_for(rig)
    one = await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'one')
    two = await rig.manager.revise(rig.sid,task['id'],'改成22度',1,'two')
    assert two['revision'] == 2 and two['id'] == one['id']
    assert (await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'one'))['revision'] == 2
    assert rejected(rig.service.call('call_tools',call_args(task,21,1,call_id='late')))
    assert rejected(rig.service.call('call_tools',call_args(task,21,2,call_id='late-new-version')))
    result = rig.service.call('call_tools',call_args(task,22,2,call_id='latest'))['results'][0]
    assert finish(rig,task,2,[result['result_id']])['status'] == 'generated'
    assert rig.store.task(task['id'])['calls'][0]['arguments']['temperature_c'] == 22


@pytest.mark.parametrize('reply', ['reject','timeout'])
async def test_steer_not_accepted_keeps_old_outputs_blocked(rig,reply):
    task = await task_for(rig)
    async def failure(*args):
        if reply == 'timeout':
            raise httpx.ReadTimeout('safe')
        return False
    rig.manager.hermes.steer = failure
    changed = await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'one')
    assert changed['status'] == 'interrupted'
    assert rig.store.latest_change(task['id'])['status'] == ('rejected' if reply == 'reject' else 'unconfirmed')
    assert rejected(rig.service.call('call_tools',call_args(task,23,0)))
    assert rejected(rig.service.call('call_tools',call_args(task,21,1)))
    assert not any(e['kind']=='task.generated' for e in rig.store.task_events(task['id']))


async def test_cancel_while_steer_in_flight_never_reopens(rig):
    task = await task_for(rig)
    entered = asyncio.Event(); release = asyncio.Event()
    async def delayed(*args):
        entered.set(); await release.wait(); return True
    rig.manager.hermes.steer = delayed
    changing = asyncio.create_task(rig.manager.revise(rig.sid,task['id'],'改成21度',0,'one'))
    await entered.wait()
    await rig.manager.cancel(rig.sid,task['id'])
    release.set(); await changing
    assert rig.store.task(task['id'])['status']=='cancelled'
    assert rejected(rig.service.call('call_tools',call_args(task,21,1)))


async def test_old_notification_queued_audio_and_transcript_are_invalidated(rig):
    task = await task_for(rig)
    bridge,sent = capture_bridge(rig.sid,rig.settings,rig.store,rig.manager)
    # Inject a queued old revision notice to exercise delivery races independently
    # of the real completion guard (a terminal task itself cannot be reopened).
    rig.store.event(task['id'],'task.generated',{'message':'旧23度结果','calls':[{'arguments':{'temperature_c':23}}]},True)
    notice = rig.store.events(rig.sid,pending=True)[0]
    bridge.notice_events[notice['id']] = notice
    bridge.notice_items[notice['id']] = 'old-notice'
    bridge.inflight = notice['id']; bridge.current_response = 'old-response'
    bridge.response_notices['old-response'] = notice['id']
    bridge.gate.model_busy = True; bridge.gate.playback_busy = True
    await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'one')
    assert bridge.inflight is None and not bridge.gate.playback_busy
    assert not rig.store.notification_current(rig.sid,notice['id'])
    assert {'type':'response.cancel'} in sent
    assert {'type':'conversation.item.delete','item_id':'old-notice'} in sent
    assert any(e['type']=='voice.invalidate' for e in bridge.browser.messages)
    async def late():
        yield json.dumps({'type':'response.audio.delta','response_id':'old-response','delta':'AAAA'})
        yield json.dumps({'type':'response.audio_transcript.delta','response_id':'old-response','delta':'23度已成功'})
    bridge.upstream = late(); await bridge.receive_model()
    assert not any(e['type'] in {'audio','assistant.delta'} for e in bridge.browser.messages)


async def test_duplicate_control_different_call_id_reuses_receipt_and_preserves_id_binding(rig):
    task = await task_for(rig)
    first = rig.service.call('call_tools',call_args(task,23,0,call_id='first'))['results'][0]
    second = rig.service.call('call_tools',call_args(task,23,0,call_id='second'))['results'][0]
    assert second['result_id']==first['result_id']
    assert len(rig.store.capability_results(task['id']))==1
    assert rejected(rig.service.call('call_tools',call_args(task,22,0,call_id='second')))


async def test_read_poll_retry_does_not_repeat_mutations():
    requests=[]
    async def remote(request):
        requests.append(request.method)
        return httpx.Response(503 if len(requests)<3 else 200,json={'status':'running'})
    async with httpx.AsyncClient(base_url='http://127.0.0.1',transport=httpx.MockTransport(remote)) as client:
        assert (await HermesClient(Settings(),client).status('run'))['status']=='running'
    assert requests == ['GET']*3


async def test_late_response_created_after_edit_is_still_bound_to_old_notice(rig):
    task=await task_for(rig)
    bridge,sent=capture_bridge(rig.sid,rig.settings,rig.store,rig.manager)
    rig.store.event(task['id'],'task.generated',{'message':'旧23度结果'},True)
    notice=rig.store.events(rig.sid,pending=True)[0]
    bridge.notice_events[notice['id']]=notice
    bridge.inflight=notice['id']; bridge.pending_notice_response=notice['id']
    bridge.gate.model_busy=True
    await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'edit')
    async def late():
        yield json.dumps({'type':'response.created','response':{'id':'late-created'}})
        yield json.dumps({'type':'response.audio.delta','response_id':'late-created','delta':'AAAA'})
        yield json.dumps({'type':'response.audio_transcript.delta','response_id':'late-created','delta':'23度已成功'})
    bridge.upstream=late();await bridge.receive_model()
    assert bridge.response_notices['late-created']==notice['id']
    assert not any(e['type'] in {'audio','assistant.delta'} for e in bridge.browser.messages)
    assert {'type':'response.cancel'} in sent


async def test_edit_other_task_does_not_flush_or_ack_unrelated_audio(rig):
    a=await task_for(rig,'before_control'); b=await task_for(rig,'during_model')
    bridge,sent=capture_bridge(rig.sid,rig.settings,rig.store,rig.manager)
    rig.store.event(a['id'],'task.generated',{'message':'A结果'},True)
    notice=rig.store.events(rig.sid,pending=True)[0]
    bridge.notice_events[notice['id']]=notice; bridge.inflight=notice['id']
    bridge.current_response='response-a';bridge.response_notices['response-a']=notice['id']
    bridge.gate.playback_busy=True;bridge.gate.audio_until=100000000
    bridge.notification_response_done=True
    await rig.manager.revise(rig.sid,b['id'],'改成21度',0,'edit-b')
    assert bridge.gate.playback_busy and bridge.inflight==notice['id']
    assert not any(e['type']=='voice.invalidate' for e in bridge.browser.messages)
    assert rig.store.notification_current(rig.sid,notice['id'])


@pytest.mark.parametrize('goal,text', [('把所有分区空调设为23度，音量调到5','改成21度'), ('把所有分区空调设为23度','不要改成21度')])
async def test_compound_and_negated_revisions_pause_instead_of_dropping_intent(rig,goal,text):
    task=await task_for(rig,goal=goal)
    current=await rig.manager.revise(rig.sid,task['id'],text,0,'ambiguous')
    assert current['status']=='awaiting_input' and current['goal']==goal
    assert rig.store.latest_change(task['id'])['constraints']=={}


async def test_asr_temperature_wording_preserves_explicit_zone(rig):
    task=await task_for(rig,goal='把所有分区的空调温度设置成23度。')
    result=await rig.manager.revise(rig.sid,task['id'],'改成21度。',0,'asr')
    assert result['status']=='running' and result['goal']=='把所有分区空调设为21度'
