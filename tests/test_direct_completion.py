from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.store import Store
from app.tool_preload import CandidatePreloader
from hermes_plugins.vehicle_completion import execute


@pytest.fixture
def rig(tmp_path, monkeypatch):
    store = Store(tmp_path/'test.db')
    catalog = CapabilityCatalog(store)
    monkeypatch.setenv('PREVIEW_DB', str(store.path))
    return store, catalog, CapabilityTools(store, catalog)


def setup(rig, goal='把所有分区空调设为26度', profile='demo-ready', mode='auto'):
    store, catalog, service = rig
    snap = catalog.snapshot()
    task, _ = store.create_task(store.create_session(), goal, 'one', catalog_revision=snap.revision,
                               profile_id=profile, discovery_mode='auto', completion_mode=mode)
    preload = CandidatePreloader().prepare(snap, goal)
    store.event(task['id'], 'catalog.preloaded' if preload['tools'] else 'catalog.preload_skipped', {k:v for k,v in preload.items() if k!='tools'})
    args = dict(task_id=task['id'], task_revision=0, catalog_revision=snap.revision, complete_if_success=True,
                calls=[dict(call_id='one', tool_id='control.climate.set_temperature', tool_version=snap.tools['control.climate.set_temperature']['version'], arguments=dict(zone='all', temperature_c=26))])
    request = {'model':'test', 'messages':[{'role':'user', 'content':[{'type':'text','text':json.dumps(dict(task_id=task['id'], task_revision=0, catalog_revision=snap.revision, user_request=goal))}]}]}
    return task, args, request


def response(args, extra=None):
    blocks = [NS(type='tool_use', name='mcp__vehicle_preview__call_tools', id='test', input=deepcopy(args))]
    if extra:
        blocks.append(NS(type='tool_use', name=extra, id='other', input={}))
    return NS(content=blocks)


def first_request(args, request, extra=None):
    return execute(request, lambda r: response(args, extra), api_mode='anthropic_messages', api_request_id='first')


def test_single_control_notifies_once_and_never_calls_provider_again(rig):
    store, catalog, service = rig
    task, args, request = setup(rig)
    first_request(args, request)
    result = service.call('call_tools', args)
    assert result['completion']['status'] == 'generated'
    assert store.task(task['id'])['status'] == 'generated'
    events = store.task_events(task['id'])
    assert sum(e['kind']=='agent.model_request' for e in events)==1
    assert sum(e['kind']=='task.fast_completed' for e in events)==1
    notice = store.events(task['session_id'], pending=True)
    assert len(notice)==1 and notice[0]['kind']=='task.generated'
    assert notice[0]['data']['calls'][0]['arguments']['temperature_c']==26
    assert notice[0]['data']['executed'] is False
    def forbidden(_):
        pytest.fail('Local completion must never call the provider')
    local = execute(request, forbidden, api_mode='anthropic_messages')
    assert local.id == 'local_vehicle_completion' and local.usage.output_tokens == 0
    assert local.stop_reason=='end_turn'
    service.call('call_tools', args)
    assert len(store.events(task['session_id'], pending=True))==1


@pytest.mark.parametrize('case', ['agent_mode','no_declaration','multiple_tools','two_controls','no_batch_guard','different_arguments','prior_query','complex','low_confidence','invalid_arguments','prior_failure','multiple_requests'])
def test_guards_keep_normal_loop(rig, case):
    store, catalog, service = rig
    goal = '把所有分区空调设为26度'
    if case=='complex': goal += '同时打开车窗'
    if case=='low_confidence': goal='帮我处理一下车里的东西'
    task, args, request = setup(rig, goal=goal, mode='agent' if case=='agent_mode' else 'auto')
    if case=='no_declaration': args.pop('complete_if_success')
    if case=='two_controls': args['calls'].append({**deepcopy(args['calls'][0]),'call_id':'two'})
    if case=='invalid_arguments': args['calls'][0]['arguments']['temperature_c']=45
    if case!='no_batch_guard': first_request(args, request, 'mcp__vehicle_preview__browse_capabilities' if case=='multiple_tools' else None)
    if case=='different_arguments': args['calls'][0]['arguments']['temperature_c']=25
    if case=='prior_query': store.event(task['id'],'catalog.browsed',{'path':['status'],'count':1})
    if case=='prior_failure': store.event(task['id'],'catalog.fallback',{'reason':'tool_error'})
    if case=='multiple_requests': first_request(args, request)
    result=service.call('call_tools',args)
    assert result.get('completion',{}).get('status')!='generated'
    assert store.task(task['id'])['status']=='running'
    assert not store.events(task['session_id'],pending=True)


