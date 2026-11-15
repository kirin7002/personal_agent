from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from app.capability_catalog import CapabilityCatalog, CatalogSnapshot
from app.capability_tools import CapabilityTools
from app.config import ROOT, Settings
from app.hermes import HermesClient, TaskManager
from app.store import Store
from app.tool_preload import CandidatePreloader


@pytest.fixture
def snapshot():
    return CatalogSnapshot(json.loads((ROOT/'capabilities/exports/vehicle-capabilities.bundle.json').read_text()))


@pytest.mark.parametrize('goal,tool', [
    ('把所有分区空调设为26度','control.climate.set_temperature'),
    ('主驾空调温度改为24度','control.climate.set_temperature'),
    ('主驾空调风量设为四档','control.climate.set_fan_speed'),
    ('左前车窗打开到40%','control.windows.set_position'),
    ('媒体音量设为35%','control.multimedia.set_volume'),
    ('副驾座椅通风设为二档','control.seats.set_ventilation'),
    ('氛围灯亮度设为70%','control.lights.set_ambient_brightness'),
    ('把空气悬架升高一档','control.suspension.set_height'),
])
def test_simple_metadata_recall_without_argument_generation(snapshot, goal, tool):
    result=CandidatePreloader().prepare(snapshot,goal)
    assert result['status']=='preloaded' and tool in result['tool_ids']
    assert len(result['tools'])<=3 and result['payload_chars']<=12000
    assert all('mock' not in t and 'input_schema' in t for t in result['tools'])
    assert 'arguments' not in result
    assert result['catalog_revision']==snapshot.revision


@pytest.mark.parametrize('goal', [
    '空调23度，同时主驾座椅加热二档',
    '先查车书再把空调设为23度',
    '空气悬架升高一档，如果失败查车况和车书',
    '查询当前空调状态',
    '帮我调整空气悬架',
    '我有点冷',
    '打开冰箱设为4度',
])
def test_complex_query_ambiguous_and_unknown_never_enable_direct_completion(snapshot,goal):
    result=CandidatePreloader().prepare(snapshot,goal)
    assert result['scores']==[]  # General recall never qualifies for direct completion.
    assert result['status'] in {'skipped','preloaded'}


def test_invalid_value_is_not_clamped_and_schema_still_enforced(snapshot,tmp_path):
    preload=CandidatePreloader().prepare(snapshot,'把所有分区空调设为45度')
    assert preload['tools'][0]['input_schema']['properties']['temperature_c']['maximum']==30
    store=Store(tmp_path/'test.db')
    catalog=CapabilityCatalog(store)
    sid=store.create_session()
    task,_=store.create_task(sid,'空调45度','invalid',catalog_revision=catalog.snapshot().revision,profile_id='demo-ready',discovery_mode='auto')
    store.event(task['id'],'catalog.preloaded',{k:v for k,v in preload.items() if k!='tools'})
    service=CapabilityTools(store,catalog)
    ctx={'task_id':task['id'],'task_revision':0}
    result=service.call('call_tools',{**ctx,'catalog_revision':task['catalog_revision'],'calls':[{'call_id':'bad','tool_id':'control.climate.set_temperature','tool_version':'1.0.0','arguments':{'zone':'all','temperature_c':45}}]})
    assert result['results'][0]['error']['code']=='INVALID_ARGUMENTS'
    assert store.task(task['id'])['calls']==[]
    assert any(e['kind']=='catalog.fallback' and e['reason']=='tool_error' for e in store.task_navigation(task['id']))
    assert service.call('request_user_input',{**ctx,'question':'目标超出范围，请确认有效温度'})['status']=='awaiting_input'


def test_prefetched_failure_can_query_other_domains_in_same_task(tmp_path):
    store=Store(tmp_path/'test.db'); catalog=CapabilityCatalog(store); sid=store.create_session()
    snap=catalog.snapshot()
    task,_=store.create_task(sid,'把空气悬架升高一档','fallback',catalog_revision=snap.revision,profile_id='demo-no-suspension',discovery_mode='auto')
    preload=CandidatePreloader().prepare(snap,task['goal'])
    store.event(task['id'],'catalog.preloaded',{k:v for k,v in preload.items() if k!='tools'})
    service=CapabilityTools(store,catalog); ctx={'task_id':task['id'],'task_revision':0}
    result=service.call('call_tools',{**ctx,'catalog_revision':snap.revision,'calls':[{'call_id':'control','tool_id':'control.suspension.set_height','tool_version':'1.0.0','arguments':{'action':'raise'}}]})
    assert result['results'][0]['error']['code']=='NOT_EQUIPPED'
    rejected=service.call('finish_task',{**ctx,'outcome':'unsupported','summary':'不能生成','result_ids':[result['results'][0]['result_id']]})
    assert rejected['error']['code']=='NEEDS_DIAGNOSTIC_EVIDENCE'
    bypass=service.call('finish_task',{**ctx,'outcome':'failed','summary':'不能生成','result_ids':[]})
    assert bypass['error']['code']=='NEEDS_DIAGNOSTIC_EVIDENCE'
    assert service.call('browse_capabilities',{**ctx,'path':['status','vehicle']})['items']
    result=service.call('call_tools',{**ctx,'catalog_revision':snap.revision,'calls':[{'call_id':'configuration','tool_id':'status.vehicle.get_configuration','tool_version':'1.0.0','arguments':{}}]})
    assert result['results'][0]['data']['equipment']['air_suspension'] is False
    assert store.task(task['id'])['status']=='running'
    assert len([n for n in store.task_navigation(task['id']) if n['kind']=='catalog.fallback'])==1


