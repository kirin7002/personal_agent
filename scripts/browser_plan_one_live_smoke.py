"""Opt-in live browser/production-service check; uses only mock vehicle controls."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    page=browser.new_page(viewport={'width':1280,'height':950})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
    expect(page.locator('#hermesStatus')).to_have_text('API 已连接',timeout=20000)
    page.locator('#message').fill('把所有分区空调设为23度')
    page.locator('#sendButton').click()
    button=page.locator('.revise-button').first
    expect(button).to_be_visible(timeout=10000)
    button.click()
    page.locator('#message').fill('改成21度')
    with page.expect_response(lambda r:'/revise' in r.url and r.request.method=='POST',timeout=25000) as pending:
        page.locator('#sendButton').click()
    response=pending.value
    assert response.status==202,response.text()
    changed=response.json(); tid=changed['id']
    row=page.locator(f'[data-task-id="{tid}"]')
    expect(row.locator('.task-status')).to_have_text('模拟成功',timeout=120000)
    task=page.request.get(f'http://127.0.0.1:8765/api/tasks/{tid}').json()
    assert task['revision']==1 and task['calls'][0]['arguments']=={'zone':'all','temperature_c':21}
    assert len(page.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks'])==1
    page.locator('#message').fill('谢谢');page.locator('#sendButton').click()
    assert len(page.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks'])==1
    page.locator('#message').fill('10秒后查询空调温度')
    page.locator('#sendButton').click()
    expect(page.locator('.task-status').filter(has_text='等待').last).to_be_visible(timeout=5000)
    tasks=page.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks']
    waiting=next(t for t in tasks if t['status']=='waiting')
    cancelled=page.request.post(f"http://127.0.0.1:8765/api/tasks/{waiting['id']}/cancel").json()
    assert cancelled['status']=='cancelled'
    assert not errors,errors
    page.screenshot(path=str(ROOT/'test-results/plan-one-live-page.png'),full_page=True)
    report={'passed':True,'actual_browser':True,'actual_hermes':True,'final_arguments':task['calls'][0]['arguments'],
            'same_task_revision':task['revision'],'thanks_created_no_task':True,'timer_wait_visible':True,'timer_cancelled':True,'javascript_errors':errors}
    (ROOT/'test-results/plan-one-live-page.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    browser.close()
