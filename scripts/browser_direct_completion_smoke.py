"""Verify the real one-request path and the separately configured S2S credential."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument('--expect-voice-denied',action='store_true',help='Explicitly test an account currently returning Model.AccessDenied')
options=parser.parse_args()
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
    context=browser.new_context(permissions=['microphone'],viewport={'width':1440,'height':1000})
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
    expect(page.locator('#catalogCount')).to_have_text('68 项')
    page.locator('#message').fill('把所有分区空调设为26度')
    page.locator('#sendButton').click()
    row=page.locator('.task').last
    expect(row.locator('.task-status')).to_have_text('模拟成功',timeout=60000)
    expect(row).to_contain_text('校验通过，直接通知前台')
    assert '确认并提交结论' not in row.inner_text()
    task=context.request.get('http://127.0.0.1:8765/api/tasks/'+row.get_attribute('data-task-id')).json()
    assert task['process']['timing']['direct_completion']
    assert task['process']['timing']['provider_requests']==1
    page.screenshot(path=str(ROOT/'test-results/direct-completion-chat.png'))
    if options.expect_voice_denied:
        # Current newly supplied credential has been observed to lack model access.
        # Validate a precise failure state instead of claiming a successful voice test.
        page.locator('#voiceButton').click()
        expect(page.locator('#chat')).to_contain_text('Model.AccessDenied',timeout=30000)
        expect(page.locator('#voiceStatus')).to_have_text('模型权限不足')
        expect(page.locator('#voiceButton')).to_have_attribute('aria-pressed','false')
        expect(row.locator('.task-status')).to_have_text('模拟成功')
        page.screenshot(path=str(ROOT/'test-results/s2s-model-access-denied.png'))
    assert not errors,errors
    print(json.dumps({'chat_direct_completion':True,'provider_requests':1,'s2s':'Model.AccessDenied shown explicitly' if options.expect_voice_denied else 'not_requested','javascript_errors':errors},ensure_ascii=False))
    browser.close()
