from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace as NS

import pytest

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.store import Store
from hermes_plugins.vehicle_completion import execute


@pytest.fixture
def rig(tmp_path,monkeypatch):
 store=Store(tmp_path/'test.db');cat=CapabilityCatalog(store);service=CapabilityTools(store,cat)
 monkeypatch.setenv('PREVIEW_DB',str(store.path))
 return store,cat,service


def new(rig,profile='demo-ready'):
 store,cat,_=rig
 task,_=store.create_task(store.create_session(),'测试任务','one',catalog_revision=cat.snapshot().revision,profile_id=profile)
 return task,{'task_id':task['id'],'task_revision':0}


def call(rig,ctx,tid,args,cid):
 store,cat,service=rig;tool=cat.snapshot().tools[tid]
 return service.call('call_tools',{**ctx,'catalog_revision':cat.snapshot().revision,'calls':[{'call_id':cid,'tool_id':tid,'tool_version':tool['version'],'arguments':args}]})['results'][0]


def finish(rig,ctx,rs,outcome=None,unresolved=None):
 args={**ctx,'summary':'测试结论','result_ids':[r['result_id'] for r in rs]}
 if outcome is not None:args['outcome']=outcome
 if unresolved is not None:args['unresolved']=unresolved
 return rig[2].call('finish_task',args)


@pytest.mark.parametrize('outcome',[None,'auto','prepared','answered','mixed'])
def test_mixed_evidence_classifies_without_retry(rig,outcome):
 store,cat,service=rig;task,ctx=new(rig)
 q=call(rig,ctx,'status.climate.get_state',{},'q')
 c=call(rig,ctx,'control.climate.set_temperature',{'zone':'all','temperature_c':23},'c')
 r=finish(rig,ctx,[q,c],outcome)
 assert r['status']=='generated' and r['outcome']=='mixed'
 assert r['type_resolution']['source']=='validated_receipts'
 assert r['type_resolution']['requested']==(outcome or 'auto')
 assert r['type_resolution']['control_results']==r['type_resolution']['query_results']==1
 final=json.loads(store.task(task['id'])['final_result'])
 assert len(final['results'])==2 and final['outcome']=='mixed'
 assert sum(e['kind']=='task.outcome_resolved' for e in store.task_events(task['id']))==1
 assert len(store.events(task['session_id'],pending=True))==1


@pytest.mark.parametrize('kind,expected',[('control','prepared'),('query','answered')])
def test_default_auto_single_result_types(rig,kind,expected):
 task,ctx=new(rig)
 r=call(rig,ctx,'control.climate.set_temperature',{'zone':'all','temperature_c':23},'c') if kind=='control' else call(rig,ctx,'status.climate.get_state',{},'q')
 out=finish(rig,ctx,[r])
 assert out['outcome']==expected
 if kind=='query':assert '下发成功' not in out['message']


@pytest.mark.parametrize('bad_type', ['prepared','mixed'])
def test_missing_claimed_control_is_not_silently_completed(rig,bad_type):
 task,ctx=new(rig);q=call(rig,ctx,'status.climate.get_state',{},'q')
 assert finish(rig,ctx,[q],bad_type)['error']['code']=='INVALID_OUTCOME'
 assert rig[0].task(task['id'])['status']=='running'


def test_empty_unknown_cancelled_and_unresolved_still_rejected(rig):
 store,cat,service=rig;task,ctx=new(rig)
 assert finish(rig,ctx,[])['error']['code']=='INVALID_OUTCOME'
 assert service.call('finish_task',{**ctx,'summary':'test','result_ids':['invented']})['error']['code']=='INVALID_EVIDENCE'
 c=call(rig,ctx,'control.climate.set_temperature',{'zone':'all','temperature_c':23},'c')
 assert finish(rig,ctx,[c],unresolved=['另一项未完成'])['error']['code']=='INVALID_OUTCOME'
 store.cancel(task['id'],task['session_id'])
 assert finish(rig,ctx,[c])['error']['code']=='INVALID_CONTEXT'
 assert not any(e['kind']=='task.outcome_resolved' for e in store.task_events(task['id']))


def test_auto_does_not_hide_unreferenced_failure(rig):
 task,ctx=new(rig,profile='demo-no-suspension')
 bad=call(rig,ctx,'control.suspension.set_height',{'action':'raise'},'bad')
 q=call(rig,ctx,'status.vehicle.get_configuration',{},'q')
 assert finish(rig,ctx,[q])['error']['code']=='EXPLICIT_OUTCOME_REQUIRED'
 assert finish(rig,ctx,[q,bad],'prepared')['error']['code']=='INVALID_OUTCOME'
 result=finish(rig,ctx,[q,bad],'unsupported')
 assert result['status']=='unsupported' and 'type_resolution' not in result


def test_repaired_arguments_can_finish_but_partial_stays_partial(rig):
 task,ctx=new(rig)
 bad=call(rig,ctx,'control.climate.set_temperature',{'zone':'all','temperature_c':45},'bad')
 good=call(rig,ctx,'control.climate.set_temperature',{'zone':'all','temperature_c':23},'fixed')
 assert finish(rig,ctx,[good])['status']=='generated'
 task,ctx=new(rig)
 q=call(rig,ctx,'status.climate.get_state',{},'q')
 partial=finish(rig,ctx,[q],'partial',['还未生成车控'])
 assert partial['status']=='partial' and 'type_resolution' not in partial


def test_local_end_skips_provider_after_classified_query_or_mixed_task(rig):
 store,cat,service=rig;task,ctx=new(rig);q=call(rig,ctx,'status.climate.get_state',{},'q')
 finish(rig,ctx,[q])
 envelope={**ctx,'catalog_revision':task['catalog_revision'],'user_request':task['goal']}
 request={'model':'test','messages':[{'role':'user','content':json.dumps(envelope)}]}
 def forbidden(_):pytest.fail('No provider call after validated completion')
 result=execute(request,forbidden,api_mode='anthropic_messages')
 assert result.id=='local_vehicle_completion' and result.usage.output_tokens==0
 assert not any(e['kind']=='agent.model_request' for e in store.task_events(task['id']))


def test_duplicate_finish_has_one_resolution_and_notice(rig):
 store,cat,service=rig;task,ctx=new(rig);q=call(rig,ctx,'status.climate.get_state',{},'q')
 with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:finish(rig,ctx,[q]),range(2)))
 assert sum(e['kind']=='task.outcome_resolved' for e in store.task_events(task['id']))==1
 assert sum(e['kind']=='task.completed' for e in store.task_events(task['id']))==1
