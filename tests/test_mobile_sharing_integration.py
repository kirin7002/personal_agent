"""Two independent browser/device credentials against the same application."""
import json
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.config import Settings
from app.hermes import TaskManager
from app.main import create_app
from app.realtime import VoiceBridge


class LocalManager(TaskManager):
    def launch(self, task):
        # Policy/storage/routes are real; model execution is tested separately.
        self.store.event(task['id'],'test.accepted',{})


@pytest.fixture
def devices(tmp_path):
    settings=Settings(db_path=tmp_path/'shared.db',hermes_key='',dashscope_key='',allowed_hosts=('localhost','127.0.0.1','phone.local'))
    app=create_app(settings,manager_factory=LocalManager)
    with TestClient(app,base_url='http://localhost') as desktop:
        assert desktop.post('/api/session',json={'name':'电脑','kind':'desktop'}).status_code==200
        phone=TestClient(app,base_url='https://phone.local',client=('192.168.1.50',1234))
        phone.portal=desktop.portal
        response=phone.post('/api/session',json={'name':'手机','kind':'mobile'})
        assert response.status_code==200 and response.json()['pairing_required']
        yield app,desktop,phone
        phone.close()


def pair(desktop,phone):
    code=desktop.post('/api/pairing/code').json()['code']
    response=phone.post('/api/pairing/redeem',json={'code':code})
    assert response.status_code==200,response.text
    return response.json()['device']


def send(client,text,key,**extra):
    response=client.post('/api/conversation/input',json={'text':text,'event_id':key,**extra})
    assert response.status_code==200,response.text
    return response.json()


def messages(client):
    response=client.get('/api/conversation/messages')
    assert response.status_code==200,response.text
    return response.json()


def test_unpaired_phone_cannot_borrow_legacy_cookie_or_call_model(devices):
    app,desktop,phone=devices
    phone.cookies.set('preview_session',desktop.cookies.get('preview_session'))
    assert phone.get('/api/tasks').status_code==403
    assert phone.post('/api/conversation/input',json={'text':'打开车窗','event_id':'attack'}).status_code==403
    assert phone.get('/api/conversation/messages').status_code==403
    pair(desktop,phone)
    assert phone.get('/api/tasks').status_code==200


def test_phone_reply_uses_desktop_task_and_both_read_same_messages(devices):
    app,desktop,phone=devices;device=pair(desktop,phone)
    task=send(desktop,'空调设为23度','desktop-input')['task']
    app.state.store.ask(task['id'],'哪个分区？',task['revision'])
    before=messages(phone)
    assert any(m['kind']=='task.question' for m in before['messages'])
    answer=send(phone,'全部','phone-answer')
    assert answer['task']['id']==task['id'] and answer['task']['revision']==1
    again=send(phone,'全部','phone-answer')
    assert again['task']['revision']==1
    d=messages(desktop);m=messages(phone)
    assert d==m
    users=[x for x in d['messages'] if x['role']=='user']
    assert [x['text'] for x in users]==['空调设为23度','全部']
    assert users[-1]['device_id']==device['id']
    assert len(desktop.get('/api/tasks').json()['tasks'])==1
    assert any(x['kind']=='task.question' and x['superseded'] for x in d['messages'])


def test_phone_task_desktop_cancel_and_refresh_replay(devices):
    app,desktop,phone=devices;pair(desktop,phone)
    task=send(phone,'打开车窗','mobile-request')['task']
    app.state.store.ask(task['id'],'哪个车窗？',0)
    snapshot=messages(desktop)
    response=send(desktop,'算了','desktop-cancel')
    assert response['task']['id']==task['id'] and response['task']['status']=='cancelled'
    phone_view=messages(phone)
    assert sum(x['role']=='user' and x['text']=='打开车窗' for x in phone_view['messages'])==1
    assert any(x['kind']=='task.cancelled' for x in phone_view['messages'])
    assert phone_view==messages(phone)
    sid=desktop.cookies.get('preview_session')
    delta=app.state.shared.events(sid,snapshot['cursor'])
    assert any(e['kind']=='message.upsert' and e['message']['kind']=='task.cancelled' for e in delta['events'])


def test_reading_results_does_not_acknowledge_audio(devices):
    app,desktop,phone=devices;pair(desktop,phone)
    task=send(phone,'打开车窗','notice')['task']
    app.state.store.finish(task['id'],'failed','测试失败原因')
    sid=desktop.cookies.get('preview_session')
    pending=app.state.store.events(sid,pending=True)
    messages(desktop);messages(phone)
    assert app.state.store.events(sid,pending=True)==pending


