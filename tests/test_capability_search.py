from copy import deepcopy
import json

import pytest

from app.capability_catalog import CapabilityCatalog, CatalogSnapshot, CatalogError
from app.capability_search import CapabilitySearch, encoded_size
from app.capability_tools import CapabilityTools
from app.direct_completion import eligible
from app.store import Store
from app.tool_preload import CandidatePreloader


@pytest.fixture
def rig(tmp_path):
 store=Store(tmp_path/'test.db');catalog=CapabilityCatalog(store)
 return store,catalog,catalog.snapshot()


@pytest.mark.parametrize('query,kind,expected',[
 ('读取当前空调温度','status','status.climate.get_state'),
 ('空调使用说明','manual','manual.climate.lookup'),
 ('车辆配置和接口','status','status.vehicle.get_configuration'),
 ('现在是否驻车驾驶状态','status','status.vehicle.get_driving_state'),
 ('设置空调温度','control','control.climate.set_temperature'),
 ('status.climate.get_state',None,'status.climate.get_state'),
])
def test_chinese_global_search_returns_matching_schemas(rig,query,kind,expected):
 _,_,snap=rig;s=CapabilitySearch();r=s.search(snap,query,kind)
 assert expected in [t['id'] for t in r['tools']]
 assert len(r['matches'])<=4
 assert all('mock' not in t and 'input_schema' in t for t in r['tools'])
 assert r['payload_chars']==encoded_size(r)<=12000
 assert r['method']=='bm25_cjk_ngrams'
 assert not r['cache_hit'] and s.search(snap,query,kind)['cache_hit']


def test_unknown_disabled_budget_and_config_only_tool(rig):
 _,_,snap=rig;s=CapabilitySearch(max_chars=1024)
 assert s.search(snap,'冰箱冷藏')['matches']==[]
 r=s.search(snap,'查询本车配置','status')
 assert encoded_size(r)<=1024
 assert r['truncated'] and len(r['tools'])<len(r['matches'])
 data=deepcopy(snap.bundle)
 t=deepcopy(snap.tools['control.climate.set_temperature'])
 t.update(id='control.climate.extra',title='净化强度调整',aliases=['空气净化强度'],description='调整空气净化强度',version='1.0.0')
 t['examples']=[{'instruction':'空气净化强度调高','arguments':t['examples'][0]['arguments']}]
 data['tools'].append(t);changed=CatalogSnapshot(data)
 assert 'control.climate.extra' in [m['id'] for m in s.search(changed,'净化强度')['matches']]
 t['enabled']=False;disabled=CatalogSnapshot(data)
 assert 'control.climate.extra' not in [m['id'] for m in s.search(disabled,'净化强度')['matches']]


def test_search_is_pinned_readonly_and_requires_active_task(rig):
 store,cat,snap=rig;sid=store.create_session();service=CapabilityTools(store,cat)
 task,_=store.create_task(sid,'查温度','x',catalog_revision=snap.revision,profile_id='demo-ready')
 args={'task_id':task['id'],'task_revision':0,'query':'当前空调温度','kind':'status'}
 r=service.call('search_capabilities',args)
 assert r['catalog_revision']==task['catalog_revision']
 assert any(t['id']=='status.climate.get_state' for t in r['tools'])
 assert store.capability_results(task['id'])==[] and store.task(task['id'])['status']=='running'
 assert any(e['kind']=='catalog.searched' for e in store.task_navigation(task['id']))
 obs=store.task_events(task['id'])[-1]['data']['observation']
 assert '全局检索' in obs['title'] and '当前空调温度' in obs['input'] and '完整参数已提供' in obs['output']
 store.cancel(task['id'],sid)
 assert service.call('search_capabilities',args)['error']['code']=='INVALID_CONTEXT'


def test_related_queries_are_definitions_and_do_not_expand_direct_completion(rig):
 _,_,snap=rig;r=CandidatePreloader().prepare(snap,'空调温度增加一度')
 assert r['tool_ids']==['control.climate.set_temperature']
 assert 'status.climate.get_state' in r['related_tool_ids']
 assert 'manual.climate.lookup' in r['related_tool_ids']
 assert ['status','climate'] in [d['path'] for d in r['directory_hints']]
 assert r['payload_chars']<=12000 and len(r['related_tools'])<=4
 assert all('mock' not in t for t in r['related_tools'])
 assert 'primary_tool_ids' not in r  # Stable stored primary candidate identity remains tool_ids.
 query=CandidatePreloader().prepare(snap,'查询当前空调温度')
 task={'completion_mode':'auto','discovery_mode':'auto','source':'hermes','revision':0,'history':[],'goal':'查询当前空调温度'}
 assert not eligible(task,query)
 assert CandidatePreloader().prepare(snap,'空调温度增加一度',mode='off')['status']=='skipped'
 assert CandidatePreloader().prepare(snap,'空调温度增加一度',task_revision=1)['status']=='skipped'


def test_optional_directory_relation_import_validation(rig):
 _,cat,snap=rig;data=deepcopy(snap.bundle)
 d=next(d for d in data['directories'] if d['path']==['control','suspension'])
 d['related_query_paths']=[['status','vehicle']]
 cat.validate(data)
 r=CandidatePreloader().prepare(CatalogSnapshot(data),'把空气悬架升高一档')
 assert 'status.vehicle.get_configuration' in r['related_tool_ids']
 assert 'status.vehicle.get_driving_state' in r['related_tool_ids']
 d['related_query_paths']=[['status','missing']]
 with pytest.raises(CatalogError):cat.validate(data)


def test_search_cache_does_not_change_old_pinned_snapshot(rig):
 _,_,snap=rig;s=CapabilitySearch();old=s.search(snap,'空调温度','control')
 data=deepcopy(snap.bundle)
 target=next(t for t in data['tools'] if t['id']=='control.climate.set_temperature');target['enabled']=False
 changed=CatalogSnapshot(data)
 assert 'control.climate.set_temperature' not in [m['id'] for m in s.search(changed,'空调温度','control')['matches']]
 assert s.search(snap,'空调温度','control')['matches']==old['matches']


def test_simple_explicit_goal_uses_small_payload_and_relative_keeps_queries(rig):
 _,_,snap=rig;p=CandidatePreloader()
 for goal in ['全部空调温度设置为21度','把所有分区空调设为26度','打开空调AC制冷']:
  r=p.prepare(snap,goal)
  assert r['preload_policy']=='primary_only'
  assert r['related_tools']==[] and r['directory_hints']==[]
  assert r['payload_chars']<1500
 for goal in ['空调温度增加一度','主驾空调温度增加一度','空调温度设置为比现在高一度']:
  r=p.prepare(snap,goal)
  assert r.get('preload_policy')!='primary_only'
  assert 'status.climate.get_state' in r.get('related_tool_ids',[])
