"""Desktop sharing contract in real Chrome, with mocked API/SSE/audio (no model or microphone).
Run: .venv/bin/python scripts/browser_desktop_sharing_smoke.py
Screenshots go to /tmp/personal-agent-desktop-{sharing,pairing}.png.
"""
import json
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
INIT = """
window.__streams = []; window.__sockets = []; window.__micCalls = 0; window.__tracksStopped = 0;
window.EventSource = class {
  constructor(url) { this.url = url; window.__streams.push(this); queueMicrotask(() => this.onopen?.({})); }
  addEventListener(type, callback) { this[type] = callback; }
  close() { this.closed = true; }
  emit(value) { if (!this.closed) this.onmessage?.({data: JSON.stringify(value)}); }
};
window.WebSocket = class {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; window.__sockets.push(this); }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = 3; this.onclose?.({}); }
  emit(value) { this.onmessage?.({data: JSON.stringify(value)}); }
};
Object.defineProperty(navigator, 'mediaDevices', {value: {getUserMedia: async () => {
  window.__micCalls++; return {getTracks: () => [{stop: () => window.__tracksStopped++}]};
}}});
const node = () => ({connect() {return this;}, disconnect() {}, gain: {}, port: {}});
window.AudioWorkletNode = class {constructor() {return node();}};
window.AudioContext = class {
  constructor() {this.state = 'running'; this.currentTime = 0; this.destination = {}; this.audioWorklet = {addModule: async () => {}};}
  async resume() {} async close() {this.state = 'closed';}
  createMediaStreamSource() {return node();} createGain() {return node();}
  createBuffer(channels, length, rate) {return {duration: length/rate, getChannelData: () => new Float32Array(length)};}
  createBufferSource() {return {...node(), start() {}, stop() {}};}
};
"""


def msg(id, role, text, version=1, source='mobile', device='phone'):
    return dict(id=int(id.removeprefix('m')) if isinstance(id, str) else id, role=role, text=text, kind='text', source=source, device_id=device,
                task_id='task1', plan_id=None, version=version)


