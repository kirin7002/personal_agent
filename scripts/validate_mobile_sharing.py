"""Live desktop+mobile browser contexts, TLS pairing, and actual Hermes mock controls."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parent.parent
access=json.loads((ROOT/'.runtime/mobile-access.json').read_text())
mobile_base=access['mobile_url'].removesuffix('/mobile')
OUT=ROOT/'test-results';OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    desk=browser.new_context(viewport={'width':1440,'height':1050})
    phone=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,ignore_https_errors=True)
    d=desk.new_page();m=phone.new_page();errors=[]
    d.on('pageerror',lambda e:errors.append('desktop: '+str(e)))
    m.on('pageerror',lambda e:errors.append('phone: '+str(e)))
    try:
        d.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
        expect(d.locator('#hermesStatus')).to_have_text('API 已连接',timeout=20000)
        d.locator('#openPhone').click();d.locator('#createPairing').click()
        expect(d.locator('#pairingCode')).to_contain_text('-',timeout=5000)
        code=d.locator('#pairingCode').inner_text()
        m.goto(access['mobile_url'],wait_until='domcontentloaded')
        expect(m.locator('#pairHintText')).to_contain_text('配对码',timeout=10000)
        m.locator('#pairHint').click();m.locator('#pairCode').fill(code);m.locator('#pairSubmit').click()
        expect(m.locator('#pairDialog')).not_to_be_visible(timeout=15000)
        expect(m.locator('#messageInput')).to_be_enabled()
        d.locator('#phoneDialog [data-close]').click()
        d.locator('#message').fill('空调设为23度');d.locator('#sendButton').click()
        expect(m.locator('.message.user').filter(has_text='空调设为23度')).to_have_count(1,timeout=10000)
        expect(m.get_by_role('button',name='全部',exact=True)).to_be_visible(timeout=120000)
        m.get_by_role('button',name='全部',exact=True).click()
        expect(d.locator('#conversationMessages').get_by_text('全部',exact=True)).to_have_count(1,timeout=10000)
        expect(d.locator('.task-status').first).to_have_text('模拟成功',timeout=120000)
        expect(m.locator('.message.assistant').filter(has_text='已模拟设置：全部分区23度')).to_have_count(1,timeout=10000)
        tasks=desk.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks']
        assert len(tasks)==1 and tasks[0]['calls'][0]['arguments']=={'zone':'all','temperature_c':23}
        assert tasks[0]['process']['steps'] and tasks[0]['output']['calls']
        # Reverse direction: phone creates, desktop cancels original after question.
        m.locator('#messageInput').fill('空调设为24度');m.locator('#sendButton').click()
        expect(d.locator('#conversationMessages').get_by_text('空调设为24度',exact=True)).to_have_count(1,timeout=10000)
        expect(m.get_by_role('button',name='全部',exact=True)).to_be_visible(timeout=120000)
        d.locator('#message').fill('算了');d.locator('#sendButton').click()
        expect(m.locator('.message.assistant').filter(has_text='好，这次不调整了。')).to_have_count(1,timeout=10000)
        state=desk.request.get('http://127.0.0.1:8765/api/tasks').json()['tasks']
        assert len(state)==2 and state[0]['status']=='cancelled'
        before=phone.request.get(mobile_base+'/api/conversation/messages').json()['messages']
        m.reload(wait_until='domcontentloaded')
        expect(m.locator('.message.user').filter(has_text='空调设为23度')).to_have_count(1,timeout=10000)
        after=phone.request.get(mobile_base+'/api/conversation/messages').json()['messages']
        assert [(x['id'],x['text']) for x in before]==[(x['id'],x['text']) for x in after]
        assert not errors,errors
        d.screenshot(path=str(OUT/'desktop-mobile-shared-live.png'),full_page=True)
        m.screenshot(path=str(OUT/'mobile-shared-live.png'),full_page=True)
        report={'passed':True,'actual_hermes':True,'independent_browser_contexts':2,'phone_viewport':[390,844],
                'tls_browser_test_ignored_errors':True,'physical_phone_tested':False,
                'desktop_to_phone':True,'phone_to_desktop':True,'same_task_reply':True,'desktop_full_process_and_json':True,
                'reload_without_duplicate_messages':True,'javascript_errors':errors}
        (OUT/'mobile-sharing-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))
    finally:
        response=desk.request.get('http://127.0.0.1:8765/api/tasks')
        if response.ok:
            for task in response.json()['tasks']:
                if task['status'] in {'running','waiting','awaiting_input'}:desk.request.post(f"http://127.0.0.1:8765/api/tasks/{task['id']}/cancel",data={})
        browser.close()