def test_revoke_blocks_old_device_even_with_valid_session_cookie(devices):
    app,desktop,phone=devices;device=pair(desktop,phone)
    send(desktop,'打开车窗','private')
    assert desktop.delete('/api/devices/'+device['id']).status_code==200
    for url in ('/api/tasks','/api/conversation/messages','/api/devices','/api/voice-owner'):
        assert phone.get(url).status_code==401
    assert phone.post('/api/session').status_code==401
    assert phone.post('/api/session/reset').status_code==200
    assert phone.post('/api/session').json()['pairing_required']
    assert phone.get('/api/tasks').status_code==403


def test_device_cannot_revoke_foreign_conversation(devices):
    app,desktop,phone=devices
    foreign=phone.post('/api/session').json()['device']
    assert desktop.delete('/api/devices/'+foreign['id']).status_code==404


def test_remote_http_and_host_spoof_do_not_grant_local_access(devices):
    app,desktop,phone=devices
    remote=TestClient(app,base_url='http://localhost',client=('192.168.1.60',2222));remote.portal=desktop.portal
    assert remote.post('/api/session').status_code==403
    assert remote.get('/api/health').status_code==200
    remote.close()


def test_cross_device_dialog_and_plan_management(devices):
    app,desktop,phone=devices;pair(desktop,phone)
    assert send(phone,'每天早上查询空调温度','plan-missing')['kind']=='clarify'
    plan=send(desktop,'早上8点','plan-time')['plan']
    assert plan['schedule']['time']=='08:00'
    assert send(phone,'取消这个计划','plan-cancel')['kind']=='clarify'
    assert send(desktop,'整个计划','scope')['plan']['status']=='cancelled'
    assert messages(phone)==messages(desktop)
    assert any(m['plan_id']==plan['id'] for m in messages(desktop)['messages'])


def test_voice_owner_explicit_takeover(devices,monkeypatch):
    app,desktop,phone=devices;device=pair(desktop,phone)
    async def idle(self):
        await self.browser.send_json({'type':'voice.ready'})
        try:
            while True:await self.browser.receive_json()
        except WebSocketDisconnect:pass
    monkeypatch.setattr(VoiceBridge,'run',idle)
    with desktop.websocket_connect('ws://localhost/ws/voice') as first:
        assert first.receive_json()['type']=='voice.ready'
        owner=phone.get('/api/voice-owner').json()['owner']
        assert owner['name']=='电脑'
        with phone.websocket_connect('wss://phone.local/ws/voice') as blocked:
            assert blocked.receive_json()['type']=='voice.busy'
        assert phone.post('/api/voice/takeover').status_code==200
        assert first.receive_json()['type']=='voice.released'
        with phone.websocket_connect('wss://phone.local/ws/voice') as second:
            assert second.receive_json()['type']=='voice.ready'
            assert desktop.get('/api/voice-owner').json()['owner']['device_id']==device['id']


def test_pair_code_reuse_is_rejected(devices):
    app,desktop,phone=devices
    code=desktop.post('/api/pairing/code').json()['code']
    assert phone.post('/api/pairing/redeem',json={'code':code}).status_code==200
    assert phone.post('/api/pairing/redeem',json={'code':code}).status_code==409


async def test_revocation_while_waiting_for_send_lock_drops_audio(devices):
    import asyncio
    app,desktop,phone=devices;pair(desktop,phone)
    class Browser:
        def __init__(self):self.messages=[]
        async def send_json(self,value):self.messages.append(value)
    b=Browser();sid=desktop.cookies.get('preview_session')
    bridge=VoiceBridge(b,sid,Settings(),app.state.store,app.state.manager)
    valid=True;bridge.binding_valid=lambda:valid
    await bridge.send_lock.acquire()
    pending=asyncio.create_task(bridge.browser_send({'type':'audio','audio':'AAAA'}))
    await asyncio.sleep(0)
    valid=False;bridge.send_lock.release()
    await pending
    await bridge.progress('late','late progress')
    assert b.messages==[]


async def test_terminal_voice_reply_does_not_duplicate_canonical_outcome(devices):
    app,desktop,phone=devices;pair(desktop,phone)
    sid=desktop.cookies.get('preview_session')
    task,_=app.state.store.create_task(sid,'查询温度','voice-result',source='fixture')
    app.state.store.finish(task['id'],'completed','模拟温度22度')
    class Browser:
        async def send_json(self,value):pass
    bridge=VoiceBridge(Browser(),sid,Settings(),app.state.store,app.state.manager)
    bridge.response_task_versions['r']=(task['id'],task['revision'])
    bridge.save_reply('r','模拟温度是22度。','completed')
    result=app.state.shared.snapshot(sid)['messages']
    assert [m['kind'] for m in result]==['task.completed']


def test_reset_revokes_the_old_credential_not_only_the_cookie(devices):
    app,desktop,phone=devices;pair(desktop,phone)
    token=phone.cookies.get('preview_device')
    assert phone.post('/api/session/reset').status_code==200
    with pytest.raises(PermissionError):app.state.devices.authenticate(token)
    assert phone.post('/api/session').json()['pairing_required']