def main():
    fixture = dict(messages=[msg('m1', 'user', '把空调调到23度'), msg('m2', 'assistant', '要调哪个分区？')],
                   cursor=2, posts=[], deletes=[], plans=[], process_events=[], revoked=False, unpaired=False, requests=[], fail_input=False, events={}, owner={'device_id': 'phone'},
                   devices=[dict(id='desktop', name='当前电脑'), dict(id='phone', name='我的手机')])
    task = dict(id='task1', goal='把空调调到23度', status='awaiting_input', revision=1, created=1800000000,
                source='mobile', question='要调哪个分区？', history=[], results=[],
                output={'temperature_c': 23}, process={'steps': [dict(id='step1', title='读取空调状态',
                    status='success', live=False, started_at=1800000000, duration_ms=42,
                    duration_kind='measured_tool', observation={'action': '查询模拟车辆', 'input': '全部分区',
                    'output': '主驾 22 度', 'data': '{"temperature_c":22}'})], 'timing': {'total_ms': 42}})
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        context.add_init_script(INIT)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(r):
            path = urlparse(r.request.url).path
            method = r.request.method
            fixture['requests'].append((method, path))
            if path == '/':
                return r.fulfill(content_type='text/html', body=(ROOT/'web/index.html').read_text())
            if path.startswith('/static/'):
                f = ROOT/'web'/path.removeprefix('/static/')
                body = f.read_text()
                if f.name == 'app.js':
                    body += '\nwindow.__test = {state, refreshMessages, refreshVoiceOwner, poll, VoiceSession};'
                return r.fulfill(content_type='application/javascript' if f.suffix == '.js' else 'text/css', body=body)
            if method == 'DELETE':
                fixture['deletes'].append(path)
                fixture['devices'] = [device for device in fixture['devices'] if device['id'] != path.rsplit('/', 1)[-1]]
                return r.fulfill(json={'ok': True})
            if method == 'POST':
                data = r.request.post_data_json
                fixture['posts'].append((path, data))
                if path == '/api/session/reset':
                    fixture['revoked'] = False
                    return r.fulfill(json={'ready': False})
                if path == '/api/plans':
                    plan = dict(id='p1', revision=0, goal=data['goal'], schedule=data['schedule'], status='pending',
                                timezone='Asia/Shanghai', next_run_at=1800000000, runs=[])
                    fixture['plans'].append(plan)
                    return r.fulfill(json=plan)
                if path == '/api/plans/p1/action':
                    plan = fixture['plans'][0]
                    assert plan['revision'] == data['revision']
                    plan['revision'] += 1
                    plan['status'] = {'pause': 'paused', 'resume': 'pending', 'cancel_plan': 'cancelled'}[data['action']]
                    return r.fulfill(json=plan)
                if path == '/api/pairing/code':
                    return r.fulfill(json={'code': 'ABCD-EFGH-JKLM', 'expires_at': time.time() + 300,
                                          'mobile_url': 'https://assistant.example/mobile', 'trust_url': 'http://assistant.example/mobile/connect'})
                if path == '/api/voice/takeover':
                    fixture['owner'] = None
                    return r.fulfill(json={'ok': True})
                if path == '/api/conversation/input':
                    # Persist before losing the response, then deduplicate the retry.
                    if data['event_id'] not in fixture['events']:
                        fixture['events'][data['event_id']] = True
                        fixture['messages'].append(msg('m3', 'user', data['text'], source='desktop', device='desktop'))
                        fixture['cursor'] += 1
                    if fixture['fail_input']:
                        return r.abort('failed')
                    return r.fulfill(json={'status': 'accepted', 'message': '已收到', 'task': task})
            if fixture['revoked'] and path == '/api/conversation/messages':
                return r.fulfill(status=401, json={'detail': '设备授权已失效'})
            all_messages = fixture['messages']
            before = parse_qs(urlparse(r.request.url).query).get('before', [None])[0]
            eligible = [m for m in all_messages if before is None or m['id'] < int(before)]
            visible = eligible[-2:]
            values = {
                '/api/session': {'device': {'id': 'desktop', 'name': '当前电脑'}, 'pairing_required': fixture['unpaired']},
                '/api/config': {'hermes_configured': False, 'voice_configured': True},
                '/api/catalog': {'profiles': [{'id': 'demo', 'title': '模拟车辆'}], 'scenarios': [],
                                 'counts': {'control': 1, 'manual': 0, 'status': 0}},
                '/api/session/profile': {'profile_id': 'demo'},
                '/api/catalog/tree': {'items': [], 'next_offset': None},
                '/api/events/cursor': {'cursor': 0}, '/api/plan-events': {'events': []},
                '/api/tasks': {'tasks': [task], 'focus_task_id': 'task1'},
                '/api/conversation/messages': {'messages': visible, 'cursor': fixture['cursor'], 'has_more': len(eligible) > 2, 'next_before': visible[0]['id'] if len(eligible) > 2 else None, 'process_events': fixture['process_events']},
                '/api/voice-owner': {'owner': fixture['owner']}, '/api/devices': {'devices': fixture['devices']},
                '/api/plans': {'plans': fixture['plans'], 'timezone': 'Asia/Shanghai'},
            }
            if path not in values:
                raise AssertionError(f'Unexpected API: {method} {path}')
            return r.fulfill(json=values[path])

        page.route('**/*', route)
        page.goto('http://127.0.0.1:18765/')
        page.wait_for_function('window.__test?.state.initialized')
        expect(page.locator('[data-message-id]')).to_have_count(2)
        expect(page.locator('#replyLabel')).to_contain_text('要调哪个分区')
        assert page.evaluate('__micCalls') == 0
        assert fixture['posts'] == [('/api/session', {'name': '电脑观察台', 'kind': 'desktop'})]
        assert page.locator('#deviceList').count() == 1
        page.get_by_role('button', name='查看结构 ↗').click()
        expect(page.locator('#jsonCode')).to_contain_text('temperature_c')
        page.get_by_role('button', name='关闭调用预览', exact=True).click()
        page.locator('#openPlans').click()
        expect(page.locator('#planTimezone')).to_contain_text('Asia/Shanghai')
        page.get_by_role('button', name='关闭周期计划', exact=True).click()
        assert fixture['posts'] == [('/api/session', {'name': '电脑观察台', 'kind': 'desktop'})], 'Reading must not change focus/playback/ownership'
        expect(page.locator('.step-main')).to_contain_text('主驾 22 度')
        expect(page.locator('.step-duration')).to_have_text('42 ms')

        def emit(message, cursor):
            page.evaluate("value => __streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).emit(value)",
                          {'cursor': cursor, 'message': message})

        updated = msg('m2', 'assistant', '请选主驾或全部', version=2)
        emit(updated, 3); emit(updated, 3)
        emit(msg('m2', 'assistant', '旧版本不能覆盖', version=1), 2)
        expect(page.locator('[data-message-id]')).to_have_count(2)
        expect(page.locator('[data-message-id="2"]')).to_contain_text('请选主驾或全部')
        # A stale snapshot cannot overwrite a newer SSE message.
        page.evaluate('__test.refreshMessages()')
        expect(page.locator('[data-message-id="2"]')).to_contain_text('请选主驾或全部')
        fixture['messages'][1] = updated; fixture['cursor'] = 3
        page.evaluate("__streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).onerror()")
        expect(page.locator('#syncStatus')).to_contain_text('连接中断')
        page.wait_for_function("__streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).url.endsWith('after=3') && !__streams.at(-1).closed")
        assert page.evaluate('__micCalls') == 0

        fixture['fail_input'] = True
        page.locator('#message').fill('全部')
        page.locator('#sendButton').click()
        page.wait_for_function('!__test.state.busy')
        expect(page.locator('#message')).to_have_value('全部')
        first = [body for path, body in fixture['posts'] if path == '/api/conversation/input'][-1]
        fixture['fail_input'] = False
        page.locator('#sendButton').click()
        expect(page.locator('#message')).to_have_value('')
        submitted = [body for path, body in fixture['posts'] if path == '/api/conversation/input'][-1]
        assert first['event_id'] == submitted['event_id']
        emit(fixture['messages'][-1], fixture['cursor'])
        expect(page.locator('[data-message-id="3"]')).to_have_count(1)
        expect(page.locator('#conversationMessages .user')).to_have_count(2)
        assert len(fixture['messages']) == 3
        page.reload(); page.wait_for_function('window.__test?.state.initialized')
        expect(page.locator('[data-message-id]')).to_have_count(3)
        page.screenshot(path='/tmp/personal-agent-desktop-sharing.png', full_page=True)
        # A phone voice trace is observable without opening any microphone or WebSocket.
        process_event = {'id': 5, 'seq': 5, 'cursor': 5, 'kind': 'voice.process', 'payload': {'device_id': 'phone', 'device_name': '我的手机',
            'step': {'id': 'vp1', 'connection_id': 'phone-call', 'title': '取得用户语音转写', 'detail': '全部分区', 'status': 'success', 'started_at': 1800000000, 'duration_ms': 42}}}
        fixture['process_events'] = [process_event]
        page.evaluate("value => __streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).emit(value)", process_event)
        expect(page.locator('.voice-process')).to_contain_text('取得用户语音转写')
        page.evaluate('__test.refreshMessages()')
        expect(page.locator('[data-voice-step="vp1"]')).to_have_count(1)
        stale = json.loads(json.dumps(process_event)); stale.update(id=4, seq=4, cursor=4)
        stale['payload']['step']['detail'] = '旧步骤不得覆盖'
        page.evaluate("value => __streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).emit(value)", stale)
        expect(page.locator('.voice-process')).not_to_contain_text('旧步骤不得覆盖')
        page.reload(); page.wait_for_function('window.__test?.state.initialized')
        expect(page.locator('[data-voice-step="vp1"]')).to_have_count(1)
        expect(page.locator('.voice-process')).to_contain_text('全部分区')
        expect(page.locator('.voice-process > summary')).to_contain_text('我的手机')
        page.screenshot(path='/tmp/personal-agent-desktop-voice-observer.png', full_page=True)
        assert page.evaluate('__micCalls') == 0 and page.evaluate('__sockets.length') == 0
        cursor_before = page.evaluate('__test.state.messageCursor')
        fixture['cursor'] = 90
        page.evaluate('__test.refreshMessages()')
        assert page.evaluate('__test.state.messageCursor') == cursor_before, 'Snapshots must not skip replay gaps'
        fixture['cursor'] = cursor_before


        page.locator('#openPhone').click()
        expect(page.locator('.device-row')).to_have_count(2)
        expect(page.locator('.device-row').filter(has_text='当前电脑').get_by_role('button')).to_have_count(0)
        page.locator('#createPairing').click()
        expect(page.locator('#pairingCode')).to_have_text('ABCD-EFGH-JKLM')
        expect(page.locator('#pairingLink')).to_have_attribute('href', 'https://assistant.example/mobile')
        expect(page.locator('#pairingExpiry')).to_contain_text('5 分钟')
        expect(page.locator('#pairingTrustLink')).to_have_attribute('href', 'http://assistant.example/mobile/connect')
        page.screenshot(path='/tmp/personal-agent-desktop-pairing.png', full_page=True)
        page.evaluate('__test.state.pairingExpires = Date.now() - 1000')
        expect(page.locator('#pairingExpiry')).to_contain_text('已过期')
        expect(page.locator('#pairingLink')).to_be_hidden()
        page.get_by_role('button', name='解除连接').click()
        expect(page.locator('.device-row')).to_have_count(1)
        assert fixture['deletes'] == ['/api/devices/phone']
        page.get_by_role('button', name='关闭连接手机', exact=True).click()

        page.locator('#voiceButton').click()
        page.wait_for_function('!__test.state.voiceStarting')
        assert page.evaluate('__micCalls') == 0, 'Owned voice must not request desktop microphone'
        assert not any(path == '/api/voice/takeover' for path, _ in fixture['posts'])
        page.locator('#takeoverVoice').click()
        page.wait_for_function('__sockets.length === 1')
        assert page.evaluate('__micCalls') == 1
        assert sum(path == '/api/voice/takeover' for path, _ in fixture['posts']) == 1
        page.evaluate("__sockets.at(-1).emit({type: 'voice.ready'})")
        expect(page.locator('#voiceStatus')).to_have_text('已连接')
        page.evaluate("__sockets.at(-1).emit({type: 'user.transcript', text: '全部'}); __sockets.at(-1).emit({type: 'assistant.delta', text: '不要本地追加'})")
        expect(page.locator('[data-message-id]')).to_have_count(3)
        assert page.evaluate('__sockets.at(-1).sent.length') == 0, 'Read/text events cannot acknowledge audio'
        page.evaluate("__sockets.at(-1).emit({type:'audio', audio:btoa(String.fromCharCode(0,0,0,0))})")
        assert page.evaluate('__sockets.at(-1).sent') == [{'type': 'playback', 'busy': True}]
        page.evaluate("__sockets.at(-1).emit({type:'voice.invalidate'})")
        assert page.evaluate('__sockets.at(-1).sent.at(-1)') == {'type': 'playback', 'busy': False}
        expect(page.locator('[data-message-id]')).to_have_count(3)
        page.evaluate("__sockets.at(-1).emit({type:'voice.released'})")
        expect(page.locator('#voiceButton')).to_have_attribute('aria-pressed', 'false')
        assert page.evaluate('__tracksStopped') == 1
        assert page.evaluate('__sockets.at(-1).readyState') == 3
        page.evaluate("__sockets.at(-1).emit({type:'audio', audio:'AAAA'})")
        assert page.evaluate('__sockets.at(-1).sent.length') == 2
        # Ownership can change after the preflight request: the busy event still tears down capture.
        page.locator('#voiceButton').click(); page.wait_for_function('__sockets.length === 2')
        page.evaluate("__sockets.at(-1).emit({type:'voice.busy', message:'其他设备通话中'})")
        expect(page.locator('#voiceStatus')).to_have_text('其他设备通话中')
        assert page.evaluate('__tracksStopped') == 2
        assert page.evaluate('__sockets.at(-1).sent') == []
        # The unchanged plan editor still creates, pauses, resumes and cancels by revision.
        page.locator('#openPlans').click()
        page.locator('#planGoal').fill('查询车辆电量')
        page.locator('#planTime').fill('08:30')
        page.locator('#savePlan').click()
        expect(page.locator('.plan-card')).to_have_count(1)
        created = [body for path, body in fixture['posts'] if path == '/api/plans'][-1]
        assert created['schedule'] == {'frequency': 'daily', 'time': '08:30', 'weekdays': []}
        page.get_by_role('button', name='暂停', exact=True).click()
        expect(page.locator('.plan-card')).to_contain_text('已暂停')
        page.get_by_role('button', name='恢复', exact=True).click()
        expect(page.get_by_role('button', name='暂停', exact=True)).to_be_visible()
        page.locator('.plan-cancel summary').click()
        page.get_by_role('button', name='取消整个计划', exact=True).click()
        expect(page.locator('.plan-card')).to_contain_text('已取消')
        revisions = [body['revision'] for path, body in fixture['posts'] if path == '/api/plans/p1/action']
        assert revisions == [0, 1, 2]
        page.get_by_role('button', name='关闭周期计划', exact=True).click()
        # Revoked credentials require an explicit reset; reads never clear cookies themselves.
        fixture['revoked'] = True
        page.evaluate("__streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).revoked({data:'{}'})")
        expect(page.locator('#deviceRecovery')).to_be_visible()
        expect(page.locator('#sendButton')).to_be_disabled()
        assert not any(path == '/api/session/reset' for path, _ in fixture['posts'])
        page.locator('#resetDevice').click()
        page.wait_for_function('window.__test?.state.initialized && !__test.state.sessionInvalid')
        assert sum(path == '/api/session/reset' for path, _ in fixture['posts']) == 1
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        fixture['unpaired'] = True; fixture['requests'].clear()
        page.reload()
        expect(page.locator('#pairingRequired')).to_be_visible()
        assert not any(path.startswith('/api/') and path != '/api/session' for _, path in fixture['requests'])
        assert page.evaluate('__micCalls') == 0
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.locator('#openPhone').click()
        assert not any(path.startswith('/api/') and path != '/api/session' for _, path in fixture['requests'])
        assert not errors, errors
        print(json.dumps({'passed': ['canonical pagination/upsert/version/replay/reload and cursor gap retention', 'idempotent retry and draft retention',
              'pairing and other-device revocation', 'phone voice.process snapshot/live dedup/stale/reload without microphone',
              'explicit takeover / busy / released / late audio', 'task process/timing/JSON and plan create/pause/resume/cancel preserved',
              'revoked identity explicit reset; unpaired identity cannot read private APIs'],
              'javascript_errors': errors, 'screenshots': ['/tmp/personal-agent-desktop-sharing.png',
              '/tmp/personal-agent-desktop-pairing.png', '/tmp/personal-agent-desktop-voice-observer.png']}, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == '__main__':
    main()
