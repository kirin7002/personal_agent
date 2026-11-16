"""Real headless Chrome timeline regression; all REST/SSE data are mocked.

Run: .venv/bin/python scripts/browser_message_order_smoke.py
Writes screenshots and measured DOM rectangles to /tmp/personal-agent-message-order*.
No backend, model, microphone, device, or network service is accessed.
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
PREFIX = '/tmp/personal-agent-message-order'
INIT = """
window.__streams = [];
window.EventSource = class {
  constructor(url) { this.url = url; __streams.push(this); queueMicrotask(() => this.onopen?.({})); }
  addEventListener(kind, listener) { this[kind] = listener; }
  close() { this.closed = true; }
  emit(value) { if (!this.closed) this.onmessage?.({data: JSON.stringify(value)}); }
};
// Tests explicitly drive polls; unrelated timer updates cannot mask an ordering bug.
window.setInterval = () => 0;
"""


def message(ident, role='user', task_id=None, version=1, text=None):
    return dict(id=ident, version=version, role=role, task_id=task_id,
                text=text or f'{"用户需求" if role == "user" else "模拟回复"} {ident}',
                source='text', device_id='desktop', kind='chat')


def task(ident, goal=None, revision=0):
    return dict(id=ident, goal=goal or f'查询 {ident} 空调温度', revision=revision,
                status='completed', source='hermes', created=1800000000,
                question=None, summary='模拟查询结果：23℃', history=[], results=[],
                output={'temperature_c': 23}, process={'steps': [{
                    'id': 'lookup', 'title': '查询模拟温度', 'status': 'success', 'live': False,
                    'duration_ms': 42, 'duration_kind': 'measured_tool',
                    'observation': {'action': '查询', 'input': '空调', 'output': '23℃',
                                    'data': '\n'.join(f'模拟回执 {n}' for n in range(80))},
                }], 'timing': {'total_ms': 42}})


def main():
    fixture = {'tasks': [], 'messages': [], 'cursor': 0}
    errors, checks, screenshots, geometry = [], [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROME) if CHROME.exists() else None, headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1050})
        page.add_init_script(INIT)
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            if path == '/':
                return request.fulfill(content_type='text/html', body=(ROOT/'web/index.html').read_text())
            if path.startswith('/static/'):
                file = ROOT/'web'/path.removeprefix('/static/')
                body = file.read_text()
                if file.name == 'app.js':
                    body += '\nwindow.__orderTest = {state, poll, refreshMessages, renderTasks};'
                return request.fulfill(content_type='application/javascript' if file.suffix == '.js' else 'text/css', body=body)
            before = parse_qs(urlparse(request.request.url).query).get('before', [None])[0]
            records = [m for m in fixture['messages'] if before is None or int(m['id']) < int(before)]
            values = {
                '/api/session': {'device': {'id': 'desktop'}, 'pairing_required': False},
                '/api/config': {'hermes_configured': False, 'voice_configured': False},
                '/api/catalog': {'profiles': [{'id': 'demo', 'title': '模拟车辆'}], 'scenarios': [],
                                 'counts': {'control': 0, 'manual': 0, 'status': 1}},
                '/api/session/profile': {'profile_id': 'demo'},
                '/api/catalog/tree': {'items': [], 'next_offset': None},
                '/api/events/cursor': {'cursor': 0}, '/api/plan-events': {'events': []},
                '/api/tasks': {'tasks': fixture['tasks'], 'focus_task_id': None},
                '/api/conversation/messages': {'messages': records, 'cursor': fixture['cursor'], 'has_more': False, 'process_events': []},
                '/api/voice-owner': {'owner': None},
            }
            if path not in values:
                raise AssertionError(f'Unexpected request {path}')
            return request.fulfill(json=values[path])

        page.route('**/*', route)

        def reload(tasks, messages):
            fixture.update(tasks=tasks, messages=messages, cursor=100)
            page.goto('http://127.0.0.1:18765/')
            page.wait_for_function('window.__orderTest?.state.initialized')

        def emit(record):
            # Persist the newest version so a reconnect/REST refresh sees it too.
            previous = next((m for m in fixture['messages'] if int(m['id']) == int(record['id'])), None)
            if previous is None or record['version'] > previous['version']:
                fixture['messages'] = [m for m in fixture['messages'] if int(m['id']) != int(record['id'])] + [record]
            fixture['cursor'] += 1
            page.evaluate("value => __streams.filter(s => s.url.startsWith('/api/conversation/stream')).at(-1).emit(value)",
                          {'kind': 'message.upsert', 'seq': fixture['cursor'], 'message': record})

        def assert_order(expected, label):
            rows = page.evaluate("""() => [...document.querySelector('#conversationMessages').children]
              .filter(n => n.dataset.messageId || n.classList.contains('task')).map(n => {
                const r=n.getBoundingClientRect(); return {key:n.dataset.messageId ? 'm'+n.dataset.messageId : 't'+n.dataset.taskId,
                  top:r.top,bottom:r.bottom}; })""")
            assert [row['key'] for row in rows] == expected, (label, rows)
            for first, second in zip(rows, rows[1:]):
                assert first['bottom'] <= second['top'] + 1, (label, first, second)
            ids = [int(row['key'][1:]) for row in rows if row['key'].startswith('m')]
            assert ids == sorted(set(ids)), (label, ids)
            assert len([row for row in rows if row['key'].startswith('t')]) == len({t['id'] for t in fixture['tasks']})
            geometry.append({'case': label, 'rectangles': rows})
            checks.append(label)

        def screenshot(name):
            path = PREFIX + '-' + name + '.png'
            page.screenshot(path=path, full_page=True)
            screenshots.append(path)

        a, b, legacy = task('a'), task('b'), task('legacy')
        reload([a], [])  # REST task card arrives before canonical user SSE.
        emit(message(10, task_id='a', text='查询空调温度'))
        emit(message(20, 'assistant', 'a'))
        assert_order(['m10', 'ta', 'm20'], 'REST task before user SSE')
        screenshot('task-first')
        reload([a], [message(10, task_id='a'), message(20, 'assistant', 'a')])
        assert_order(['m10', 'ta', 'm20'], 'canonical messages before REST task')

        reload([a], [message(10), message(20, 'assistant', 'a')])
        assert_order(['m10', 'm20', 'ta'], 'unlinked user uses safe fallback')
        page.evaluate("""() => {
          const card=document.querySelector('[data-task-id="a"]'); window.__savedCard=card;
          window.__savedProcess=card.querySelector('.process'); __savedProcess.open=true;
          window.__savedDetail=card.querySelector('[data-step-detail]'); __savedDetail.open=true;
          __savedDetail.querySelector('pre').scrollTop=90;
        }""")
        emit(message(10, task_id='a', version=2))
        assert_order(['m10', 'ta', 'm20'], 'message version adds task link without task change')
        preserved = page.evaluate("""() => ({card:__savedCard===document.querySelector('[data-task-id="a"]'),
          process:__savedProcess===__savedCard.querySelector('.process'), open:__savedProcess.open,
          detail:__savedDetail===__savedCard.querySelector('[data-step-detail]'), expanded:__savedDetail.open,
          scroll:__savedDetail.querySelector('pre').scrollTop})""")
        assert preserved == {'card': True, 'process': True, 'open': True, 'detail': True, 'expanded': True, 'scroll': 90}, preserved
        page.evaluate("""() => { document.querySelector('#conversationMessages').prepend(__savedCard); __orderTest.renderTasks(); }""")
        assert_order(['m10', 'ta', 'm20'], 'unchanged task fingerprint still repairs position')
        assert page.evaluate('__savedProcess===__savedCard.querySelector(".process") && __savedDetail.open')
        checks.append('DOM identity, expanded process/data and inner scroll retained')

        emit(message(30, task_id='a', text='全部分区'))
        emit(message(25, 'assistant', 'a', text='迟到的旧轮次回复'))
        assert_order(['m10', 'm20', 'm25', 'm30', 'ta'], 'follow-up answer anchors card after latest user')
        emit(message(40, task_id='a', text='改成查询座椅温度'))
        fixture['tasks'] = [task('a', '查询座椅温度', revision=1)]
        page.evaluate('__orderTest.poll()')
        expect(page.locator('[data-task-id="a"] .task-context').first).to_contain_text('查询座椅温度')
        assert_order(['m10', 'm20', 'm25', 'm30', 'm40', 'ta'], 'revised target follows revised user request')

        fixture['tasks'] = [b, *fixture['tasks'], legacy]
        page.evaluate('__orderTest.poll()')
        emit(message(50, task_id='b', text='查询另一个独立任务'))
        emit(message(60, task_id='a', text='只查主驾座椅'))
        expected = ['m10', 'm20', 'm25', 'm30', 'm40', 'm50', 'tb', 'm60', 'ta', 'tlegacy']
        assert_order(expected, 'two independent task anchors and legacy fallback')
        emit(message('60', task_id='a', text='same-version replay ignored'))
        emit(message(10, task_id='b', version=1, text='stale wrong link'))
        assert_order(expected, 'duplicate numeric/string IDs and stale link replay')
        emit(message(70, text='尚未关联的下一条输入'))
        assert_order(expected[:-1] + ['m70', 'tlegacy'], 'legacy card remains after new unlinked input')
        screenshot('two-tasks')

        # A reconnect snapshot may contain both newer links and messages in arbitrary order.
        fixture['messages'] = [message(70, task_id='b', version=2), message(15, 'assistant', 'a'),
                               *reversed(fixture['messages'])]
        page.evaluate('__orderTest.refreshMessages()')
        expected = ['m10', 'm15', 'm20', 'm25', 'm30', 'm40', 'm50', 'm60', 'ta', 'm70', 'tb', 'tlegacy']
        assert_order(expected, 'out-of-order batched reconnect snapshot and link update')
        page.evaluate('__orderTest.refreshMessages()')
        page.evaluate('__orderTest.poll()')
        assert_order(expected, 'repeated snapshot and unchanged task poll are idempotent')

        reload([a], [message(i, text=('历史消息内容 ' * 30) + str(i)) for i in range(100, 140)])
        emit(message(140, task_id='a'))
        page.evaluate("""() => {
          const chat=document.querySelector('#chat'), anchor=document.querySelector('[data-message-id="118"]');
          chat.scrollTop += anchor.getBoundingClientRect().top-chat.getBoundingClientRect().top-25;
          window.__readingTop=anchor.getBoundingClientRect().top;
        }""")
        emit(message(110, task_id='a', version=2))  # Older link cannot replace latest task anchor.
        assert abs(page.locator('[data-message-id="118"]').evaluate('node => node.getBoundingClientRect().top-window.__readingTop')) < 2
        emit(message(105, 'assistant', version=2, text='旧消息更新\n' * 20))
        assert abs(page.locator('[data-message-id="118"]').evaluate('node => node.getBoundingClientRect().top-window.__readingTop')) < 2
        assert page.locator('[data-message-id="140"]').evaluate('node => node.nextElementSibling.dataset.taskId') == 'a'
        checks.append('reading position preserved across older message/link updates')
        page.evaluate('document.querySelector("#chat").scrollTop=document.querySelector("#chat").scrollHeight')
        emit(message(150, task_id='a', text='最新补充'))
        assert page.evaluate('(() => {const c=document.querySelector("#chat");return c.scrollHeight-c.scrollTop-c.clientHeight;})()') < 2
        checks.append('bottom-follow retained when latest task anchor moves')
        screenshot('reading-position')
        assert not errors, errors
        result = {'passed': checks, 'javascript_errors': errors, 'screenshots': screenshots, 'geometry': geometry}
        Path(PREFIX + '.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != 'geometry'}, ensure_ascii=False, indent=2))
        browser.close()


if __name__ == '__main__':
    main()
