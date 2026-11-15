import json

import pytest

from app.config import Settings
from app.store import Store
from app.capability_catalog import CapabilityCatalog
from app.hermes import TaskManager
from app.realtime import VoiceBridge


class Browser:
 def __init__(self):self.messages=[]
 async def send_json(self,e):self.messages.append(e)


def pending(store,cat,sid,key='one'):
 t,_=store.create_task(sid,'空调调到23度',key,catalog_revision=cat.snapshot().revision,profile_id='demo-ready')
 store.ask(t['id'],'哪个分区：主驾、副驾、后排还是全部？')
 return store.task(t['id'])


def test_resume_preserves_task_goal_and_rejects_stale_question(tmp_path):
 store=Store(tmp_path/'test.db');cat=CapabilityCatalog(store);sid=store.create_session();t=pending(store,cat,sid)
 resumed=store.resume(t['id'],sid,'全部',expected_revision=0)
 assert resumed['id']==t['id'] and resumed['goal']=='空调调到23度' and resumed['revision']==1
 assert resumed['history']==[{'question':t['question'],'answer':'全部'}]
 assert len(store.tasks(sid))==1
 store.ask(t['id'],'请再确认目标温度',revision=1)
 with pytest.raises(ValueError,match='追问已更新'):store.resume(t['id'],sid,'全部',expected_revision=0)
 assert len(store.task(t['id'])['history'])==1
 with pytest.raises(KeyError):store.resume(t['id'],store.create_session(),'全部',expected_revision=1)
 store.cancel(t['id'],sid)
 with pytest.raises(ValueError):store.resume(t['id'],sid,'全部',expected_revision=1)


async def test_s2s_restores_pending_context_silently_and_answers_original_task(tmp_path):
 store=Store(tmp_path/'test.db');cat=CapabilityCatalog(store);sid=store.create_session();t=pending(store,cat,sid)
 manager=TaskManager(store,cat,Settings(db_path=store.path,hermes_key=''))
 launched=[];manager.launch=lambda task:launched.append(task['id'])
 bridge=VoiceBridge(Browser(),sid,Settings(),store,manager);sent=[]
 async def send(e):sent.append(e)
 bridge.send=send
 async def updates():yield json.dumps({'type':'session.updated'})
 bridge.upstream=updates();await bridge.receive_model()
 assert [e['type'] for e in sent]==['conversation.item.create']  # No unsolicited response.create.
 context=json.loads(sent[0]['item']['content'][0]['text'])['pending_task_context']
 assert context['tasks'][0]['task_id']==t['id'] and context['tasks'][0]['task_revision']==0
 await bridge.handle_tool({'call_id':'answer-one','name':'answer_vehicle_task','arguments':json.dumps({'task_id':t['id'],'answer':'全部'})})
 assert launched==[t['id']] and len(store.tasks(sid))==1
 assert store.task(t['id'])['history'][0]['answer']=='全部'
 assert store.task(t['id'])['goal']=='空调调到23度'
 await bridge.handle_tool({'call_id':'answer-one','name':'answer_vehicle_task','arguments':json.dumps({'task_id':t['id'],'answer':'全部'})})
 assert len(store.task(t['id'])['history'])==1
 await manager.close()


async def test_s2s_stale_revision_cannot_answer_new_question(tmp_path):
 store=Store(tmp_path/'test.db');cat=CapabilityCatalog(store);sid=store.create_session();t=pending(store,cat,sid)
 store.resume(t['id'],sid,'全部',0);store.ask(t['id'],'新的问题',1)
 manager=TaskManager(store,cat,Settings(db_path=store.path,hermes_key=''))
 bridge=VoiceBridge(Browser(),sid,Settings(),store,manager);sent=[]
 async def send(e):sent.append(e)
 bridge.send=send
 await bridge.handle_tool({'call_id':'stale','name':'answer_vehicle_task','arguments':json.dumps({'task_id':t['id'],'task_revision':0,'answer':'旧答案'})})
 assert json.loads(sent[0]['item']['output'])['error']=='invalid_task_request'
 assert len(store.task(t['id'])['history'])==1 and store.task(t['id'])['status']=='awaiting_input'
 await manager.close()