def test_failure_keeps_diagnostics_and_cannot_autofinish_after_repair(rig):
    store,catalog,service=rig
    task,args,request=setup(rig,goal='把空气悬架升高一档',profile='demo-no-suspension')
    args['calls']=[dict(call_id='suspension',tool_id='control.suspension.set_height',tool_version='1.0.0',arguments={'action':'raise'})]
    first_request(args,request)
    result=service.call('call_tools',args)
    assert result['results'][0]['error']['code']=='NOT_EQUIPPED'
    assert store.task(task['id'])['status']=='running'
    res=service.call('finish_task',dict(task_id=task['id'],task_revision=0,outcome='unsupported',summary='failed',result_ids=[result['results'][0]['result_id']]))
    assert res['error']['code']=='NEEDS_DIAGNOSTIC_EVIDENCE'
    called=[]
    execute(request,lambda r:called.append(1),api_mode='anthropic_messages')
    assert called==[1]


def test_resume_and_cancel_never_autocomplete(rig):
    store,catalog,service=rig
    task,args,request=setup(rig)
    first_request(args,request)
    store.ask(task['id'],'哪个分区？')
    resumed=store.resume(task['id'],task['session_id'],'所有分区')
    args['task_revision']=resumed['revision']
    result=service.call('call_tools',args)
    assert result['completion']['status']=='continue'
    store.cancel(task['id'],task['session_id'])
    assert service.call('call_tools',args)['error']['code']=='INVALID_CONTEXT'
    assert not any(e['kind']=='task.generated' for e in store.task_events(task['id']))


def test_concurrent_duplicate_only_one_notification(rig):
    store,catalog,service=rig
    task,args,request=setup(rig)
    first_request(args,request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.call('call_tools',args),range(2)))
    assert store.task(task['id'])['status']=='generated'
    assert sum(e['kind']=='task.generated' for e in store.task_events(task['id']))==1


def test_unrelated_request_and_unsupported_provider_pass_through(rig):
    _,_,_=rig
    calls=[]
    for mode in ['anthropic_messages','chat_completions']:
        assert execute({'messages':[{'role':'user','content':'hello'}]},lambda r:calls.append(r) or 'real',api_mode=mode)=='real'
    assert len(calls)==2


def test_completion_mode_part_of_idempotency(rig):
    store,catalog,_=rig
    task,_,_=setup(rig)
    with pytest.raises(ValueError):
        store.create_task(task['session_id'], task['goal'], 'one',catalog_revision=task['catalog_revision'],profile_id=task['profile_id'],discovery_mode='auto',completion_mode='agent')


async def test_direct_completion_reaches_s2s_with_original_idle_gate(rig):
    import asyncio
    from app.config import Settings
    from app.hermes import TaskManager
    from app.realtime import VoiceBridge
    store,catalog,service=rig
    task,args,request=setup(rig)
    manager=TaskManager(store,catalog,Settings(db_path=store.path,hermes_key=''))
    async def browser_send(_): pass
    bridge=VoiceBridge(NS(send_json=browser_send),task['session_id'],Settings(),store,manager)
    first_request(args,request)
    service.call('call_tools',args)
    sent=[]
    async def send(e): sent.append(e)
    bridge.send=send
    bridge.gate.ready=True
    bridge.gate.user_speaking=True
    job=asyncio.create_task(bridge.deliver_notifications())
    try:
        await asyncio.sleep(.3)
        assert sent==[]
        bridge.gate.user_speaking=False
        await asyncio.sleep(.3)
        assert [e['type'] for e in sent]==['conversation.item.create','response.create']
        payload=json.loads(sent[0]['item']['content'][0]['text'])['background_task_event']
        assert payload['kind']=='task.generated'
        assert payload['result']['calls'][0]['arguments']['temperature_c']==26
        assert payload['executed'] is False
        assert '模拟下发成功' in payload['result']['message']
    finally:
        job.cancel();await asyncio.gather(job,return_exceptions=True);await manager.close()
