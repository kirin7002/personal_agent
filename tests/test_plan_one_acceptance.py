"""Plan-one functional acceptance: actual adapters, controlled remote boundary."""
import asyncio
import json
import time
import pytest

from test_running_steer_validation import rig, task_for, call_args, finish, rejected, until
from app.task_view import task_view
from app.realtime import FRONT_INSTRUCTIONS


async def test_timer_wait_has_no_model_calls_then_resumes_same_task(rig):
    task = rig.manager.submit(rig.sid,'10秒后查询空调温度','timer')
    assert task['status']=='waiting'
    assert not rig.requests
    wait = rig.manager.loop_events.waits(task['id'],rig.sid)[0]
    assert wait['deadline'] == task['created']+10
    await asyncio.sleep(.15)
    assert not rig.requests
    assert rig.manager.loop_events.consume_due(now=wait['deadline']-.01)==[]
    resumed = rig.manager.loop_events.consume_due(now=wait['deadline'])
    assert len(resumed)==1 and resumed[0]['id']==task['id']
    assert rig.manager.loop_events.consume_due(now=wait['deadline']+1)==[]
    rig.manager.launch(resumed[0])
    await until(lambda: rig.store.task(task['id'])['run_id'] is not None)
    assert len(rig.store.tasks(rig.sid))==1
    start = next(body for method,path,body in rig.requests if method=='POST' and path=='/v1/runs')
    content = json.loads(start['input'])
    assert content['clarifications'][-1]['kind']=='timer_due'
    assert content['clarifications'][-1]['continuation']=='查询空调温度'


async def test_wait_tool_exits_run_without_model_spin_and_cancel_invalidates(rig):
    task = await task_for(rig)
    result = rig.service.call('wait_for_event',{'task_id':task['id'],'task_revision':0,'wait_id':'timer','seconds':10,'continuation':'查询空调温度'})
    assert result['status']=='waiting'
    await until(lambda: task['id'] not in rig.manager.jobs)
    assert rig.store.task(task['id'])['status']=='waiting'
    await rig.manager.cancel(rig.sid,task['id'])
    assert rig.manager.loop_events.consume_due(now=time.time()+60)==[]
    assert rig.store.task(task['id'])['status']=='cancelled'


async def test_external_result_adapter_matches_reference_and_resumes_once(rig):
    task = await task_for(rig)
    rig.manager.loop_events.register_wait(task,'external_result','external',result_ref='fake-call-one',continuation='解释测试结果')
    await until(lambda: task['id'] not in rig.manager.jobs)
    initial_starts=sum(path=='/v1/runs' for _,path,_ in rig.requests)
    assert await rig.manager.external_result(rig.sid,task['id'],'external','fake-call-one','pending',{}) is None
    with pytest.raises(ValueError):
        await rig.manager.external_result(rig.sid,task['id'],'external','wrong-reference','done',{})
    resumed=await rig.manager.external_result(rig.sid,task['id'],'external','fake-call-one','done',{'test_only':True,'value':21})
    assert resumed['id']==task['id'] and resumed['revision']==1
    assert await rig.manager.external_result(rig.sid,task['id'],'external','fake-call-one','done',{'test_only':True,'value':21}) is None
    await until(lambda: sum(path=='/v1/runs' for _,path,_ in rig.requests)==initial_starts+1)
    history=rig.store.task(task['id'])['history']
    assert history[-1]['result_ref']=='fake-call-one' and history[-1]['status']=='done'


async def test_followup_queue_deduplicates_and_runs_after_original(rig):
    task=await task_for(rig)
    body={'text':'查看空调温度','task_revision':0,'event_id':'followup1'}
    for _ in range(2):
        response=await rig.http.post(f"/api/tasks/{task['id']}/followup",json=body)
        assert response.status_code==202
    await asyncio.sleep(.15)
    assert len(rig.store.tasks(rig.sid))==1 and rig.store.task(task['id'])['goal']==task['goal']
    result=rig.service.call('call_tools',call_args(task))['results'][0]
    finish(rig,task,0,[result['result_id']])
    await until(lambda: len(rig.store.tasks(rig.sid))==2)
    child=next(t for t in rig.store.tasks(rig.sid) if t['id']!=task['id'])
    assert child['parent_task_id']==task['id'] and child['goal']=='查看空调温度'
    assert task_view(rig.store,child)['parent_context']['calls'][0]['arguments']['temperature_c']==23


