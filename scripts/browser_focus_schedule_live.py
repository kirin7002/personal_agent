"""Opt-in actual local-page acceptance; all vehicle operations remain mock."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    context=browser.new_context(viewport={'width':1280,'height':980})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def submit(value):
        expect(page.locator('#sendButton')).to_be_enabled(timeout=15000)
        page.locator('#message').fill(value)
        with page.expect_response(lambda r:'/api/conversation/input' in r.url and r.request.method=='POST',timeout=25000) as result:
            page.locator('#sendButton').click()
        assert result.value.status==200,result.value.text()
        return result.value.json()
    try:
        page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
        expect(page.locator('#hermesStatus')).to_have_text('API 已连接',timeout=20000)
        first=submit('每天早上查询空调温度')
        assert first['kind']=='clarify'
        supplied=submit('早上8点');plan=supplied['plan']
        assert plan['schedule']['time']=='08:00'
        page.locator('#openPlans').click()
        card=page.locator(f'.plan-card[data-plan-id="{plan["id"]}"]')
        expect(card).to_be_visible()
        card.get_by_role('button',name='暂停',exact=True).click()
        expect(card).to_contain_text('已暂停',timeout=15000)
        card.get_by_role('button',name='恢复',exact=True).click()
        expect(card).to_contain_text('待执行',timeout=15000)
        page.get_by_role('button',name='关闭周期计划',exact=True).click()
        asked=submit('取消这个计划');assert asked['dialog_kind']=='cancel_scope'
        cancelled=submit('整个计划');assert cancelled['plan']['status']=='cancelled'
        task=submit('空调设为23度')['task']
        row=page.locator(f'[data-task-id="{task["id"]}"]')
        expect(row.locator('.task-status')).to_have_text('等待补充',timeout=120000)
        result=submit('算了')
        assert result['task']['id']==task['id'] and result['task']['status']=='cancelled'
        expect(row.locator('.task-status')).to_have_text('已取消')
        assert len(context.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks'])==1
        assert not errors,errors
        report={'passed':True,'actual_browser':True,'actual_backend':True,'actual_hermes_question':True,
                'missing_time_clarified':True,'plan_time':plan['schedule']['time'],'timezone':plan['timezone'],
                'pause_resume':True,'cancel_scope_clarified':True,'plan_cancelled':True,
                'short_cancel_same_task':True,'javascript_errors':errors}
        (ROOT/'test-results/focus-schedule-live-page.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        page.screenshot(path=str(ROOT/'test-results/focus-schedule-live-page.png'),full_page=True)
        print(json.dumps(report,ensure_ascii=False))
    finally:
        response=context.request.get('http://127.0.0.1:8765/api/plans')
        if response.ok:
            for plan in response.json()['plans']:
                if plan['status']!='cancelled':
                    context.request.post(f"http://127.0.0.1:8765/api/plans/{plan['id']}/action",data={'action':'cancel_plan','revision':plan['revision']})
        response=context.request.get('http://127.0.0.1:8765/api/tasks')
        if response.ok:
            for task in response.json()['tasks']:
                if task['status'] in {'running','waiting','awaiting_input'}:
                    context.request.post(f"http://127.0.0.1:8765/api/tasks/{task['id']}/cancel",data={})
        browser.close()
