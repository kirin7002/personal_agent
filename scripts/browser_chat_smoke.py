"""Real browser checks for the chat UI and observable per-step timings."""
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'test-results'
OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
    context = browser.new_context(viewport={'width':1440,'height':1000})
    page = context.new_page()
    errors, streams = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('response', lambda r: streams.append(r.status) if '/api/events/stream' in r.url else None)
    page.goto('http://127.0.0.1:8765', wait_until='domcontentloaded')
    expect(page.locator('#catalogCount')).to_have_text('68 项')
    page.screenshot(path=str(OUT/'chat-welcome.png'))
    page.locator('#openCatalog').click()
    expect(page.locator('#catalogDialog')).to_be_visible()
    expect(page.locator('.catalog-tool')).to_have_count(3)
    page.get_by_role('button', name='浏览下级 →').first.click()
    expect(page.locator('.catalog-tool')).to_have_count(8)
    page.get_by_role('button', name='关闭能力目录').click()
    results = []

    def submit(goal):
        page.locator('#message').fill(goal)
        page.locator('#sendButton').click()
        row = page.locator('.task').last
        expect(row).to_contain_text(goal)
        return row

    def finish(row, statuses=('模拟成功',)):
        page.wait_for_function("([id,states]) => states.includes(document.querySelector(`[data-task-id=\"${id}\"] .task-status`)?.textContent)", arg=[row.get_attribute('data-task-id'), list(statuses)], timeout=140000)
        tid = row.get_attribute('data-task-id')
        task = context.request.get(f'http://127.0.0.1:8765/api/tasks/{tid}').json()
        assert task['process']['timing']['precise_tool_timers'], task['process']
        assert any(s['duration_kind']=='measured_tool' for s in task['process']['steps'])
        assert any(s['duration_kind']=='boundary_wait' for s in task['process']['steps'])
        results.append({'goal':task['goal'],'status':task['status'],'timing':task['process']['timing'],'steps':task['process']['steps']})
        (OUT/'chat-timing-smoke.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        return task

    row = submit('把所有分区空调设为26度')
    expect(row.locator('[data-timer-start]').first).to_be_visible()
    initial = row.locator('.total-time').inner_text()
    page.wait_for_timeout(350)
    assert row.locator('.total-time').inner_text() != initial
    # User toggles must survive progress updates and the global composer must retain focus/draft.
    row.locator('.process > summary').click()
    page.locator('#message').fill('我的下一条草稿')
    task = finish(row)
    expect(row.locator('.process')).not_to_have_attribute('open', '')
    expect(page.locator('#message')).to_have_value('我的下一条草稿')
    expect(page.locator('#message')).to_be_focused()
    assert task['output']['calls'][0]['arguments']['temperature_c']==26
    row.locator('.process > summary').click()
    expect(row).to_contain_text('工具实测')
    expect(row).to_contain_text('模型 / 网络 / 调度区间')
    row.get_by_role('button', name='查看结构 ↗').click()
    assert json.loads(page.locator('#jsonCode').inner_text())['mode']=='mock'
    page.get_by_role('button', name='关闭调用预览').click()
    page.screenshot(path=str(OUT/'chat-fast-timing.png'))
    print('Fast task and per-step timers passed', flush=True)

    # Missing parameter -> same-task continuation via the primary chat composer.
    row = submit('把空调温度调一下')
    expect(row.locator('.task-status')).to_have_text('等待补充', timeout=90000)
    row.get_by_role('button', name='补充信息 →').click()
    expect(page.locator('#replyContext')).to_be_visible()
    tid = row.get_attribute('data-task-id')
    page.locator('#message').fill('所有分区26度')
    page.locator('#sendButton').click()
    task = finish(row)
    assert task['id']==tid and len(task['history'])==1
    assert task['process']['timing']['human_wait_ms']>0
    expect(page.locator('#replyContext')).to_be_hidden()
    print('Clarification and same-task continuation passed', flush=True)

    page.locator('#openScenarios').click()
    page.locator('#profileSelect').select_option('demo-no-suspension')
    expect(page.locator('#toast')).to_contain_text('模拟配置已切换')
    page.get_by_role('button', name='关闭能力目录').click()
    row = submit('把空气悬架升高一档')
    task = finish(row, ('不支持', '部分完成', '未完成'))
    assert any(r['kind'] in ('manual','status') and r['status']=='returned_mock' for r in task['results'])
    expect(row).to_contain_text('转入完整目录查证')
    page.screenshot(path=str(OUT/'chat-failure-timing.png'))
    print('Failure diagnosis and catalog fallback passed', flush=True)

    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#message').fill('新草稿')
    page.screenshot(path=str(OUT/'chat-mobile.png'))
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('.task')).to_have_count(3)
    expect(page.locator('.task .user').first).to_contain_text('26度')
    assert streams and all(x==200 for x in streams), streams
    assert not errors, errors
    (OUT/'chat-timing-smoke.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print('Chat UI passed: real model x3, live timers, SSE, draft/focus, clarification, fallback, JSON drawer, catalog, mobile, reload.', flush=True)
    browser.close()