def test_version_cache_and_configuration_only_new_tool(snapshot):
    preloader=CandidatePreloader()
    first=preloader.prepare(snapshot,'把所有分区空调设为23度')
    assert not first['cache_hit']
    assert preloader.prepare(snapshot,'把所有分区空调设为26度')['cache_hit']
    data=deepcopy(snapshot.bundle)
    original=next(t for t in data['tools'] if t['id']=='control.climate.set_temperature')
    original['enabled']=False;original['version']='2.0.0'
    new=deepcopy(original)
    new.update(id='control.climate.set_purification',title='设置空调净化强度',aliases=['净化强度'],description='生成净化强度结构',enabled=True)
    new['examples']=[{'instruction':'空调净化强度设为二档','arguments':{'level':2}}]
    new['input_schema']={'type':'object','properties':{'level':{'type':'integer','minimum':0,'maximum':3}},'required':['level']}
    data['tools'].append(new)
    changed=CatalogSnapshot(data)
    result=preloader.prepare(changed,'空调净化强度设为三档')
    assert result['tool_ids']==['control.climate.set_purification']
    assert not result['cache_hit']
    assert 'control.climate.set_temperature' not in preloader.prepare(changed,'把所有分区空调设为23度')['tool_ids']
    assert preloader.prepare(snapshot,'把所有分区空调设为23度')['tool_ids']==first['tool_ids']


def test_disable_continuation_budget_and_ambiguity(snapshot):
    p=CandidatePreloader()
    assert p.prepare(snapshot,'媒体音量设为30%',mode='off')['reason']=='disabled'
    assert p.prepare(snapshot,'媒体音量设为30%',task_revision=1)['reason']=='continuation'
    assert CandidatePreloader(max_chars=256).prepare(snapshot,'媒体音量设为30%')['reason']=='schema_budget'
    data=deepcopy(snapshot.bundle)
    original=next(t for t in data['tools'] if t['id']=='control.multimedia.set_volume')
    for i in range(4):
        t=deepcopy(original);t['id']=f'control.multimedia.same_{i}';data['tools'].append(t)
    assert p.prepare(CatalogSnapshot(data),'媒体音量设为30%')['scores']==[]


def test_shipped_control_scenarios_recall_or_safely_skip(snapshot):
    p=CandidatePreloader()
    for scenario in snapshot.scenarios.values():
        if scenario['kind']!='control': continue
        result=p.prepare(snapshot,scenario['instruction'])
        if result['tools']:
            expected={c['tool_id'] for c in scenario['expected']['calls']}
            assert set(result['tool_ids']) & expected,scenario['id']


def test_mode_is_part_of_request_identity_and_survives_clarification(tmp_path):
    store=Store(tmp_path/'test.db'); catalog=CapabilityCatalog(store);sid=store.create_session()
    task,_=store.create_task(sid,'test','same-key',catalog_revision=catalog.snapshot().revision,profile_id='demo-ready',discovery_mode='auto')
    with pytest.raises(ValueError):
        store.create_task(sid,'test','same-key',catalog_revision=catalog.snapshot().revision,profile_id='demo-ready',discovery_mode='off')
    store.ask(task['id'],'补充一下')
    resumed=store.resume(task['id'],sid,'回答')
    assert resumed['discovery_mode']=='auto' and resumed['catalog_revision']==task['catalog_revision']


async def test_candidate_schemas_are_task_context_not_system_prompt(tmp_path):
    store=Store(tmp_path/'test.db');catalog=CapabilityCatalog(store);sid=store.create_session()
    seen=[]
    def handler(request):
        if request.url.path=='/v1/capabilities':return httpx.Response(200,json={'features':{'run_submission':True}})
        if request.method=='POST' and request.url.path=='/v1/runs':
            body=json.loads(request.content);seen.append(body)
            context=json.loads(body['input'])
            store.finish(context['task_id'],'failed','test transport terminates without inference')
            return httpx.Response(202,json={'run_id':'run_test'})
        return httpx.Response(200,json={'status':'cancelled'})
    settings=Settings(db_path=store.path,hermes_key='test')
    hermes=HermesClient(settings,httpx.AsyncClient(base_url='http://hermes',transport=httpx.MockTransport(handler)))
    manager=TaskManager(store,catalog,settings,hermes)
    for mode in ['off','auto']:
        task,_=store.create_task(sid,'把所有分区空调设为26度',mode,catalog_revision=catalog.snapshot().revision,profile_id='demo-ready',discovery_mode=mode)
        await manager.run(task)
    assert seen[0]['instructions']==seen[1]['instructions']
    assert 'control.climate.set_temperature' not in seen[1]['instructions']
    assert 'candidate_tools' not in json.loads(seen[0]['input'])
    candidates=json.loads(seen[1]['input'])['candidate_tools']
    assert candidates['catalog_revision']==catalog.snapshot().revision
    assert candidates['tools'][0]['id']=='control.climate.set_temperature'
    await manager.close()