async def test_cancel_drops_queued_followup(rig):
    task=await task_for(rig)
    rig.manager.append_followup(rig.sid,task['id'],'查看温度',0,'later')
    await rig.manager.cancel(rig.sid,task['id'])
    await asyncio.sleep(.2)
    assert len(rig.store.tasks(rig.sid))==1


async def test_completed_task_new_request_is_linked_and_old_preserved(rig):
    task=await task_for(rig)
    result=rig.service.call('call_tools',call_args(task))['results'][0]
    finish(rig,task,0,[result['result_id']])
    response=await rig.http.post('/api/tasks',json={'goal':'把所有分区空调设21度','request_key':'new-linked','parent_task_id':task['id']})
    assert response.status_code==202
    assert response.json()['id']!=task['id']
    assert response.json()['parent_task_id']==task['id']
    assert rig.store.task(task['id'])['calls'][0]['arguments']['temperature_c']==23


async def test_question_and_full_zone_answer_keep_original_goal(rig):
    task=await task_for(rig,goal='空调调到23度')
    rig.store.ask(task['id'],'哪个分区？',0)
    result=await rig.http.post(f"/api/tasks/{task['id']}/answer",json={'answer':'全部','task_revision':0})
    assert result.status_code==202
    duplicate=await rig.http.post(f"/api/tasks/{task['id']}/answer",json={'answer':'全部','task_revision':0})
    assert duplicate.status_code==202
    current=rig.store.task(task['id'])
    assert current['goal']=='空调调到23度' and current['history']==[{'question':'哪个分区？','answer':'全部'}]


async def test_ambiguous_revision_answer_supplies_zone_without_losing_new_temperature(rig):
    task=await task_for(rig,goal='空调设为23度')
    changed=await rig.manager.revise(rig.sid,task['id'],'改成21度',0,'edit')
    assert changed['status']=='awaiting_input'
    resumed=await rig.manager.resume(rig.sid,task['id'],'全部',1)
    assert resumed['id']==task['id'] and '21度' in resumed['goal'] and '所有分区' in resumed['goal']


def test_front_rules_distinguish_chat_followup_and_waiting():
    assert '谢谢不建任务' in FRONT_INSTRUCTIONS
    assert '做完再' in FRONT_INSTRUCTIONS and 'route_vehicle_input' in FRONT_INSTRUCTIONS
    assert 'task.waiting' in FRONT_INSTRUCTIONS


async def test_model_cannot_guess_all_zones_when_user_has_not_chosen(rig):
    task=await task_for(rig,goal='空调设为23度')
    result=rig.service.call('call_tools',call_args(task))
    assert rejected(result)
    current=rig.store.task(task['id'])
    assert current['status']=='awaiting_input' and current['calls']==[]
    assert rig.store.capability_results(task['id'])==[]
    response=await rig.http.post(f"/api/tasks/{task['id']}/answer",json={'answer':'全部','task_revision':0})
    assert response.status_code==202
    good=rig.service.call('call_tools',call_args(task,23,1,call_id='confirmed'))['results'][0]
    assert good['status']=='prepared'
    assert rejected(rig.service.call('call_tools',call_args(task,23,1,zone='driver',call_id='wrong-zone')))


async def test_old_remote_completion_cannot_fail_resumed_wait_revision(rig):
    task=await task_for(rig)
    rig.manager.loop_events.register_wait(task,'timer','late-poll',delay_seconds=0,continuation='查温度')
    resumed=rig.manager.loop_events.consume_due(now=time.time()+1)[0]
    assert resumed['revision']==1
    assert not rig.manager.finish_run(task['id'],0,'isolated-run','failed','旧run迟到失败')
    assert rig.store.task(task['id'])['status']=='running'
    assert not rig.store.finish(task['id'],'failed','错误旧写',expected_revision=0,expected_run='isolated-run')


async def test_one_followup_failure_does_not_stop_scheduler(rig):
    task=await task_for(rig)
    rig.manager.append_followup(rig.sid,task['id'],'查看温度',0,'bad-child')
    original=rig.manager.dispatch_followup
    def failure(event,parent):
        raise ValueError('controlled failure')
    rig.manager.dispatch_followup=failure
    rig.store.finish(task['id'],'completed','fixture original complete')
    await until(lambda:any(e['kind']=='task.followup_failed' for e in rig.store.task_events(task['id'])))
    assert not rig.manager.scheduler.done()
    rig.manager.dispatch_followup=original
    timer=rig.manager.submit(rig.sid,'0.2秒后查询空调温度','after-failure')
    await until(lambda:rig.store.task(timer['id'])['revision']==1)
    assert not rig.manager.scheduler.done()
