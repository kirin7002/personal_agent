"""Model-output boundaries: valid JSON must still respect the user's request."""
import uuid

import pytest

from app.capability_catalog import CapabilityCatalog
from app.capability_tools import CapabilityTools
from app.store import Store


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / 'test.db')
    catalog = CapabilityCatalog(store)
    return store, catalog, CapabilityTools(store, catalog)


def task(rig, goal):
    store, catalog, _ = rig
    return store.create_task(store.create_session(), goal, uuid.uuid4().hex,
                             catalog_revision=catalog.snapshot().revision,
                             profile_id='demo-ready')[0]


def invoke(rig, current, tool_id, arguments):
    _, catalog, service = rig
    snap = catalog.snapshot(current['catalog_revision'])
    return service.call('call_tools', {
        'task_id': current['id'], 'task_revision': current['revision'],
        'catalog_revision': snap.revision, 'calls': [{
            'call_id': uuid.uuid4().hex, 'tool_id': tool_id,
            'tool_version': snap.tools[tool_id]['version'], 'arguments': arguments,
        }],
    })['results'][0]


@pytest.mark.parametrize('goal,tool_id,args', [
    ('空调风量调到最大', 'control.climate.set_fan_speed', {'zone': 'all', 'level': 7}),
    ('打开车窗', 'control.windows.open', {'window': 'all'}),
    ('座椅通风设为二档', 'control.seats.set_ventilation', {'seat': 'driver', 'level': 2}),
])
def test_schema_valid_guessed_scope_pauses_without_receipt(rig, goal, tool_id, args):
    current = task(rig, goal)
    result = invoke(rig, current, tool_id, args)
    assert result['error']['code'] == 'NEEDS_USER_INPUT'
    saved = rig[0].task(current['id'])
    assert saved['status'] == 'awaiting_input' and saved['calls'] == []
    assert sum(e['kind'] == 'task.question' for e in rig[0].task_events(current['id'])) == 1


def test_relative_control_requires_related_query_in_current_task(rig):
    current = task(rig, '主驾空调风量增加一档')
    args = {'zone': 'driver', 'level': 3}
    assert invoke(rig, current, 'control.climate.set_fan_speed', args)['error']['code'] == 'NEEDS_RELATIVE_QUERY'
    invoke(rig, current, 'status.windows.get_state', {})
    assert invoke(rig, current, 'control.climate.set_fan_speed', args)['error']['code'] == 'NEEDS_RELATIVE_QUERY'
    other = task(rig, '查询空调状态')
    invoke(rig, other, 'status.climate.get_state', {})
    assert invoke(rig, current, 'control.climate.set_fan_speed', args)['error']['code'] == 'NEEDS_RELATIVE_QUERY'
    invoke(rig, current, 'status.climate.get_state', {})
    assert invoke(rig, current, 'control.climate.set_fan_speed', args)['status'] == 'prepared'


def test_partial_seat_coverage_cannot_finish_as_all(rig):
    current = task(rig, '全部座椅通风设为二档')
    receipts = [invoke(rig, current, 'control.seats.set_ventilation', {'seat': 'driver', 'level': 2})]
    def finish():
        return rig[2].call('finish_task', {
            'task_id': current['id'], 'task_revision': current['revision'],
            'summary': '模拟通风参数已生成', 'result_ids': [r['result_id'] for r in receipts],
        })
    assert finish()['error']['code'] == 'CONTROL_TARGET_INCOMPLETE'
    assert rig[0].task(current['id'])['status'] == 'running'
    for seat in ['passenger', 'rear_left', 'rear_right']:
        receipts.append(invoke(rig, current, 'control.seats.set_ventilation', {'seat': seat, 'level': 2}))
    assert finish()['status'] == 'generated'


def test_valid_but_different_target_is_rejected_and_invalid_schema_keeps_error(rig):
    current = task(rig, '主驾空调风量设为三档')
    assert invoke(rig, current, 'control.climate.set_fan_speed', {'zone': 'driver', 'level': 2})['error']['code'] == 'OUTDATED_TARGET'
    assert invoke(rig, current, 'control.climate.set_fan_speed', {'zone': 'driver', 'level': 99})['error']['code'] == 'INVALID_ARGUMENTS'
    assert rig[0].task(current['id'])['calls'] == []
