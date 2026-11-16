import json

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.store import Store
from app.task_observation import observation, facts
from app.task_presentation import build_process


def test_steps_show_actual_path_query_arguments_and_results(tmp_path):
    store=Store(tmp_path/'test.db'); catalog=CapabilityCatalog(store);snap=catalog.snapshot()
    task,_=store.create_task(store.create_session(),'测试','x',catalog_revision=snap.revision,profile_id='demo-ready')
    tools=CapabilityTools(store,catalog);ctx={'task_id':task['id'],'task_revision':0}
    tools.call('browse_capabilities',{**ctx,'path':['status','climate'],'query':'空调'})
    tools.call('browse_capabilities',{**ctx,'path':['wrong']})
    query=tools.call('call_tools',{**ctx,'catalog_revision':snap.revision,'calls':[{'call_id':'query','tool_id':'status.climate.get_state','tool_version':'1.0.0','arguments':{}}]})
    tools.call('call_tools',{**ctx,'catalog_revision':snap.revision,'calls':[{'call_id':'set','tool_id':'control.climate.set_temperature','tool_version':snap.tools['control.climate.set_temperature']['version'],'arguments':{'zone':'all','temperature_c':23}}]})
    events=store.task_events(task['id']);receipts=store.capability_results(task['id'])
    result=build_process(store.task(task['id']),events,snap,receipts)
    obs=[s['observation'] for s in result['steps'] if s.get('observation')]
    assert obs[0]['title']=='浏览目录：车况 / 空调'
    assert '关键词：空调' in obs[0]['input'] and '查询空调状态' in obs[0]['output']
    assert 'wrong' in obs[1]['input'] and 'DIRECTORY_NOT_FOUND' in obs[1]['output']
    assert '主驾温度（℃）：22' in obs[2]['output'] and '副驾温度（℃）：22' in obs[2]['output']
    assert '分区：全部' in obs[3]['input'] and '目标温度（℃）：23' in obs[3]['input']
    assert '未执行真实车控' in obs[3]['output']
    assert json.loads(obs[2]['data'])['results'][0]['result_id']==query['results'][0]['result_id']
    # Existing records can use receipts to display observed values without new model calls.
    for e in events: e['data'].pop('observation',None)
    history=build_process(store.task(task['id']),events,snap,receipts)
    old=[s['observation'] for s in history['steps'] if s.get('observation')]
    assert '未保存筛选条件' in old[0]['input']
    assert '未保存请求路径' in old[1]['input']  # Never invent the old failing path.
    assert '主驾温度（℃）：22' in old[2]['output']


def test_arbitrary_tool_fields_fallback_and_bounded_output(tmp_path):
    store=Store(tmp_path/'test.db');snap=CapabilityCatalog(store).snapshot()
    detail=observation('call_tools',{}, {'results':[{'tool_id':'custom','kind':'status','status':'returned_mock','arguments':{'x':2},'data':{'new_field':'<script>bad</script>','long':'x'*30000}}]},snap)
    assert 'new_field' in detail['output']
    assert len(detail['output'])<=2450 and len(detail['data'])<24100
    assert 'new_field：value' in facts({'new_field':'value'},{'properties':{'new_field':True}})
