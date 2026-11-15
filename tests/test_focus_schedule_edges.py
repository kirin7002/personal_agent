import asyncio
import json
import pytest

from test_focus_schedule_integration import focus_rig, text
from app.realtime import VoiceBridge
from hermes_plugins.vehicle_completion import execute


async def plan(rig):
    r=await rig.http.post('/api/plans',json={'goal':'查询空调温度','schedule':{'frequency':'daily','time':'08:00'},'request_key':'plan'})
    assert r.status_code==202
    return r.json()['plan']


@pytest.mark.parametrize('value',['不要取消整个计划','别取消整个计划','不取消整个计划','先不要暂停这个计划','不是说取消整个计划'])
async def test_negated_plan_management_does_not_mutate(focus_rig,value):
    rig=focus_rig;p=await plan(rig)
    await text(rig,value,'negative')
    actual=rig.manager.plans.get(rig.sid,p['id'])
    assert actual['status']=='pending' and actual['revision']==0


async def test_plan_pause_focus_is_recorded_before_remote_wait(focus_rig):
    rig=focus_rig;p=await plan(rig)
    batch=rig.manager.plans.due(rig.manager.catalog.snapshot().revision,'demo-ready','auto','auto',now=p['next_run_at'])
    assert batch['tasks']
    entered=asyncio.Event();release=asyncio.Event()
    async def stop(_):entered.set();await release.wait();return True
    rig.manager.stop_remote=stop
    pause=asyncio.create_task(rig.manager.plan_action(rig.sid,p['id'],'pause',0))
    await entered.wait()
    newer=rig.manager.submit(rig.sid,'打开车窗','newer')
    release.set();await pause
    assert rig.manager.router.current(rig.sid)['id']==newer['id']


async def test_schedule_time_edit_keeps_original_goal(focus_rig):
    rig=focus_rig;p=await plan(rig)
    result=await text(rig,'把这个计划改为每天早上9点','edit')
    assert result['kind']=='plan_updated'
    assert result['plan']['id']==p['id']
    assert result['plan']['schedule']['time']=='09:00'
    assert result['plan']['goal']=='查询空调温度'
    assert len(rig.manager.plans.list(rig.sid))==1
    assert not rig.store.tasks(rig.sid)


async def test_question_ends_native_round_without_extra_model(focus_rig,monkeypatch):
    rig=focus_rig
    task=rig.manager.submit(rig.sid,'打开车窗','wait-local')
    rig.store.ask(task['id'],'哪个车窗？',0)
    monkeypatch.setenv('PREVIEW_DB',str(rig.store.path))
    request={'model':'fixture','messages':[{'role':'user','content':json.dumps({'task_id':task['id'],'task_revision':0,'catalog_revision':task['catalog_revision'],'user_request':task['goal']})}]}
    def forbidden(_):pytest.fail('No model round is needed to close a question')
    response=execute(request,forbidden,api_mode='anthropic_messages')
    assert response.id=='local_vehicle_completion'
    assert rig.store.task(task['id'])['status']=='awaiting_input'


async def test_followup_still_queues_through_unified_route(focus_rig):
    rig=focus_rig
    task=rig.manager.submit(rig.sid,'空调设置23度','follow')
    rig.store.ask(task['id'],'哪个分区？',0)
    result=await text(rig,'做完再告诉我详细参数','followup')
    assert result['kind']=='append_followup' and result['task_id']==task['id']
    assert len(rig.store.tasks(rig.sid))==1
    assert rig.store.task(task['id'])['status']=='awaiting_input'


async def test_explicit_candidate_resolves_historical_ambiguity(focus_rig):
    rig=focus_rig
    a=rig.manager.submit(rig.sid,'打开车窗','window-a')
    b=rig.manager.submit(rig.sid,'打开车窗','window-b')
    rig.store.ask(a['id'],'哪个车窗？',0);rig.store.ask(b['id'],'哪个车窗？',0)
    wording='之前开车窗那个，取消'
    first=await text(rig,wording,'which')
    assert first['kind']=='clarify' and len(first['candidates'])==2
    chosen=await text(rig,wording,'chosen',task_id=a['id'],task_revision=0)
    assert chosen['task']['status']=='cancelled' and chosen['task']['id']==a['id']
    assert rig.store.task(b['id'])['status']=='awaiting_input'


async def test_notification_response_cannot_create_new_work(focus_rig):
    rig=focus_rig
    rig.bridge.response_notices['notice-response']='plan:1'
    await rig.bridge.handle_tool({'call_id':'invented-from-notice','response_id':'notice-response','name':'route_vehicle_input','arguments':json.dumps({'text':'每天8点查询空调温度'})})
    output=next(m['item'] for m in rig.sent if m.get('item',{}).get('type')=='function_call_output')
    assert json.loads(output['output']).get('error')
    assert rig.manager.plans.list(rig.sid)==[] and rig.store.tasks(rig.sid)==[]


async def test_completed_notice_is_settled_before_next_user_response(focus_rig):
    rig=focus_rig
    t,_=rig.store.create_task(rig.sid,'test','notice-test',source='fixture')
    rig.store.finish(t['id'],'completed','test result')
    event=rig.store.events(rig.sid,pending=True)[0]
    b=rig.bridge;b.inflight=event['id'];b.notification_response_done=True;b.gate.ready=True
    await b.settle_notification()
    assert b.inflight is None and not b.notification_response_done
    assert not rig.store.notification_current(rig.sid,event['id'])


async def test_full_goal_reference_distinguishes_same_object_tasks(focus_rig):
    rig=focus_rig
    a=rig.manager.submit(rig.sid,'空调设为23度','temp23')
    b=rig.manager.submit(rig.sid,'空调设为24度','temp24')
    rig.store.ask(a['id'],'哪个分区？',0);rig.store.ask(b['id'],'哪个分区？',0)
    response=await text(rig,'之前空调设为23度那个任务，取消','cancel23')
    assert response['task']['id']==a['id'] and response['task']['status']=='cancelled'
    assert rig.store.task(b['id'])['status']=='awaiting_input'
