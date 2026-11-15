import json

from fastapi.testclient import TestClient

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.config import ROOT, Settings
from app.main import create_app
from app.store import Store
from app.task_presentation import build_process


def event(i, kind, at, **data):
    return dict(id=i, kind=kind, created=at, data=data)


def task(status='generated', end=104):
    return dict(created=100, updated=end, status=status)


def step(i, at, duration=10, revision=0, **extra):
    return event(i, 'agent.step', at + duration/1000, started_at=at,
                 duration_ms=duration, task_revision=revision, title='调用工具',
                 detail='已完成参数校验', status='success', **extra)


def test_precise_timers_separate_remote_wait_and_tool_execution():
    result = build_process(task(end=104.01), [
        event(1, 'catalog.preloaded', 100.002, elapsed_ms=1.2, tool_ids=['a']),
        event(2, 'task.dispatched', 100.02), step(3, 102.3), step(4, 104),
        event(5, 'provider.reasoning', 102, reasoning='DO_NOT_SHOW'),
    ])
    assert result['timing'] == dict(total_ms=4010, local_measured_ms=21.2,
        model_service_interval_ms=3970, human_wait_ms=0, precise_tool_timers=True, direct_completion=False, provider_requests=None)
    assert [s['duration_ms'] for s in result['steps'] if s['duration_kind']=='boundary_wait'] == [2280,1690]
    assert 'DO_NOT_SHOW' not in json.dumps(result)
    assert all(s['duration_ms'] >= 0 for s in result['steps'])


def test_legacy_records_are_stage_intervals_not_tool_execution():
    result = build_process(task(), [event(1, 'task.dispatched', 100.02),
        event(2, 'tool.preview', 102.3, tool_id='test'),
        event(3, 'task.generated', 104)])
    assert not result['timing']['precise_tool_timers']
    assert [s['duration_kind'] for s in result['steps']] == ['stage_interval']*2
    assert [s['duration_ms'] for s in result['steps']] == [2280,1700]


def test_resume_live_timer_excludes_user_pause():
    events = [event(1, 'task.dispatched', 100.02), step(2, 101),
              event(3, 'task.question', 101.01), event(4, 'task.resumed', 120, task_revision=1),
              event(5, 'task.dispatched', 120.02, task_revision=1)]
    result = build_process(task('running', 120), events)
    live = next(s for s in result['steps'] if s['live'])
    assert live['started_at'] == 120.02
    assert live['duration_ms'] is None
    assert result['timing']['human_wait_ms'] == 18990
    events.extend([step(6, 122, revision=1), step(7, 123, revision=1)])
    result = build_process(task(end=123.01), events)
    assert result['timing']['model_service_interval_ms'] == 3950
    assert result['timing']['human_wait_ms'] == 18990


def test_cancellation_during_question_is_user_wait():
    result = build_process(task('cancelled', 120), [
        event(1, 'task.dispatched', 100.02), step(2, 101), event(3, 'task.question', 101.01)])
    assert result['timing']['human_wait_ms'] == 18990
    assert result['timing']['model_service_interval_ms'] == 980
    assert not any(s['live'] for s in result['steps'])


def test_real_tool_events_include_timers_and_safe_result_summary(tmp_path):
    store = Store(tmp_path/'test.db')
    catalog = CapabilityCatalog(store, ROOT/'capabilities')
    sid = store.create_session()
    task_, _ = store.create_task(sid, '查询', 'one', catalog_revision=catalog.snapshot().revision, profile_id='demo-ready')
    service = CapabilityTools(store, catalog)
    response = service.call('browse_capabilities', {'task_id': task_['id'], 'task_revision': 0})
    assert len(response['items']) == 3
    e = store.task_events(task_['id'])[-1]
    assert e['kind'] == 'agent.step' and e['data']['duration_ms'] >= 0
    assert e['data']['title'] == '浏览能力目录'
    assert e['data']['detail'] == '根目录 · 返回 3 项'
    store.cancel(task_['id'], sid)
    assert service.call('browse_capabilities', {'task_id': task_['id'], 'task_revision': 0})['error']['code'] == 'INVALID_CONTEXT'


def test_process_http_projection_and_event_cursor_isolation(tmp_path):
    app = create_app(Settings(db_path=tmp_path/'web.db', hermes_key='', dashscope_key=''))
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get('/api/events/cursor').status_code == 401
        assert client.get('/api/events/stream').status_code == 401
        client.post('/api/session')
        assert client.get('/api/events/cursor').json()['cursor'] == 0
        result = client.post('/api/examples/control.climate.set_temperature').json()
        process = result['process']
        assert process['kind'] == 'execution_summary'
        assert process['timing']['precise_tool_timers']
        assert len([s for s in process['steps'] if s['duration_kind']=='measured_tool']) == 2
        assert client.get('/api/events/cursor').json()['cursor'] > 0
        assert client.get('/api/events/stream', headers={'last-event-id':'bad'}).status_code == 422
        client.cookies.clear()
        client.post('/api/session')
        assert client.get('/api/events/cursor').json()['cursor'] == 0
        assert client.get('/api/tasks').json()['tasks'] == []


def test_legacy_cancel_does_not_count_human_pause_as_model_wait():
    result = build_process(task('cancelled', 120), [
        event(1, 'task.dispatched', 100.02), event(2, 'task.question', 101.01),
        event(3, 'task.cancelled', 120)])
    assert result['timing']['human_wait_ms'] == 18990
    assert result['timing']['model_service_interval_ms'] == 990
