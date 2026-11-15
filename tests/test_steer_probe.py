"""Guards used only by the isolated steer experiment."""
import json
import pytest
from app.store import Store
from app.capability_catalog import CapabilityCatalog
from experiments.steer_probe.adapter import setup,register_case,revise,ProbeTools,TOOL


@pytest.fixture
def rig(tmp_path):
 store=Store(tmp_path/'test.db');setup(store);cat=CapabilityCatalog(store);sid=store.create_session()
 task,_=store.create_task(sid,'把所有分区空调设为23度','probe',catalog_revision=cat.snapshot().revision,profile_id='demo-ready')
 register_case(store,task,'unit',True,'before_control')
 return store,cat,sid,task,ProbeTools(store,cat)


def call(rig,revision,temp,cid):
 store,cat,sid,task,tools=rig
 return tools.call('call_tools',{'task_id':task['id'],'task_revision':revision,'catalog_revision':cat.snapshot().revision,
  'calls':[{'call_id':cid,'tool_id':TOOL,'tool_version':cat.snapshot().tools[TOOL]['version'],'arguments':{'zone':'all','temperature_c':temp}}]})


def finish(rig,revision,ids):
 store,cat,sid,task,tools=rig
 return tools.call('finish_task',{'task_id':task['id'],'task_revision':revision,'summary':'按新目标生成21度预览','result_ids':ids})


def test_stale_revision_and_stale_value_are_both_rejected(rig):
 store,cat,sid,task,tools=rig
 assert revise(store,task['id'],sid,'把所有分区空调设为21度')==1
 assert call(rig,0,23,'old')['error']['code']=='INVALID_CONTEXT'
 assert call(rig,1,23,'wrong-value')['error']['code']=='OUTDATED_TARGET'
 assert store.capability_results(task['id'])==[]
 good=call(rig,1,21,'new')['results'][0]
 assert good['arguments']['temperature_c']==21
 assert finish(rig,1,[good['result_id']])['status']=='generated'
 assert len(store.events(sid,pending=True))==1


def test_old_preview_is_retained_but_cannot_finish_new_goal(rig):
 store,cat,sid,task,tools=rig
 old=call(rig,0,23,'old')['results'][0]
 revise(store,task['id'],sid,'把所有分区空调设为21度')
 assert store.task(task['id'])['calls']==[]
 assert len(store.capability_results(task['id']))==1
 assert finish(rig,1,[old['result_id']])['error']['code']=='SUPERSEDED_EVIDENCE'
 good=call(rig,1,21,'new')['results'][0]
 assert finish(rig,1,[old['result_id'],good['result_id']])['error']['code']=='SUPERSEDED_EVIDENCE'
 assert finish(rig,1,[good['result_id']])['status']=='generated'
 result=json.loads(store.task(task['id'])['final_result'])
 assert [c['arguments']['temperature_c'] for c in result['calls']]==[21]
 assert len(store.capability_results(task['id']))==2
 assert len(store.events(sid,pending=True))==1
 with pytest.raises(ValueError,match='TASK_NOT_RUNNING'):revise(store,task['id'],sid,'20度',20)


def test_foreign_session_and_cancelled_task_cannot_be_revised(rig):
 store,cat,sid,task,tools=rig
 with pytest.raises(KeyError):revise(store,task['id'],store.create_session(),'21度')
 store.cancel(task['id'],sid)
 with pytest.raises(ValueError,match='TASK_NOT_RUNNING'):revise(store,task['id'],sid,'21度')
 assert store.task(task['id'])['revision']==0


def test_normal_task_without_registered_probe_retains_original_behavior(rig):
 store,cat,sid,task,tools=rig
 other,_=store.create_task(sid,'其他测试','other',catalog_revision=cat.snapshot().revision,profile_id='demo-ready')
 r=tools.call('call_tools',{'task_id':other['id'],'task_revision':0,'catalog_revision':cat.snapshot().revision,
 'calls':[{'call_id':'normal','tool_id':TOOL,'tool_version':cat.snapshot().tools[TOOL]['version'],'arguments':{'zone':'all','temperature_c':23}}]})
 assert r['results'][0]['status']=='prepared'
