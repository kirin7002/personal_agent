"""Isolated Chrome UI acceptance. All HTTP routes are mocked; no server/model starts.

Run: .venv/bin/python scripts/browser_focus_schedule_smoke.py
Artifacts default to /tmp/personal-agent-focus-schedule (override --output-dir).
"""
import argparse
import copy
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, default=Path('/tmp/personal-agent-focus-schedule'))
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
checks, errors = [], []

def task(id='one', goal='打开车窗', status='awaiting_input', revision=0):
    return dict(id=id, goal=goal, status=status, revision=revision, created=1800000000,
                source='fixture', history=[], question='哪个分区？' if status == 'awaiting_input' else None,
                process={'steps': [], 'timing': {}})

def plan(**changes):
    value = dict(id='p1', goal='查询车辆电量', status='pending', revision=2,
                 next_run_at=1800059400, timezone='Asia/Shanghai',
                 schedule={'frequency': 'daily', 'time': '08:30', 'weekdays': []}, runs=[])
    value.update(changes)
    return value

with sync_playwright() as playwright:
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    browser = playwright.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
    def setup(tasks=None, focus=None, plans=None):
        ctx = browser.new_context(viewport={'width': 1280, 'height': 950})
        page = ctx.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        f = dict(tasks=copy.deepcopy(tasks or []), focus=focus, plans=copy.deepcopy(plans or []),
                 posts=[], fail=False, dialog=None, plan_gets=0, plan_events=[], event_queries=[], result={'kind':'chat','status':'ok','message':'好的。'})
        def route(r):
            path = urlparse(r.request.url).path
            if path == '/':
                return r.fulfill(content_type='text/html', body=(ROOT/'web/index.html').read_text())
            if path.startswith('/static/'):
                file = ROOT/'web'/path.removeprefix('/static/')
                body = file.read_text()
                if file.name == 'app.js':
                    body += '\nwindow.__test={state,poll,renderTasks,VoiceSession,pollPlanEvents};'
                return r.fulfill(content_type='application/javascript' if file.suffix == '.js' else 'text/css', body=body)
            if r.request.method == 'POST' and path != '/api/session':
                data = r.request.post_data_json
                f['posts'].append((path, data))
                if f['fail']:
                    return r.fulfill(status=409, json={'detail':'版本已变化，草稿已保留'})
                if path == '/api/conversation/input':
                    result = copy.deepcopy(f['result'])
                    if result.get('task'):
                        t = result['task'];f['tasks'] = [t] + [old for old in f['tasks'] if old['id'] != t['id']]
                        if result['kind'] not in ('chat','historical'):
                            f['focus'] = t['id'] if t['status'] in ('running','awaiting_input','waiting') else None
                    return r.fulfill(json=result)
                if path == '/api/tasks':
                    t = task('new', data['goal'], 'running');t['parent_task_id'] = data.get('parent_task_id')
                    f['tasks'].insert(0,t);f['focus']=t['id'];return r.fulfill(json=t)
                if path.startswith('/api/tasks/'):
                    t = next(t for t in f['tasks'] if t['id'] == path.split('/')[3])
                    if path.endswith('/revise'):
                        t.update(revision=t['revision']+1,status='running',question=None,
                                 latest_change={'text':data['text'],'status':'accepted'})
                        return r.fulfill(json=t)
                    if path.endswith('/cancel'):
                        t.update(status='cancelled',question=None);return r.fulfill(json=t)
                    if path.endswith('/followup'):
                        return r.fulfill(json={'status':'queued','task_id':t['id'],'event_id':data['event_id']})
                if path == '/api/plans':
                    p = plan(id='created', goal=data['goal'], schedule=data['schedule'], revision=0)
                    f['plans'].insert(0,p)
                    return r.fulfill(json={'kind':'plan_created','status':'pending','message':'计划已创建','plan':p})
                if path.startswith('/api/plans/') and path.endswith('/action'):
                    p = next(p for p in f['plans'] if p['id'] == path.split('/')[3])
                    if data['revision'] != p['revision']:
                        return r.fulfill(status=409,json={'detail':'Plan revision changed'})
                    action = data['action'];p['revision'] += 1
                    if action == 'edit': p.update(goal=data['goal'], schedule=data['schedule'])
                    elif action == 'pause': p.update(status='paused',next_run_at=None)
                    elif action == 'resume': p.update(status='pending',next_run_at=1800059400)
                    elif action == 'cancel_plan': p.update(status='cancelled',next_run_at=None)
                    elif action == 'cancel_once':
                        for run in p['runs']:
                            if run.get('task_status') in ('running','waiting','awaiting_input'): run['task_status']='cancelled'
                        p['next_run_at']=1800145800
                    else: raise AssertionError('Unexpected plan action '+action)
                    return r.fulfill(json={'kind':'plan_updated','status':p['status'],'message':'计划已更新','plan':p})
                raise AssertionError('Unexpected mutation '+path)
            values = {
                '/api/session':{}, '/api/config':{'hermes_configured':False,'voice_configured':False},
                '/api/catalog':{'profiles':[{'id':'demo','title':'演示'}],'scenarios':[], 'counts':{'control':1,'manual':0,'status':0}},
                '/api/session/profile':{'profile_id':'demo'}, '/api/catalog/tree':{'items':[],'next_offset':None},
                '/api/events/cursor':{'cursor':0}, '/api/tasks':{'tasks':f['tasks'],'focus_task_id':f['focus'],'dialog':f['dialog']},
                '/api/plans':{'plans':f['plans'],'timezone':'Asia/Shanghai'}}
            if path == '/api/plans': f['plan_gets'] += 1
            if path == '/api/plan-events':
                f['event_queries'].append(r.request.url)
                return r.fulfill(json={'events':f['plan_events']})
            if path == '/api/events/stream': return r.fulfill(content_type='text/event-stream',body=': fixture\n\n')
            if path not in values: raise AssertionError('Unexpected GET '+path)
            return r.fulfill(json=values[path])
        page.route('**/*', route)
        page.goto('http://127.0.0.1:18765/')
        page.wait_for_function('window.__test?.state.initialized && __test.state.planEventsInitialized')
        return ctx,page,f
    def submit(page,text):
        page.locator('#message').fill(text);page.locator('#sendButton').click()
        page.wait_for_function('!__test.state.busy')
    def done(ctx,label):
        checks.append(label);print('PASS '+label,flush=True);ctx.close()
    def open_plans(page):
        page.locator('#openPlans').click();expect(page.locator('#planTimezone')).to_contain_text('Asia/Shanghai')
    def save_plan(page,goal='查询车辆电量',frequency='daily',at='08:30',days=(),minutes='1'):
        page.locator('#planGoal').fill(goal);page.locator('#planFrequency').select_option(frequency)
        if frequency == 'interval': page.locator('#planMinutes').fill(minutes)
        else: page.locator('#planTime').fill(at)
        for day in days:page.locator(f'#planWeekdays input[value="{day}"]').check()
        page.locator('#savePlan').click();page.wait_for_function('!__test.state.planBusy')
    try:
        old,new = task('old','打开车窗'),task('new','空调调到23度')
        ctx,page,f=setup([old,new],'new')
        expect(page.locator('#replyLabel')).to_contain_text('空调调到23度');expect(page.locator('#replyChoices')).to_be_hidden()
        f['tasks'].reverse();f['tasks'][1]['summary']='旧任务收到新进度';page.evaluate('__test.poll()')
        expect(page.locator('#replyLabel')).to_contain_text('空调调到23度')
        f['result']={'kind':'answer_question','status':'running','message':'已收到补充','task':task('new','空调调到23度','running',1)}
        submit(page,'全部');path,body=f['posts'][-1]
        assert path=='/api/conversation/input' and set(body)=={'text','event_id'} and body['text']=='全部'
        expect(page.locator('#replyContext')).to_be_hidden();expect(page.locator('.task')).to_have_count(2)
        done(ctx,'focus routes latest question; old progress cannot steal focus; running focus has no reply banner')

        ctx,page,f=setup([old,new],'old');page.locator('#message').fill('主驾')
        f['focus']='new';page.evaluate('__test.poll()');expect(page.locator('#message')).to_have_value('主驾')
        expect(page.locator('#replyLabel')).to_contain_text('空调');page.locator('#sendButton').click()
        expect(page.locator('#message')).to_have_value('');assert 'task_id' not in f['posts'][0][1]
        done(ctx,'new server focus replaces automatic context without replacing draft or pinning old task')

        ctx,page,f=setup([old,new],'new')
        page.locator('[data-task-id="old"]').get_by_role('button',name='补充信息 →').click();submit(page,'主驾')
        assert f['posts'][0][1]['task_id']=='old' and f['posts'][0][1]['task_revision']==0
        done(ctx,'explicit manual task overrides server focus through unified endpoint')

        for text in ['算了','不用全部，主驾就好','别取消','改成21度','谢谢','打开车窗']:
            ctx,page,f=setup([old,new],'new');submit(page,text)
            assert len(f['posts'])==1 and f['posts'][0][0]=='/api/conversation/input' and f['posts'][0][1]['text']==text
            assert set(f['posts'][0][1])=={'text','event_id'}
            done(ctx,'normal text delegates semantic classification once: '+text)

        ctx,page,f=setup([old],None);f['result']={'kind':'clarify','status':'awaiting_input','message':'当前没有适用的待答问题，请明确任务。'}
        expect(page.locator('#replyContext')).to_be_hidden();submit(page,'主驾')
        assert 'task_id' not in f['posts'][0][1];expect(page.locator('.task')).to_have_count(1)
        expect(page.locator('.bubble').last).to_contain_text('没有适用');done(ctx,'no focus never falls back to historical pending question')

        ctx,page,f=setup([old,new],'new');f['result']={'kind':'clarify','status':'awaiting_input','message':'请选择历史任务','candidates':[old,new]}
        submit(page,'之前那个任务，取消');expect(page.locator('#message')).to_have_value('之前那个任务，取消')
        expect(page.locator('#replyChoices button')).to_have_count(2)
        page.locator('#replyChoices button').first.click();f['result']={'kind':'cancel','status':'cancelled','message':'已取消','task':task('old','打开车窗','cancelled')}
        page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
        assert f['posts'][-1][1]['task_id']=='old' and f['posts'][0][1]['event_id']!=f['posts'][-1][1]['event_id']
        done(ctx,'historical ambiguity preserves draft and resubmits explicit candidate id/revision')

        for binding in ['automatic','manual','revise','followup']:
            ctx,page,f=setup([old],'old')
            if binding!='automatic':page.get_by_role('button',name={'manual':'补充信息 →','revise':'改口','followup':'完成后追加'}[binding],exact=True).click()
            page.locator('#message').fill('全部');f['tasks'][0].update(status='expired',revision=1,question=None);f['focus']=None
            page.evaluate('__test.poll()');expect(page.locator('#replyContext')).to_be_hidden();expect(page.locator('#reviseContext')).to_be_hidden()
            expect(page.locator('#message')).to_have_value('全部');expect(page.locator('.task-status')).to_have_text('已过期')
            expect(page.get_by_role('button',name='基于此新建任务',exact=True)).to_be_visible()
            page.locator('#sendButton').click();page.wait_for_function('!__test.state.busy');assert not f['posts']
            done(ctx,'expiry clears '+binding+' binding and preserves draft without late submission')

        ctx,page,f=setup([old],'old');f['fail']=True;submit(page,'主驾')
        expect(page.locator('#message')).to_have_value('主驾');first=f['posts'][0][1]
        f['fail']=False;page.locator('#sendButton').click();expect(page.locator('#message')).to_have_value('')
        assert f['posts'][1][1]==first;done(ctx,'unified input retry reuses event id and preserves failed draft')

        ctx,page,f=setup([old],'old');page.evaluate('window.sent=[];__test.state.voice={connected:true,send:e=>sent.push(e)}')
        submit(page,'全部');assert f['posts'][0][0]=='/api/conversation/input' and page.evaluate('sent')==[]
        done(ctx,'connected S2S receives no duplicate typed input')

        ctx,page,f=setup([old],'old');f['dialog']={'kind':'schedule_creation','question':'请明确几点几分执行','expires':2000000000}
        page.evaluate('__test.poll()');expect(page.locator('#replyLabel')).to_have_text('请明确几点几分执行')
        submit(page,'08:30');assert 'task_id' not in f['posts'][0][1]
        page.get_by_role('button',name='补充信息 →').click();submit(page,'全部');assert f['posts'][-1][1]['task_id']=='old'
        done(ctx,'plan dialog restores its question while manual task selection remains explicit')

        for status in ['running','awaiting_input']:
            ctx,page,f=setup([task(status=status)],'one');page.get_by_role('button',name='改口',exact=True).click();submit(page,'改成21度')
            assert f['posts'][0][0]=='/api/tasks/one/revise' and f['posts'][0][1]['task_revision']==0
            done(ctx,'explicit revise preserved for '+status)
        ctx,page,f=setup([task(status='waiting')],'one')
        expect(page.get_by_role('button',name='改口',exact=True)).to_have_count(0)
        page.get_by_role('button',name='完成后追加').click();submit(page,'做完告诉我参数')
        assert f['posts'][0][0]=='/api/tasks/one/followup';expect(page.locator('#message')).to_have_value('')
        done(ctx,'waiting retains followup queue and hides unsupported revise')
        ctx,page,f=setup([task(status='completed')]);original=copy.deepcopy(f['tasks'][0])
        page.get_by_role('button',name='基于此新建任务').click();submit(page,'把空调设为21度')
        assert f['posts'][0][1]['parent_task_id']=='one' and next(t for t in f['tasks'] if t['id']=='one')==original
        done(ctx,'renamed completed action creates child while preserving original')

        ctx,page,f=setup();f['result']={'kind':'plan_created','status':'pending','message':'计划已创建','plan':plan()}
        submit(page,'每天08:30查询车辆电量');bubble=page.locator('.bubble').last
        for text in ['查询车辆电量','每天 08:30','Asia/Shanghai','下次：']:expect(bubble).to_contain_text(text)
        page.screenshot(path=str(args.output_dir/'chat-plan-summary.png'))
        done(ctx,'chat plan receipt includes goal schedule timezone and next run')

        runs=[dict(scheduled_at=1800000000,status='skipped',reason='previous_occurrence_active'),dict(scheduled_at=1800000100,status='missed',reason='late_beyond_grace'),dict(scheduled_at=1800000200,status='created',task_status='expired',task_id='one')]
        ctx,page,f=setup([task(status='expired')],plans=[plan(runs=runs)]);open_plans(page)
        expect(page.locator('.plan-card')).to_contain_text('每天 08:30');page.locator('.plan-history summary').click()
        for text in ['已跳过','上次执行尚未结束','错过执行','已过期']:expect(page.locator('.plan-history')).to_contain_text(text)
        page.screenshot(path=str(args.output_dir/'plans-desktop.png'))
        page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(150)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(args.output_dir/'plans-responsive.png'))
        done(ctx,'plans render service timezone next run and skipped/missed/expired execution history')

        for frequency,days,expected in [('daily',(),[]),('weekdays',(),[0,1,2,3,4]),('weekly',(1,5),[1,5])]:
            ctx,page,f=setup();open_plans(page);save_plan(page,frequency=frequency,days=days)
            path,body=f['posts'][0];assert path=='/api/plans' and len(body['request_key'])==36
            assert body['schedule']=={'frequency':'daily' if frequency=='daily' else 'weekly','time':'08:30','weekdays':expected}
            expect(page.locator('.plan-card')).to_have_count(1);expect(page.locator('#planGoal')).to_have_value('')
            done(ctx,'create '+frequency+' plan with exact schedule contract')
        ctx,page,f=setup();open_plans(page);page.locator('#planGoal').fill('查询电量');page.locator('#savePlan').click()
        assert not f['posts'] and not page.locator('#planTime').evaluate('(n)=>n.checkValidity()')
        page.locator('#planFrequency').select_option('weekly');page.locator('#planTime').fill('09:15');page.locator('#savePlan').click()
        expect(page.locator('#planError')).to_be_visible();assert not f['posts'];done(ctx,'plan requires explicit time and weekly day selection')
        ctx,page,f=setup();open_plans(page);f['fail']=True;save_plan(page)
        expect(page.locator('#planGoal')).to_have_value('查询车辆电量');expect(page.locator('#planError')).to_be_visible();first=f['posts'][0][1]
        f['fail']=False;page.locator('#savePlan').click();expect(page.locator('#planGoal')).to_have_value('')
        assert f['posts'][1][1]==first;done(ctx,'plan-create error preserves draft and stable request_key')

        ctx,page,f=setup(plans=[plan()]);open_plans(page);page.get_by_role('button',name='修改',exact=True).click()
        save_plan(page,goal='查询胎压',frequency='weekly',at='19:45',days=(0,4))
        path,body=f['posts'][0];assert path=='/api/plans/p1/action' and body['action']=='edit' and body['revision']==2
        assert body['schedule']['weekdays']==[0,4];expect(page.locator('.plan-card h3')).to_have_text('查询胎压')
        done(ctx,'plan editing submits edit action with selected revision')
        ctx,page,f=setup(plans=[plan()]);open_plans(page);page.get_by_role('button',name='修改',exact=True).click()
        f['plans'][0]['revision']=3;save_plan(page,goal='查询胎压')
        expect(page.locator('#planError')).to_contain_text('revision');expect(page.locator('#planGoal')).to_have_value('查询胎压')
        done(ctx,'stale plan edit leaves revision and draft intact for review')

        ctx,page,f=setup(plans=[plan()]);open_plans(page);page.get_by_role('button',name='暂停',exact=True).click()
        expect(page.locator('.plan-card')).to_contain_text('已暂停');assert f['posts'][0][1]=={'action':'pause','revision':2}
        page.get_by_role('button',name='恢复',exact=True).click();expect(page.get_by_role('button',name='暂停',exact=True)).to_be_visible()
        assert f['posts'][1][1]=={'action':'resume','revision':3};done(ctx,'pause/resume use fresh revision and refreshed plan status')
        for active in [False,True]:
            runs=[dict(task_id='one',task_status='waiting',status='created',scheduled_at=1800000000)] if active else []
            ctx,page,f=setup(plans=[plan(runs=runs)]);open_plans(page);page.locator('.plan-cancel summary').click()
            assert not f['posts'];expect(page.locator('.plan-cancel')).to_contain_text('当前这次' if active else '下一次')
            page.get_by_role('button',name='只取消这一次',exact=True).click();page.wait_for_function('!__test.state.planBusy')
            assert f['posts'][0][1]=={'action':'cancel_once','revision':2} and f['plans'][0]['status']=='pending'
            done(ctx,'explicit cancel-once scope '+('current execution' if active else 'next trigger')+' preserves plan')
        ctx,page,f=setup(plans=[plan()]);open_plans(page);page.locator('.plan-cancel summary').click()
        page.get_by_role('button',name='取消整个计划',exact=True).click();expect(page.locator('.plan-card')).to_contain_text('已取消')
        assert f['posts'][0][1]=={'action':'cancel_plan','revision':2};expect(page.get_by_role('button',name='恢复',exact=True)).to_have_count(0)
        done(ctx,'cancel entire plan is separately selected and retains history card')
        ctx,page,f=setup(plans=[plan(status='paused',next_run_at=None)]);open_plans(page);page.locator('.plan-cancel summary').click()
        expect(page.get_by_role('button',name='只取消这一次')).to_be_disabled();done(ctx,'no current/upcoming occurrence cannot be cancelled once')

        ctx,page,f=setup(plans=[plan()]);f['result']={'kind':'plans','status':'ok','message':'已返回周期计划','plans':[plan()]}
        submit(page,'查看周期计划');expect(page.locator('#plansDialog')).to_be_visible();expect(page.locator('.plan-card')).to_have_count(1)
        done(ctx,'unified list-plans response opens the plans view')

        t=task(status='running');t.update(results=[{'result_id':'old','tool_id':'空调','status':'prepared'},{'result_id':'new','tool_id':'空调','status':'prepared'}],superseded_result_ids=['old'])
        ctx,page,f=setup([t],'one');expect(page.locator('.tool-chip.superseded')).to_contain_text('已失效');expect(page.locator('.tool-chip.prepared')).to_have_count(1)
        valid=page.evaluate('''() => {const v=new __test.VoiceSession();v.send=()=>{};let stops=0;v.receive({type:'assistant.delta',text:'旧播报'});v.sources.add({stop:()=>stops++});v.playing=true;v.receive({type:'voice.invalidate',task_id:'one',task_revision:1});return stops===1&&!v.currentText&&!v.sources.size&&!document.querySelector('.bubble');}''')
        assert valid;done(ctx,'superseded preview and conservative voice invalidation remain intact')

        ctx,page,f=setup();open_plans(page);save_plan(page,frequency='interval',minutes='1')
        assert f['posts'][0][1]['schedule']=={'frequency':'interval','seconds':60,'timezone':'Asia/Shanghai'}
        expect(page.locator('.plan-card')).to_contain_text('每 1 分钟')
        done(ctx,'interval form creates integer minutes as seconds with timezone')

        ctx,page,f=setup(plans=[plan(schedule={'frequency':'interval','seconds':300,'timezone':'Asia/Shanghai'})]);open_plans(page)
        expect(page.locator('.plan-card')).to_contain_text('每 5 分钟');page.get_by_role('button',name='修改',exact=True).click()
        expect(page.locator('#planFrequency')).to_have_value('interval');expect(page.locator('#planMinutes')).to_have_value('5')
        expect(page.locator('#planTime')).to_be_disabled();page.locator('#planMinutes').fill('2');page.locator('#savePlan').click()
        expect(page.locator('.plan-card')).to_contain_text('每 2 分钟')
        assert f['posts'][0][1]['schedule']=={'frequency':'interval','seconds':120,'timezone':'Asia/Shanghai'}
        page.screenshot(path=str(args.output_dir/'interval-plan.png'))
        done(ctx,'interval plan edits through existing action without daily-time fields')

        ctx,page,f=setup();open_plans(page);page.locator('#planGoal').fill('查询电量');page.locator('#planFrequency').select_option('interval')
        for invalid in ['','0','1.5']:
            page.locator('#planMinutes').fill(invalid);page.locator('#savePlan').click()
            assert not f['posts'] and not page.locator('#planMinutes').evaluate('(n)=>n.checkValidity()')
        page.locator('#planFrequency').select_option('daily');expect(page.locator('#planMinutes')).to_be_disabled()
        expect(page.locator('#planTime')).to_be_enabled();done(ctx,'interval requires positive integer minutes and toggles native time validation')

        ctx,page,f=setup(plans=[plan(schedule={'frequency':'interval','seconds':90,'timezone':'Asia/Shanghai'})]);open_plans(page)
        page.get_by_role('button',name='修改',exact=True).click();expect(page.locator('#planIntervalNote')).to_contain_text('90 秒')
        page.locator('#planGoal').fill('查询胎压');page.locator('#savePlan').click();expect(page.locator('#planGoal')).to_have_value('')
        assert f['posts'][0][1]['schedule']['seconds']==90
        done(ctx,'non-minute existing intervals retain exact seconds when editing only goal')

        ctx,page,f=setup();f['result']={'kind':'plan_created','status':'pending','message':'计划已创建','plan':plan(schedule={'frequency':'interval','seconds':60,'timezone':'Asia/Shanghai'})}
        submit(page,'每分钟查询车辆电量');expect(page.locator('.bubble').last).to_contain_text('每 1 分钟')
        done(ctx,'natural interval receipt has readable interval label')

        ctx,page,f=setup(plans=[plan()]);open_plans(page);page.locator('.plan-history summary').click();page.locator('.plan-cancel summary').click()
        page.locator('#planGoal').fill('尚未提交的草稿');page.locator('#planFrequency').select_option('interval');page.locator('#planMinutes').fill('3')
        before=f['plan_gets'];f['plans'][0]['runs'].append(dict(scheduled_at=1800000000,status='missed',reason='late_beyond_grace'))
        expect(page.locator('.plan-history')).to_contain_text('错过执行',timeout=5000)
        assert before < f['plan_gets'] <= before+2
        assert page.locator('.plan-history').evaluate('(n)=>n.open') and page.locator('.plan-cancel').evaluate('(n)=>n.open')
        expect(page.locator('#planGoal')).to_have_value('尚未提交的草稿');expect(page.locator('#planMinutes')).to_have_value('3')
        page.get_by_role('button',name='修改',exact=True).click();before=f['plan_gets'];page.wait_for_timeout(2300);assert f['plan_gets']==before
        page.locator('#cancelPlanEdit').click();page.evaluate('__test.state.planBusy=true');before=f['plan_gets'];page.wait_for_timeout(2300);assert f['plan_gets']==before
        page.evaluate('__test.state.planBusy=false');page.get_by_role('button',name='关闭周期计划',exact=True).click();before=f['plan_gets'];page.wait_for_timeout(2300);assert f['plan_gets']==before
        done(ctx,'two-second open refresh preserves drafts/expanded history and pauses when editing busy or closed')

        ctx,page,f=setup()
        # Recreate a first successful event load with history already present.
        f['plan_events']=[{'id':1,'kind':'plan.missed','notify':1,'plan_id':'p1','data':{'goal':'旧计划','message':'旧通知'}}]
        page.evaluate('__test.state.planEventsInitialized=false; __test.state.planEventsCursor=0; __test.pollPlanEvents()')
        expect(page.locator('.bubble')).to_have_count(0)
        f['plan_events'] += [
            {'id':2,'kind':'plan.created','notify':1,'data':{'goal':'电量计划','message':'已创建'}},
            {'id':3,'kind':'plan.missed','notify':1,'data':{'goal':'电量计划','message':'错过触发时间，未补跑'}},
            {'id':4,'kind':'plan.skipped','notify':1,'data':{'goal':'胎压计划','message':'上次执行尚未结束，本次跳过'}},
            {'id':5,'kind':'plan.skipped','notify':0,'data':{'goal':'静默计划','message':'无需通知'}},
            {'id':6,'kind':'plan.pause','notify':1,'data':{'goal':'电量计划','message':'已暂停'}}]
        expect(page.locator('.bubble')).to_have_count(2,timeout=5000)
        expect(page.locator('.bubble').first).to_contain_text('电量计划');expect(page.locator('.bubble').last).to_contain_text('本次跳过')
        expect(page.locator('#plansDialog')).not_to_be_visible()
        page.evaluate('__test.pollPlanEvents()');expect(page.locator('.bubble')).to_have_count(2)
        assert 'after=6' in f['event_queries'][-1] and not f['posts']
        page.screenshot(path=str(args.output_dir/'plan-notices.png'))
        done(ctx,'new missed/skipped notices appear with dialog closed; baseline/actions/silent events suppressed; cursor dedupes without audio ack')
        assert not errors, errors
    finally:
        (args.output_dir/'results.json').write_text(json.dumps({'passed':len(checks),'javascript_errors':errors,'checks':checks,'mode':'mocked APIs; no backend or model calls'},ensure_ascii=False,indent=2))
        browser.close()
print(json.dumps({'passed':len(checks),'javascript_errors':errors,'artifacts':str(args.output_dir)},ensure_ascii=False))
