import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
checks=[]
errors=[]

def task(id='one', goal='空调调到23度', status='running', revision=0):
    return dict(id=id,goal=goal,status=status,revision=revision,created=1800000000,source='fixture',history=[],process=dict(steps=[],timing={}),question='哪个分区？' if status=='awaiting_input' else None)

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    def setup(tasks):
        ctx=browser.new_context(viewport={'width':1200,'height':900})
        page=ctx.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
        fixture={'tasks':tasks,'posts':[],'fail':False}
        def route(r):
            path=urlparse(r.request.url).path
            if path=='/': return r.fulfill(content_type='text/html',body=(ROOT/'web/index.html').read_text())
            if path.startswith('/static/'):
                f=ROOT/'web'/path.removeprefix('/static/')
                body=f.read_text() if f.exists() else ''
                if f.name=='app.js': body+='\nwindow.__test = {state, VoiceSession, poll, renderTasks};'
                return r.fulfill(content_type='application/javascript' if f.suffix=='.js' else 'text/css',body=body)
            if r.request.method=='POST' and path.startswith('/api/tasks'):
                data=r.request.post_data_json;fixture['posts'].append((path,data))
                if fixture['fail']: return r.fulfill(status=409,json={'detail':'任务版本已变化，请重试'})
                if path.endswith('/followup'):
                    t=next(t for t in fixture['tasks'] if t['id']==path.split('/')[3])
                    if fixture.get('invalid_ack'): return r.fulfill(json={'status':'queued','task_id':'wrong','event_id':data['event_id']})
                    t['process']['steps'].append(dict(id=data['event_id'],title='收件箱：排队追加',detail='已排队，原任务结束后处理追加请求',started_at=1800000000,duration_ms=0,duration_kind='instant',status='success',live=False))
                    return r.fulfill(json={'status':'queued','task_id':t['id'],'event_id':data['event_id']})
                if path.endswith('/cancel'):
                    t=next(t for t in fixture['tasks'] if t['id']==path.split('/')[3]);t['status']='cancelled'
                    return r.fulfill(json=t)
                if path.endswith('/revise'):
                    t=next(t for t in fixture['tasks'] if t['id']==path.split('/')[3])
                    t.update(revision=t['revision']+1,latest_change={'status':fixture.get('change_status','accepted'),'text':data['text']})
                    if fixture.get('change_status')=='needs_input': t.update(status='awaiting_input',question='请说明要修改的对象')
                    if fixture.get('change_status') in ('rejected','unconfirmed'): t.update(status='interrupted')
                    return r.fulfill(json=t)
                if path.endswith('/answer'):
                    t=next(t for t in fixture['tasks'] if t['id']==path.split('/')[3]);t.update(status='running',question=None,revision=t['revision']+1)
                    return r.fulfill(json=t)
                if path=='/api/tasks':
                    t=task('new',data['goal']);fixture['tasks'].insert(0,t);return r.fulfill(json=t)
            values={
                '/api/session':{},'/api/config':{'hermes_configured':False,'voice_configured':False},
                '/api/catalog':{'profiles':[{'id':'demo','title':'演示'}],'scenarios':[],'counts':{'control':1,'manual':0,'status':0}},
                '/api/session/profile':{'profile_id':'demo'},'/api/catalog/tree':{'items':[],'next_offset':None},
                '/api/events/cursor':{'cursor':0},'/api/tasks':{'tasks':fixture['tasks']}}
            if path=='/api/events/stream': return r.fulfill(content_type='text/event-stream',body=': test\n\n')
            return r.fulfill(json=values.get(path,{}))
        page.route('**/*',route)
        page.goto('http://127.0.0.1:18765/')
        page.wait_for_function('window.__test?.state.initialized')
        return ctx,page,fixture
    def submit(page,text):
        page.locator('#message').fill(text);page.locator('#sendButton').click();page.wait_for_function('!window.__test.state.busy')
    def done(ctx,label):
        checks.append(label);ctx.close();print('PASS '+label,flush=True)

    ctx,page,f=setup([task()])
    page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click()
    expect(page.locator('#reviseLabel')).to_contain_text('空调调到23度')
    submit(page,'改为全车21度')
    assert len(f['posts'])==1 and f['posts'][0][0]=='/api/tasks/one/revise'
    body=f['posts'][0][1];assert body['text']=='改为全车21度' and body['task_revision']==0 and len(body['change_id'])==36
    expect(page.locator('.task')).to_have_count(1);expect(page.locator('.task-change')).to_contain_text('已接收')
    expect(page.locator('#message')).to_have_value('');expect(page.locator('#reviseContext')).to_be_hidden()
    done(ctx,'explicit revise: original task, exact request fields, returned revision/change status')

    ctx,page,f=setup([task()]);submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/one/revise';done(ctx,'one running task: simple correction auto-associates')

    for text in ['把空调改成21度','打开车窗','改成21度并打开车窗']:
        ctx,page,f=setup([task()]);submit(page,text)
        assert f['posts'][0][0]=='/api/tasks';done(ctx,'ordinary request stays new: '+text)

    ctx,page,f=setup([]);submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks';done(ctx,'no running task: no revise endpoint')

    ctx,page,f=setup([task(),task('two','音量调到30%')]);submit(page,'改成21度')
    assert not f['posts'];expect(page.locator('#reviseChoices button')).to_have_count(2)
    expect(page.locator('#message')).to_have_value('改成21度')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.wait_for_timeout(250)
    assert page.locator('#sendButton').bounding_box()['y'] < 844
    page.screenshot(path='/tmp/personal-agent-revise-mobile.png')
    page.locator('#reviseChoices button').filter(has_text='空调').click();page.locator('#sendButton').click()
    expect(page.locator('#message')).to_have_value('');assert f['posts'][0][0]=='/api/tasks/one/revise'
    done(ctx,'multiple running tasks: explicit selection required; mobile has no overflow')

    ctx,page,f=setup([task(),task('two','音量调到30%')]);submit(page,'改成21度')
    page.locator('#clearRevise').click();page.locator('#sendButton').click()
    expect(page.locator('#message')).to_have_value('');assert f['posts'][0][0]=='/api/tasks'
    done(ctx,'new-task escape from ambiguous correction')

    ctx,page,f=setup([task()]);page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click()
    f['fail']=True;submit(page,'改成21度');expect(page.locator('#message')).to_have_value('改成21度')
    expect(page.locator('#reviseContext')).to_be_visible();expect(page.locator('#toast')).to_contain_text('版本已变化')
    first=f['posts'][0][1];f['fail']=False;page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
    assert f['posts'][1][1]['change_id']==first['change_id'];done(ctx,'API failure preserves draft/target; same retry reuses change_id')

    ctx,page,f=setup([task()]);page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click()
    f['fail']=True;submit(page,'改成21度');first=f['posts'][0][1];submit(page,'改成22度')
    assert f['posts'][1][1]['change_id']!=first['change_id'];done(ctx,'changed draft gets distinct change_id')

    for change in [{'revision':1},{'status':'completed'},{'status':'waiting'}]:
        ctx,page,f=setup([task()]);page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click()
        f['tasks'][0].update(change);submit(page,'改成21度')
        assert not f['posts'];expect(page.locator('#message')).to_have_value('改成21度');expect(page.locator('#toast')).to_contain_text('状态或版本已变化')
        done(ctx,'preflight detects changed task: '+str(change))

    ctx,page,f=setup([task(status='awaiting_input'),task('two','座椅加热')])
    page.get_by_role('button',name='补充信息 →').click();submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/one/revise' and f['posts'][0][1]['task_revision']==0
    done(ctx,'selected pending question changes to revise on the same task')

    ctx,page,f=setup([task(status='awaiting_input'),task('two','座椅加热',status='awaiting_input')]);submit(page,'全部')
    assert not f['posts'];expect(page.locator('#replyChoices button')).to_have_count(2)
    page.locator('#replyChoices button').first.click();page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
    assert f['posts'][0][0].endswith('/answer');done(ctx,'multiple awaiting answers retain explicit reply selection')

    ctx,page,f=setup([task(),task('two','座椅加热',status='awaiting_input')])
    page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click();submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/one/revise';done(ctx,'explicit revise overrides another task awaiting an answer')

    for explicit in [True,False]:
        ctx,page,f=setup([task()])
        page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
        if explicit: page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click()
        submit(page,'改成21度')
        assert f['posts'][0][0]=='/api/tasks/one/revise' and page.evaluate('sent.length')==0
        done(ctx,'voice connected: correction uses revise API ('+str(explicit)+')')

    ctx,page,f=setup([task(status='awaiting_input')]);page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
    submit(page,'全部');assert not f['posts'] and page.evaluate('sent')==[{'type':'text','text':'全部'}]
    done(ctx,'voice connected: automatic awaiting answer keeps S2S routing')

    ctx,page,f=setup([task(status='awaiting_input')]);page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
    page.get_by_role('button',name='补充信息 →').click();submit(page,'全部')
    assert f['posts'][0][0].endswith('/answer') and page.evaluate('sent.length')==0
    done(ctx,'voice connected: manual awaiting answer keeps answer API routing')

    ctx,page,f=setup([])
    result=page.evaluate('''() => {
      const v=new __test.VoiceSession(), sent=[], stopped=[];
      v.send=e=>sent.push(e);
      const queue=() => { for(let i=0;i<2;i++) v.sources.add({onended:()=>{},stop(){stopped.push(this);}});v.playing=true;v.nextAudio=100; };
      v.receive({type:'assistant.delta',text:'完整历史'});v.receive({type:'response.done'});
      v.receive({type:'assistant.delta',text:'过期播报'});queue();v.receive({type:'response.done'});
      v.receive({type:'voice.invalidate',task_id:'one',task_revision:1});
      const cleared=stopped.length===2 && !v.sources.size && !v.currentText && !v.playbackTexts.size && v.nextAudio===0 && !v.playing;
      const history=[...document.querySelectorAll('.bubble')].map(n=>n.textContent);
      v.receive({type:'assistant.delta',text:'新的播报'});queue();v.receive({type:'speech.started'});
      const interruption=[...document.querySelectorAll('.bubble')].map(n=>n.textContent);
      v.receive({type:'assistant.delta',text:'未完成文字'});v.receive({type:'voice.invalidate',task_id:'one',task_revision:2});
      return {cleared,history,interruption,textCleared:!document.querySelector('#tasks').textContent.includes('未完成文字'),sent,stopped:stopped.length};
    }''')
    assert result['cleared'] and result['history']==['完整历史'] and result['interruption']==['完整历史','新的播报'] and result['textCleared']
    assert result['stopped']==4 and all(e=={'type':'playback','busy':False} for e in result['sent'])
    done(ctx,'voice.invalidate stops all queued sources/removes stale text; speech interruption retains transcript')
    for status in ['rejected','unconfirmed','failed']:
        ctx,page,f=setup([task()]);f['change_status']=status
        page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click();submit(page,'改成21度')
        expect(page.locator('#message')).to_have_value('改成21度');expect(page.locator('#reviseContext')).to_be_visible()
        expect(page.locator('.task-change')).to_contain_text('改口：改成21度')
        done(ctx,'HTTP success with '+status+' preserves draft')
    ctx,page,f=setup([task()]);f['change_status']='needs_input'
    page.locator('[data-task-id="one"]').get_by_role('button',name='改口',exact=True).click();submit(page,'改成21度')
    expect(page.locator('#message')).to_have_value('');expect(page.locator('#replyContext')).to_be_visible()
    submit(page,'空调温度');assert f['posts'][-1]==('/api/tasks/one/answer',{'answer':'空调温度','task_revision':1})
    done(ctx,'revise needing clarification resumes existing answer flow at new revision')

    ctx,page,f=setup([task(status='waiting')])
    expect(page.locator('.task-status')).to_have_text('等待时间 / 外部结果')
    expect(page.get_by_role('button',name='取消任务')).to_be_visible()
    assert page.locator('.process').evaluate('(n)=>n.open')
    page.get_by_role('button',name='取消任务').click();expect(page.locator('.task-status')).to_have_text('已取消')
    assert f['posts']==[('/api/tasks/one/cancel',{})]
    done(ctx,'waiting status is distinct, expanded and cancellable')

    ctx,page,f=setup([task()])
    waiting=dict(id='wait-1',title='等待时间 / 外部结果',detail='等待定时器结束',started_at=1800000000,duration_ms=None,duration_kind='external_wait',status='waiting',live=True)
    f['tasks'][0].update(status='waiting',process={'steps':[waiting],'timing':{}})
    page.evaluate("__test.state.events.onmessage({data:JSON.stringify({id:1,kind:'task.waiting'})})")
    expect(page.locator('.task-status')).to_have_text('等待时间 / 外部结果')
    expect(page.locator('.process-step')).to_contain_text('等待定时器结束')
    waiting.update(live=False,duration_ms=1000)
    continued=dict(id='continued-2',title='继续原任务',detail='等待结束，继续检查车辆状态',started_at=1800000001,duration_ms=0,duration_kind='instant',status='success',live=False)
    f['tasks'][0].update(status='running',process={'steps':[waiting,continued],'timing':{}})
    page.evaluate("__test.state.events.onmessage({data:JSON.stringify({id:2,kind:'task.continued'})})")
    expect(page.locator('.task-status')).to_have_text('处理中');expect(page.locator('.process-step')).to_have_count(2)
    expect(page.locator('.process-step').last).to_contain_text('等待结束，继续检查车辆状态')
    done(ctx,'server waiting/continued SSE refreshes real process steps')

    for status in ['running','awaiting_input','waiting']:
        ctx,page,f=setup([task(status=status)])
        original=json.loads(json.dumps(f['tasks'][0]))
        page.get_by_role('button',name='完成后追加',exact=True).click();submit(page,'做完再告诉我空调参数')
        assert len(f['posts'])==1 and f['posts'][0][0]=='/api/tasks/one/followup'
        body=f['posts'][0][1];assert body['text']=='做完再告诉我空调参数' and body['task_revision']==0 and len(body['event_id'])==36
        assert f['tasks'][0]['goal']==original['goal'] and f['tasks'][0]['status']==original['status'] and f['tasks'][0]['revision']==0
        expect(page.locator('.process')).to_contain_text('已排队，原任务结束后处理追加请求');expect(page.locator('#message')).to_have_value('')
        expect(page.locator('.task')).to_have_count(1)
        page.reload();page.wait_for_function('window.__test?.state.initialized');expect(page.locator('.process')).to_contain_text('收件箱：排队追加')
        if status=='waiting':
            page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path='/tmp/personal-agent-plan1-mobile.png')
        done(ctx,'followup queue persists without changing/cancelling '+status+' task')

    ctx,page,f=setup([task(),task('two','打开车窗')])
    page.locator('[data-task-id="two"]').get_by_role('button',name='完成后追加').click();submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/two/followup'
    done(ctx,'explicit followup target wins over natural revise or multiple tasks')

    ctx,page,f=setup([task()]);page.get_by_role('button',name='完成后追加').click();f['fail']=True
    submit(page,'做完告诉我参数');expect(page.locator('#message')).to_have_value('做完告诉我参数')
    expect(page.locator('#reviseLabel')).to_contain_text('完成后追加')
    first=f['posts'][0][1];f['fail']=False;page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
    assert f['posts'][1][1]['event_id']==first['event_id']
    done(ctx,'followup failure preserves draft and target; retry reuses event_id')

    for changed in [{'revision':1},{'status':'completed'}]:
        ctx,page,f=setup([task()]);page.get_by_role('button',name='完成后追加').click()
        f['tasks'][0].update(changed);submit(page,'做完告诉我参数')
        assert not f['posts'];expect(page.locator('#message')).to_have_value('做完告诉我参数')
        done(ctx,'followup preflight preserves draft when task changed '+str(changed))

    ctx,page,f=setup([task()]);page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
    page.get_by_role('button',name='完成后追加').click();submit(page,'做完告诉我参数')
    assert f['posts'][0][0].endswith('/followup') and page.evaluate('sent')==[]
    done(ctx,'explicit followup with S2S connected uses queue API')

    for status in ['completed','generated','partial','failed','cancelled','interrupted','unsupported']:
        ctx,page,f=setup([task(status=status)]);original=json.loads(json.dumps(f['tasks'][0]))
        page.get_by_role('button',name='改为新任务',exact=True).click()
        expect(page.locator('#message')).to_have_value('空调调到23度');submit(page,'把空调设为21度')
        assert f['posts'][0][0]=='/api/tasks' and f['posts'][0][1]['goal']=='把空调设为21度' and f['posts'][0][1]['parent_task_id']=='one'
        assert next(t for t in f['tasks'] if t['id']=='one')==original
        expect(page.locator('.task')).to_have_count(2)
        done(ctx,'ended '+status+' task creates separate child and preserves original')

    ctx,page,f=setup([task(status='completed')]);page.get_by_role('button',name='改为新任务',exact=True).click();submit(page,'改成21度')
    assert not f['posts'];expect(page.locator('#message')).to_have_value('改成21度');expect(page.locator('#toast')).to_contain_text('完整的新需求')
    f['fail']=True;submit(page,'把空调设为21度');expect(page.locator('#message')).to_have_value('把空调设为21度')
    first=f['posts'][0][1];f['fail']=False;page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
    assert f['posts'][1][1]==first
    done(ctx,'child task requires full instruction and preserves request on failed retry')

    ctx,page,f=setup([task(status='completed')]);page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
    page.get_by_role('button',name='改为新任务',exact=True).click();submit(page,'把空调设为21度')
    assert f['posts'][0][1]['parent_task_id']=='one' and not page.evaluate('sent')
    done(ctx,'explicit child task with S2S connected uses new-task API')

    ctx,page,f=setup([task(status='completed')]);page.get_by_role('button',name='改为新任务',exact=True).click();page.locator('#clearRevise').click();submit(page,'打开车窗')
    assert 'parent_task_id' not in f['posts'][0][1]
    done(ctx,'leaving child context clears parent link')

    for tasks in [[],[task()],[task(status='awaiting_input')],[task(status='waiting')]]:
        ctx,page,f=setup(tasks);submit(page,'谢谢')
        assert not f['posts'];expect(page.locator('.bubble')).to_contain_text('不客气');expect(page.locator('.task')).to_have_count(len(tasks))
        done(ctx,'local thanks creates no task and does not answer/modify '+str([t['status'] for t in tasks]))

    ctx,page,f=setup([]);submit(page,'谢谢，帮我打开车窗');assert f['posts'][0][0]=='/api/tasks'
    done(ctx,'thanks with a real request is not swallowed as acknowledgment')

    ctx,page,f=setup([task(status='awaiting_input')]);page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
    page.get_by_role('button',name='补充信息 →').click();submit(page,'谢谢')
    assert not f['posts'] and page.evaluate('sent')==[{'type':'text','text':'谢谢'}]
    expect(page.locator('.bubble')).to_have_count(0)
    done(ctx,'S2S handles thanks even with a clarification selected')

    for mode in ['automatic','manual','button','voice']:
        ctx,page,f=setup([task(status='awaiting_input')])
        if mode=='manual': page.get_by_role('button',name='补充信息 →').click()
        if mode=='button': page.get_by_role('button',name='改口',exact=True).click()
        if mode=='voice': page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
        submit(page,'改成21度')
        assert len(f['posts'])==1 and f['posts'][0][0]=='/api/tasks/one/revise' and f['posts'][0][1]['task_revision']==0
        if mode=='voice': assert not page.evaluate('sent')
        done(ctx,'awaiting_input accepts revise: '+mode)

    for statuses in [('awaiting_input','running'),('awaiting_input','awaiting_input')]:
        ctx,page,f=setup([task(status=statuses[0]),task('two','座椅加热',status=statuses[1])])
        submit(page,'改成21度');assert not f['posts'];expect(page.locator('#reviseChoices button')).to_have_count(2)
        expect(page.locator('#replyContext')).to_be_hidden()
        page.locator('#reviseChoices button').first.click();page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
        assert f['posts'][0][0].endswith('/revise')
        done(ctx,'mixed editable candidates require selection: '+str(statuses))

    ctx,page,f=setup([task(status='awaiting_input'),task('two','等待温度变化',status='waiting')]);submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/one/revise'
    expect(page.locator('[data-task-id="two"]').get_by_role('button',name='改口',exact=True)).to_have_count(0)
    done(ctx,'timer waiting excluded from edit buttons and correction candidates')

    ctx,page,f=setup([task()]);page.get_by_role('button',name='改口',exact=True).click()
    f['tasks'][0].update(status='awaiting_input',question='哪个分区？');submit(page,'改成21度')
    assert f['posts'][0][0]=='/api/tasks/one/revise'
    done(ctx,'selected running task may transition to awaiting_input without losing revise identity')

    ctx,page,f=setup([task(status='awaiting_input')]);f['tasks'][0]['revision']=1;submit(page,'改成21度')
    assert not f['posts'];expect(page.locator('#message')).to_have_value('改成21度')
    done(ctx,'stale pending-question correction is not silently rebound')

    ctx,page,f=setup([task()]);f['invalid_ack']=True;page.get_by_role('button',name='完成后追加').click();submit(page,'做完告诉我参数')
    expect(page.locator('#message')).to_have_value('做完告诉我参数');expect(page.locator('#reviseLabel')).to_contain_text('完成后追加')
    done(ctx,'mismatched queue receipt preserves draft and target')

    t=task();t['results']=[{'result_id':'old','tool_id':'空调温度','status':'prepared'},{'result_id':'new','tool_id':'空调温度','status':'prepared'},{'result_id':'query','tool_id':'车辆状态','status':'returned_mock'}]
    t['output']={'results':t['results']};t['superseded_result_ids']=['old']
    ctx,page,f=setup([t])
    expect(page.locator('.tool-chip.superseded')).to_have_count(1);expect(page.locator('.tool-chip.superseded')).to_contain_text('旧预览 · 已失效')
    assert '✓' not in page.locator('.tool-chip.superseded').inner_text()
    expect(page.locator('.tool-chip.prepared')).to_have_count(1);expect(page.locator('.tool-chip.returned_mock')).to_have_count(1)
    page.get_by_role('button',name='查看结构 ↗').click();expect(page.locator('#validationNote')).to_contain_text('含已失效的旧预览')
    assert json.loads(page.locator('#jsonCode').inner_text())==t['output']
    page.locator('[data-close="previewDialog"]').click()
    page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(200)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');page.screenshot(path='/tmp/personal-agent-old-preview-mobile.png')
    f['tasks'][0]['superseded_result_ids']=[];page.evaluate('__test.poll()')
    expect(page.locator('.tool-chip.superseded')).to_have_count(0)
    done(ctx,'only server-marked old previews lose success badge; inspector warns without altering output')
    assert not errors,errors
    browser.close()
print(json.dumps({'passed':len(checks),'javascript_errors':errors,'checks':checks},ensure_ascii=False,indent=2))
