"""Real text/S2S follow-up tests plus deterministic multi-question UI fixtures."""
import json
from pathlib import Path
import sys
import wave
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.store import Store
from app.capability_catalog import CapabilityCatalog

OUT=ROOT/'test-results';OUT.mkdir(exist_ok=True)
silent=OUT/'clarification-silence.wav'
with wave.open(str(silent),'wb') as w:
 w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0'*(32000*180))

with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',f'--use-file-for-fake-audio-capture={silent}'])
 errors=[]
 def open_page(voice=False):
  ctx=browser.new_context(viewport={'width':1440,'height':1050},permissions=['microphone'])
  page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded');expect(page.locator('#catalogCount')).to_have_text('68 项')
  return ctx,page
 def start_question(page):
  goal='空调调到23度，请先确认分区'
  page.locator('#message').fill(goal);page.locator('#sendButton').click()
  row=page.locator('.task').last;expect(row.locator('.task-status')).to_have_text('等待补充',timeout=90000)
  return row.get_attribute('data-task-id'),goal
 def check_result(ctx,page,tid,goal):
  row=page.locator(f'.task[data-task-id="{tid}"]');expect(row.locator('.task-status')).to_have_text('模拟成功',timeout=120000)
  tasks=ctx.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks']
  assert len(tasks)==1 and tasks[0]['id']==tid and tasks[0]['goal']==goal
  task=tasks[0];assert task['revision']==1 and len(task['history'])==1 and '全部' in task['history'][0]['answer']
  assert task['calls'][0]['arguments']=={'zone':'all','temperature_c':23}
  return row

 # Plain text: no manual reply click, even after a page reload.
 ctx,page=open_page();tid,goal=start_question(page)
 page.reload(wait_until='domcontentloaded')
 expect(page.locator('#replyContext')).to_be_visible();expect(page.locator('#replyLabel')).to_contain_text('回复此任务')
 page.locator('#message').fill('全部');page.locator('#sendButton').click()
 row=check_result(ctx,page,tid,goal);expect(page.locator('#replyContext')).to_be_hidden()
 page.screenshot(path=str(OUT/'clarification-text.png'))
 print('Text passed: reload → automatic reply target → 全部 → same task, goal, and correct parameters.',flush=True)
 ctx.close()

 # Reconnected S2S must know a question that was asked before this connection.
 ctx,page=open_page(voice=True);tid,goal=start_question(page);frames=[]
 def receive(frame):
  e=json.loads(frame);frames.append(e['type'])
 page.on('websocket',lambda ws:ws.on('framereceived',receive))
 page.locator('#voiceButton').click();expect(page.locator('#voiceStatus')).to_have_text('已连接',timeout=30000)
 expect(page.locator('.voice-process')).to_contain_text('恢复待答任务上下文')
 page.wait_for_timeout(1500)
 assert not any(t in ['assistant.delta','audio'] for t in frames),frames
 page.locator('#message').fill('全部');page.locator('#sendButton').click()
 row=check_result(ctx,page,tid,goal)
 expect(page.locator('.voice-process')).to_contain_text('将补充信息交回原任务')
 expect(page.locator('.voice-process')).to_contain_text('任务结果已回传并播报',timeout=45000)
 page.screenshot(path=str(OUT/'clarification-s2s.png'))
 page.locator('#voiceButton').click();ctx.close()
 print('S2S passed: pending context restored silently → 全部 → answer original task → spoken completion.',flush=True)

 # Multiple pending questions are UI fixtures, no fake model success is claimed.
 ctx,page=open_page();sid=next(c['value'] for c in ctx.cookies() if c['name']=='preview_session')
 store=Store(ROOT/'.data/preview.sqlite3');cat=CapabilityCatalog(store);pending=[]
 for goal,question in [('空调调到23度','哪个分区？'),('主驾座椅加热','几档？')]:
  t,_=store.create_task(sid,goal,'fixture-'+goal,source='fixture',catalog_revision=cat.snapshot().revision,profile_id='demo-ready');store.ask(t['id'],question);pending.append(t)
 page.reload(wait_until='domcontentloaded');expect(page.locator('.reply-choice')).to_have_count(2)
 page.locator('#message').fill('全部');page.locator('#sendButton').click();expect(page.locator('#toast')).to_contain_text('请选择')
 assert len(store.tasks(sid))==2
 posted=[]
 def capture(route):
  posted.append(route.request.post_data_json);route.fulfill(status=202,json={'id':pending[0]['id']})
 page.route('**/api/tasks/'+pending[0]['id']+'/answer',capture)
 page.locator('.reply-choice').filter(has_text='空调调到23度').click();page.locator('#sendButton').click()
 expect(page.locator('#message')).to_have_value('')
 assert posted==[{'answer':'全部','task_revision':0}]
 # Explicit new-task control is respected, rather than binding another request to the old question.
 page.locator(f'.task[data-task-id="{pending[1]["id"]}"]').get_by_role('button',name='补充信息 →').click()
 page.locator('#clearReply').click();page.locator('#message').fill('打开空调AC制冷')
 created=[]
 page.route('**/api/tasks',lambda route:(created.append(route.request.post_data_json),route.fulfill(status=202,json={'id':'fixture-new'})) if route.request.method=='POST' else route.continue_())
 page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
 assert len(created)==1 and created[0]['goal']=='打开空调AC制冷'
 assert all(store.task(t['id'])['history']==[] for t in pending)
 for t in pending:store.cancel(t['id'],sid)
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert not errors,errors
 (OUT/'clarification-check.json').write_text(json.dumps({'text_same_task':True,'voice_same_task':True,'multi_question_ui_fixture':True,'new_task_choice':True,'javascript_errors':errors},ensure_ascii=False,indent=2))
 browser.close();print('Clarification routing passed, including multiple-question selection and explicit new task.',flush=True)
