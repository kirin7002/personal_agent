"""Isolated mobile contract/visual smoke; all APIs and voice hardware are mocked.

Run: .venv/bin/python scripts/browser_mobile_contract_smoke.py
Artifacts: /tmp/personal-agent-mobile (override --output-dir). No backend changes.
"""
import argparse
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    checks, errors, layouts = [], [], []
    with sync_playwright() as playwright:
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        browser = playwright.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)

        def setup(width=390, messages=None, tasks=None, pairing_required=False, older=None):
            ctx = browser.new_context(viewport={'width': width, 'height': 844}, is_mobile=True, has_touch=True, device_scale_factor=1)
            page = ctx.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            fixture = {'messages': copy.deepcopy(messages or []), 'tasks': copy.deepcopy(tasks or []), 'posts': [],
                       'plans': [{'id':'p1', 'goal':'查询车辆电量', 'status':'pending', 'revision':2,
                                  'schedule':{'frequency':'daily','time':'08:00'}, 'timezone':'Asia/Shanghai', 'next_run_at':1800059400}],
                       'devices':[{'id':'phone','name':'我的手机'}], 'owner':None, 'fail':False, 'pair_fail':False,
                       'cursor':len(messages or []), 'message_gets':0, 'pairing_required':pairing_required, 'older':older or []}
            page.add_init_script("""
              window.__streams=[]; window.__voiceStarts=0; window.__voiceStops=0;
              class MockEventSource extends EventTarget {
                constructor(url){super();this.url=url;this.closed=false;__streams.push(this);setTimeout(()=>this.onopen?.(),0)}
                close(){this.closed=true}
                emit(data){const e=new MessageEvent('message',{data:JSON.stringify(data)});this.onmessage?.(e)}
              } window.EventSource=MockEventSource;
            """)
            def route(r):
                path=urlparse(r.request.url).path
                if path=='/mobile': return r.fulfill(content_type='text/html',body=(ROOT/'web/mobile.html').read_text())
                if path=='/static/voice-client.js':
                    return r.fulfill(content_type='application/javascript',body="""
                      export class VoiceSession {
                        constructor(options){this.options=options;this.connected=false;window.__voice=this}
                        async start(){__voiceStarts++;this.options.onState('connecting');if(window.__denyMic)throw new DOMException('Denied','NotAllowedError');this.connected=true;this.options.onState('connected')}
                        stop(){__voiceStops++;this.connected=false;this.options.onState('stopped')}
                      }
                    """)
                if path.startswith('/static/'):
                    file=ROOT/'web'/path.removeprefix('/static/')
                    body=file.read_text()
                    if file.name=='mobile.js': body+='\nwindow.__mobileTest={state,refreshAll,refreshMessages,refreshTasks,refreshPlans,renderMessages,reconnect,stopVoice};'
                    return r.fulfill(content_type='application/javascript' if file.suffix=='.js' else 'text/css',body=body)
                if r.request.method=='POST':
                    data=r.request.post_data_json
                    if path=='/api/session':
                        assert data['kind']=='mobile'
                        return r.fulfill(json={'ready':True,'device':{'id':'phone','name':'我的手机'},'pairing_required':fixture['pairing_required']})
                    fixture['posts'].append((path,data))
                    if path=='/api/conversation/input':
                        if fixture['fail']: return r.fulfill(status=503,json={'detail':'连接暂时不可用'})
                        if not any(m.get('event_id')==data['event_id'] for m in fixture['messages']):
                            fixture['messages'].append({'id':'user-'+data['event_id'],'role':'user','text':data['text'],'kind':'text','version':1,'event_id':data['event_id']})
                            fixture['cursor']+=1
                        return r.fulfill(json={'kind':'chat','message':'THIS LOCAL SUMMARY MUST NOT APPEAR'})
                    if path=='/api/pairing/redeem':
                        if fixture['pair_fail']: return r.fulfill(status=400,json={'detail':'配对码已失效，请在电脑重新生成。'})
                        fixture['messages']=[{'id':'shared','role':'assistant','text':'这段对话，现在可以接着聊。','kind':'text','version':1}]
                        fixture['tasks']=[];fixture['pairing_required']=False;fixture['devices'].append({'id':'desktop','name':'电脑观察台','role':'observer'})
                        return r.fulfill(json={'device':{'id':'phone','paired':True}})
                    if path=='/api/voice/takeover': fixture['owner']=None;return r.fulfill(json={'ok':True})
                    if path=='/api/plans/p1/action':
                        p=fixture['plans'][0]
                        assert data['revision']==p['revision'], 'Plan action used a stale revision'
                        p['revision']+=1
                        if data['action']=='pause':p['status']='paused';p['next_run_at']=None
                        if data['action']=='resume':p['status']='pending';p['next_run_at']=1800059400
                        if data['action']=='cancel_plan':p['status']='cancelled'
                        return r.fulfill(json={'kind':'plan_updated','plan':p})
                    raise AssertionError('Unexpected POST '+path)
                if fixture['pairing_required']: raise AssertionError('Private API called before pairing: '+path)
                if path=='/api/conversation/messages':
                    fixture['message_gets']+=1
                    before=parse_qs(urlparse(r.request.url).query).get('before')
                    return r.fulfill(json={'messages':fixture['older'] if before else fixture['messages'],'cursor':fixture['cursor'],
                                          'process_events':[{'kind':'voice.process','payload':{'step':{'title':'SECRET TOOL TRACE','tool':'control.climate.set_temperature'}}}],
                                          'has_more':bool(fixture['older']) and not before,'next_before':fixture['messages'][0]['id'] if fixture['older'] and not before else None})
                values={'/api/tasks':{'tasks':fixture['tasks'],'focus_task_id':fixture['tasks'][0]['id'] if fixture['tasks'] else None},
                        '/api/plans':{'plans':fixture['plans'],'timezone':'Asia/Shanghai'},'/api/devices':{'devices':fixture['devices']},
                        '/api/voice-owner':{'owner':fixture['owner']}}
                if path in values:return r.fulfill(json=values[path])
                raise AssertionError('Unexpected request '+path)
            page.route('**/*',route)
            page.goto('http://localhost:18765/mobile')
            page.wait_for_function('window.__mobileTest?.state.ready || window.__mobileTest?.state.pairingRequired')
            return ctx,page,fixture

        def finish(ctx,label):
            checks.append(label);print('PASS '+label,flush=True);ctx.close()

        def submit(page,text):
            page.locator('#messageInput').fill(text);page.locator('#sendButton').click()
            page.wait_for_function('!__mobileTest.state.busy')

        def stream(page,message,cursor=100):
            page.evaluate('data=>__streams.at(-1).emit(data)',{'message':message,'cursor':cursor})

        def no_overflow(page):
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'horizontal overflow'
            assert page.locator('#sendButton').bounding_box()['x']>=0

        def chrome_in_view(page, label):
            page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            bounds=page.evaluate('''() => {
              const box=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height}};
              const v=window.visualViewport;
              return {header:box('.topbar'),composer:box('.composer-area'),input:box('#messageInput'),send:box('#sendButton'),conversation:box('#conversation'),
                viewport:{height:v?.height||innerHeight,offsetTop:v?.offsetTop||0,width:v?.width||innerWidth},
                outerScroll:scrollY,documentHeight:document.documentElement.scrollHeight,innerHeight,
                chatScroll:document.querySelector('#conversation').scrollTop};
            }''')
            layouts.append({'label':label,**bounds})
            top=bounds['viewport']['offsetTop'];bottom=top+bounds['viewport']['height']
            for name in ('header','composer','input','send'):
                box=bounds[name]
                assert box['height']>0 and box['top']>=top-1 and box['bottom']<=bottom+1, f'{label}: {name} clipped: {bounds}'
            assert abs(bounds['outerScroll'])<=1, f'{label}: outer document scrolled: {bounds}'
            assert bounds['header']['bottom']<=bounds['conversation']['top']+1, bounds
            assert bounds['conversation']['bottom']<=bounds['composer']['top']+1, bounds
            no_overflow(page)

        try:
            for width in (320,390,430):
                ctx,page,f=setup(width)
                expect(page.locator('#welcome')).to_be_visible();expect(page.locator('#pairDialog')).not_to_be_visible()
                no_overflow(page)
                page.screenshot(path=str(output/f'mobile-welcome-{width}.png'),full_page=True)
                submit(page,'谢谢')
                expect(page.locator('.message.user')).to_have_count(1)
                assert f['posts'][0][0]=='/api/conversation/input'
                assert 'task_id' not in f['posts'][0][1]
                expect(page.locator('body')).not_to_contain_text('THIS LOCAL SUMMARY')
                finish(ctx,f'{width}px welcome fits; unpaired chat works; canonical-only rendering')

            task={'id':'t1','goal':'把空调调到23度','status':'awaiting_input','revision':3,'question':'要调哪个位置？主驾，还是全部？'}
            messages=[{'id':'u1','role':'user','text':'把空调调到23度','kind':'text','version':1},
                      {'id':'q1','role':'assistant','text':task['question'],'kind':'task.question','task_id':'t1','task_revision':3,'version':1}]
            for width in (320,390):
                ctx,page,f=setup(width,messages=messages,tasks=[task])
                chrome_in_view(page,f'{width}: initial canonical question')
                page.screenshot(path=str(output/f'mobile-question-viewport-{width}.png'))
                submit(page,'全部')
                chrome_in_view(page,f'{width}: after submitted answer')
                result={'id':'layout-result','role':'assistant','text':'已模拟设置：全部分区21度。','kind':'task.result','version':1}
                stream(page,result)
                chrome_in_view(page,f'{width}: result SSE update')
                f['messages'].append(result);page.evaluate('__mobileTest.refreshMessages()')
                chrome_in_view(page,f'{width}: result snapshot update')
                page.screenshot(path=str(output/f'mobile-result-viewport-{width}.png'))
                finish(ctx,f'{width}px question/result hydration and live updates keep header and composer within viewport')

                history=[{'id':i,'role':'assistant' if i%2 else 'user','text':f'已有对话 {i}：这里保留一段可供向上浏览的聊天记录。','kind':'text','version':1} for i in range(1,25)]
                ctx,page,f=setup(width,messages=history+messages,tasks=[task])
                chrome_in_view(page,f'{width}: long canonical history hydrated')
                # Check the stable page after smooth inner scrolling has finished.
                page.wait_for_timeout(650)
                chrome_in_view(page,f'{width}: settled history hydration')
                assert page.locator('#conversation').evaluate('n=>n.scrollHeight>n.clientHeight+300'), 'fixture did not create scrollable history'
                page.evaluate('''() => {const chat=document.querySelector('#conversation');chat.style.scrollBehavior='auto';chat.scrollTop=120}''')
                chrome_in_view(page,f'{width}: reading older history')
                stream(page,result)
                chrome_in_view(page,f'{width}: new result while reading history')
                assert abs(page.locator('#conversation').evaluate('n=>n.scrollTop')-120)<=1, 'new message displaced history reading position'
                page.evaluate('''() => {const chat=document.querySelector('#conversation');chat.scrollTop=chat.scrollHeight}''')
                chrome_in_view(page,f'{width}: scrolled to latest message')
                stream(page,{**result,'id':'layout-result-later','text':'新的结果已经同步。'})
                chrome_in_view(page,f'{width}: result appended at chat bottom')
                assert page.locator('#conversation').evaluate('n=>n.scrollHeight-n.scrollTop-n.clientHeight')<=1
                page.screenshot(path=str(output/f'mobile-history-bottom-{width}.png'))
                page.locator('#messageInput').fill('继续看看');page.locator('#messageInput').focus()
                chrome_in_view(page,f'{width}: focused composer during history scroll')
                page.reload();page.wait_for_function('__mobileTest.state.ready')
                chrome_in_view(page,f'{width}: long history after refresh')
                finish(ctx,f'{width}px long history scrolling, focused input and refresh do not scroll outer document')

            ctx,page,f=setup(messages=messages,tasks=[task])
            expect(page.get_by_role('button',name='全部',exact=True)).to_be_visible()
            chrome_in_view(page,'original mobile-question screenshot')
            page.screenshot(path=str(output/'mobile-question.png'),full_page=True)
            page.get_by_role('button',name='全部',exact=True).click();page.wait_for_function('!__mobileTest.state.busy')
            assert f['posts'][0][1]['task_id']=='t1' and f['posts'][0][1]['task_revision']==3
            canonical={'id':'r1','role':'assistant','text':'已模拟设置：全部分区21度。','kind':'task.result','task_id':'t1','task_revision':4,'version':2}
            f['tasks'][0].update(status='generated',revision=4,question=None)
            page.evaluate('__mobileTest.refreshTasks()')
            stream(page,canonical);stream(page,canonical)
            stream(page,{**canonical,'text':'过时的23度结果','version':1})
            expect(page.locator('[data-message-id="r1"]')).to_have_count(1)
            chrome_in_view(page,'original mobile-result screenshot')
            expect(page.locator('[data-message-id="r1"]')).to_contain_text('21度')
            expect(page.locator('.choice')).to_have_count(0)
            expect(page.locator('body')).not_to_contain_text('过时的23度结果')
            f['messages'].append(canonical)
            page.evaluate('__mobileTest.refreshMessages()')
            expect(page.locator('[data-message-id="r1"]')).to_have_count(1)
            page.screenshot(path=str(output/'mobile-result.png'),full_page=True)
            finish(ctx,'question chips bind task+revision; duplicate/stale SSE ignored; old question loses actions')

            ctx,page,f=setup()
            f['fail']=True;submit(page,'把车窗打开')
            expect(page.locator('#messageInput')).to_have_value('把车窗打开')
            expect(page.locator('#sendError')).to_be_visible();expect(page.locator('.message')).to_have_count(0)
            first=f['posts'][0][1]['event_id'];f['fail']=False
            page.locator('#sendButton').click();page.wait_for_function('!__mobileTest.state.busy')
            assert f['posts'][1][1]['event_id']==first
            expect(page.locator('.message.user')).to_have_count(1)
            page.locator('#messageInput').fill('尚未发送的草稿');page.reload();page.wait_for_function('__mobileTest.state.ready')
            expect(page.locator('#messageInput')).to_have_value('尚未发送的草稿')
            expect(page.locator('.message.user')).to_have_count(1)
            page.evaluate('__streams.at(-1).onerror()');expect(page.locator('#offlineBanner')).to_be_visible()
            page.locator('#reconnect').click();expect(page.locator('#offlineBanner')).to_be_hidden()
            expect(page.locator('.message.user')).to_have_count(1)
            finish(ctx,'retry retains idempotency key and draft; refresh and reconnect do not duplicate messages')

            ctx,page,f=setup(messages=messages,tasks=[task])
            page.locator('#openMenu').click();page.get_by_role('tab',name='计划',exact=True).click()
            page.screenshot(path=str(output/'mobile-plans.png'),full_page=True)
            page.get_by_role('button',name='暂停计划',exact=True).click();expect(page.get_by_role('button',name='恢复计划',exact=True)).to_be_visible()
            page.get_by_role('button',name='恢复计划',exact=True).click();expect(page.get_by_role('button',name='暂停计划',exact=True)).to_be_visible()
            page.get_by_role('button',name='取消…',exact=True).click();page.get_by_role('button',name='只取消这一次',exact=True).click()
            page.wait_for_function('document.querySelectorAll(".plan-card button:disabled").length===0')
            page.get_by_role('button',name='取消…',exact=True).click();page.get_by_role('button',name='取消整个计划',exact=True).click()
            expect(page.locator('.plan-card')).to_contain_text('已取消')
            assert [b['action'] for p,b in f['posts']]==['pause','resume','cancel_once','cancel_plan']
            page.get_by_role('tab',name='连接',exact=True).click();page.locator('#openPairing').click()
            f['pair_fail']=True;page.locator('#pairCode').fill('abcd-1234-efgh');page.locator('#pairSubmit').click()
            expect(page.locator('#pairError')).to_contain_text('失效')
            f['pair_fail']=False;page.locator('#pairSubmit').click()
            expect(page.locator('#pairDialog')).not_to_be_visible();expect(page.locator('[data-message-id="shared"]')).to_be_visible()
            expect(page.locator('[data-message-id="u1"]')).to_have_count(0)
            assert f['posts'][-1][1]=={'code':'ABCD1234EFGH'}
            expect(page.locator('#pairHintText')).to_contain_text('同步')
            finish(ctx,'plan pause/resume/scoped cancellation use revisions; failed pairing retries; pairing clears old conversation')

            ctx,page,f=setup()
            f['owner']={'device_id':'desktop'}
            page.locator('#voiceButton').click();expect(page.locator('#voiceDialog')).to_be_visible()
            assert page.evaluate('__voiceStarts')==0
            page.locator('#takeoverVoice').click();expect(page.locator('#voiceStrip')).to_be_visible()
            page.wait_for_function('__voice?.connected')
            assert f['posts'][-1][0]=='/api/voice/takeover'
            page.evaluate('__voice.options.onEvent({type:"transcript",text:"Do not append me"})')
            expect(page.locator('.message')).to_have_count(0)
            page.locator('#stopVoice').click();expect(page.locator('#voiceStrip')).not_to_be_visible()
            page.evaluate('window.__denyMic=true');page.locator('#voiceButton').click()
            expect(page.locator('#sendError')).to_contain_text('麦克风权限')
            expect(page.locator('#messageInput')).to_be_enabled()
            finish(ctx,'voice requires explicit takeover; no local transcript duplication; mic denial preserves text mode')

            ctx,page,f=setup(pairing_required=True)
            assert f['message_gets']==0
            expect(page.locator('#sendButton')).to_be_disabled()
            page.locator('#pairHint').click();expect(page.locator('#pairDialog')).to_be_visible()
            page.locator('#pairCode').fill('ABCD-EFGH-2345');page.locator('#pairSubmit').click()
            page.wait_for_function('__mobileTest.state.ready')
            submit(page,'谢谢');expect(page.locator('.message.user')).to_have_count(1)
            finish(ctx,'remote untrusted device never reads private APIs before pairing; paired chat becomes usable')

            older=[{'id':1,'role':'user','text':'更早的需求','kind':'text','version':1}]
            recent=[{'id':101,'role':'assistant','text':'最近的对话','kind':'text','version':1}]
            ctx,page,f=setup(messages=recent,older=older)
            page.locator('#loadEarlier').click();expect(page.locator('[data-message-id="1"]')).to_be_visible()
            assert page.locator('.message').first.get_attribute('data-message-id')=='1'
            expect(page.locator('#loadEarlier')).to_be_hidden()
            f['cursor']=999;page.evaluate('__mobileTest.refreshMessages()')
            assert page.evaluate('__mobileTest.state.cursor')==1, 'snapshot skipped undelivered SSE'
            generated={'id':102,'role':'assistant','text':'调用结构已校验，Schema 已通过 control.climate.set_temperature',
                       'kind':'task.generated','version':1,'raw_source':{'data':{'calls':[{'tool':'climate_set_temperature','arguments':{'zone':'all','temperature_c':21}}]}}}
            stream(page,generated,1000)
            expect(page.locator('[data-message-id="102"]')).to_contain_text('已模拟设置：全部分区21度')
            expect(page.locator('body')).not_to_contain_text('Schema')
            expect(page.locator('body')).not_to_contain_text('control.climate')
            stream(page,{**generated,'id':103,'raw_source':{'data':{'calls':[{'tool':'windows_open','arguments':{}}]}}},1001)
            expect(page.locator('[data-message-id="103"]')).to_contain_text('已模拟完成这次调整')
            stream(page,{'id':104,'role':'assistant','text':'Schema failure','kind':'task.failed','version':1},1002)
            expect(page.locator('[data-message-id="104"]')).to_contain_text('参数校验失败')
            page.screenshot(path=str(output/'mobile-projection.png'),full_page=True)
            page.evaluate('__streams.at(-1).dispatchEvent(new MessageEvent("revoked",{data:"{}"}))')
            expect(page.locator('.message')).to_have_count(0);expect(page.locator('#sendError')).to_contain_text('失效')
            expect(page.locator('#sendButton')).to_be_disabled()
            finish(ctx,'older history stays ordered; snapshot preserves replay cursor; concise factual projection; revocation clears private UI')

            mixed_text='设置空调温度：已模拟下发成功。模拟查询结论：空调当前设定温度为22度；无车内温度传感器，无法读取实际车内温度。'
            mixed={'id':201,'role':'assistant','text':mixed_text,'kind':'task.generated','version':1,
                   'raw_source':{'data':{'outcome':'mixed','message':mixed_text,'calls':[{'tool':'control.climate.set_temperature','arguments':{'zone':'all','temperature_c':21}}]}}}
            ctx,page,f=setup(messages=[{'id':200,'role':'user','text':'空调设成21度，再看看车里现在多少度','kind':'text','version':1},mixed])
            result=page.locator('[data-message-id="201"]')
            expect(result).to_contain_text('已模拟设置：全部分区21度')
            expect(result).to_contain_text('当前设定温度为22度')
            expect(result).to_contain_text('无车内温度传感器')
            expect(result).to_contain_text('无法读取实际车内温度')
            expect(result).not_to_contain_text('实际车内温度为22度')
            expect(page.locator('body')).not_to_contain_text('SECRET TOOL TRACE')
            page.screenshot(path=str(output/'mobile-mixed-query.png'),full_page=True)
            stream(page,{**mixed,'id':202,'text':'打开车窗：已模拟下发成功。模拟查询结论：模拟电量为72%，续航为310公里。',
                         'raw_source':{'data':{'outcome':'mixed','calls':[{'tool':'control.windows.open','arguments':{'window':'all'}}]}}},1200)
            expect(page.locator('[data-message-id="202"]')).to_contain_text('已模拟完成这次调整')
            expect(page.locator('[data-message-id="202"]')).to_contain_text('模拟电量为72%')
            expect(page.locator('[data-message-id="202"]')).to_contain_text('续航为310公里')
            stream(page,{**mixed,'id':203,'text':'查询到的是设定温度22度，不是实际车内温度；没有传感器读数。'},1201)
            expect(page.locator('[data-message-id="203"]')).to_contain_text('不是实际车内温度')
            expect(page.locator('[data-message-id="203"]')).to_contain_text('没有传感器读数')
            finish(ctx,'mixed temperature and other controls preserve query values and sensor limits; missing marker remains factual; process_events ignored')

            limitations=[
                {'id':301,'role':'assistant','text':'根据模拟查询数据：Schema 已通过，空调设定温度22度。无车内温度传感器，不能把设定温度当作实际车内温度。','kind':'task.completed','version':1},
                {'id':302,'role':'assistant','text':'仅完成部分请求：调用结构已通过校验，已模拟打开车窗。未完成：车内温度查询没有传感器读数，无法确认实际温度。','kind':'task.partial','version':1},
                {'id':303,'role':'assistant','text':'根据模拟数据，无法完成请求：control.suspension.set_height 不支持；本车未配备空气悬架，无法升高车身。','kind':'task.unsupported','version':1},
                {'id':304,'role':'assistant','text':'任务未完成：Schema 校验失败，温度超出16至30度的范围；没有生成这次调整。','kind':'task.failed','version':1},
            ]
            ctx,page,f=setup(messages=limitations)
            expect(page.locator('[data-message-id="301"]')).to_contain_text('不能把设定温度当作实际车内温度')
            expect(page.locator('[data-message-id="301"]')).to_contain_text('无车内温度传感器')
            expect(page.locator('[data-message-id="302"]')).to_contain_text('已模拟打开车窗')
            expect(page.locator('[data-message-id="302"]')).to_contain_text('未完成：车内温度查询没有传感器读数')
            expect(page.locator('[data-message-id="303"]')).to_contain_text('本车未配备空气悬架，无法升高车身')
            expect(page.locator('[data-message-id="304"]')).to_contain_text('温度超出16至30度的范围')
            expect(page.locator('[data-message-id="304"]')).to_contain_text('没有生成这次调整')
            expect(page.locator('body')).not_to_contain_text('Schema')
            expect(page.locator('body')).not_to_contain_text('control.suspension')
            page.screenshot(path=str(output/'mobile-query-limitations.png'),full_page=True)
            finish(ctx,'query, partial, unsupported and failed projections retain facts, incomplete actions and reasons without schema/tool identifiers')

            ctx,page,f=setup()
            page.evaluate('''() => {const original=window.fetch;window.fetch=(path,options)=>path==='/api/voice-owner'?new Promise(resolve=>{window.__resolveOwner=()=>resolve(new Response('{"owner":null}',{headers:{'Content-Type':'application/json'}}))}):original(path,options)}''')
            page.locator('#voiceButton').click();page.wait_for_function('!!window.__resolveOwner')
            page.evaluate('__mobileTest.stopVoice(); __resolveOwner()')
            page.wait_for_function('!__mobileTest.state.voiceStarting')
            assert page.evaluate('__voiceStarts')==0
            finish(ctx,'stopping during owner lookup cancels delayed microphone startup')

            ctx,page,f=setup(320,messages=messages,tasks=[task])
            page.set_viewport_size({'width':320,'height':420});page.locator('#messageInput').fill('主驾')
            no_overflow(page)
            bounds=page.locator('#sendButton').bounding_box();assert bounds['y']+bounds['height']<=420
            chrome_in_view(page,'320: question with keyboard-height viewport')
            page.locator('#openMenu').click();no_overflow(page)
            page.get_by_role('tab',name='连接',exact=True).click();page.locator('#openPairing').click();no_overflow(page)
            page.screenshot(path=str(output/'mobile-small-keyboard-pairing.png'),full_page=True)
            page.emulate_media(reduced_motion='reduce')
            assert page.locator('.orb-hero').evaluate('n=>getComputedStyle(n).animationName')=='none'
            finish(ctx,'320px keyboard-height viewport, sheets, safe layout and reduced-motion support')
            assert not errors, errors
        finally:
            browser.close()
            (output/'report.json').write_text(json.dumps({'checks':checks,'errors':errors,'layout_bounds':layouts,'scope':'mocked HTTP, SSE and voice; not a real phone/backend/voice acceptance'},ensure_ascii=False,indent=2))
    print(f'{len(checks)} contract groups passed. Artifacts: {output}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=Path('/tmp/personal-agent-mobile'))
    run(parser.parse_args().output_dir)
