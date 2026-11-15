"""Read-only UI verification using this project's own completed benchmark session."""
import json
from pathlib import Path
import sqlite3

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
report=json.loads((ROOT/'test-results/preload-ab-direct.json').read_text())
started=report['summary']['created_at_unix']
with sqlite3.connect(ROOT/'.data/preview.sqlite3') as db:
    benchmark_session=db.execute("SELECT session_id FROM tasks WHERE source='hermes' AND created>=? GROUP BY session_id ORDER BY count(*) DESC LIMIT 1",(started,)).fetchone()[0]
    fast=db.execute("SELECT id,session_id FROM tasks WHERE session_id=? AND created>=? AND discovery_mode='auto' AND goal='把所有分区空调设为26度' AND status='generated' ORDER BY created DESC LIMIT 1",(benchmark_session,started)).fetchone()
    assert fast, 'No completed fast-path benchmark task'
    fallback=db.execute("SELECT id FROM tasks WHERE session_id=? AND discovery_mode='auto' AND profile_id='demo-no-suspension' AND status IN ('unsupported','partial') ORDER BY created DESC LIMIT 1",(fast[1],)).fetchone()
assert fast and fallback

with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    browser=p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None,headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000})
    context.add_cookies([{'name':'preview_session','value':fast[1],'url':'http://127.0.0.1:8765','httpOnly':True,'sameSite':'Strict'}])
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
    fast_row=page.locator(f'.task[data-task-id="{fast[0]}"]')
    expect(fast_row).to_contain_text('候选已预加载')
    fast_row.locator('.navigation-trace > summary').click()
    expect(fast_row).to_contain_text('预加载候选')
    assert '浏览 根目录' not in fast_row.inner_text()
    fast_row.get_by_role('button',name='查看结构 ↗').click()
    output=json.loads(page.locator('#jsonCode').inner_text())
    assert output['calls'][0]['arguments']['temperature_c']==26
    page.get_by_role('button',name='关闭调用预览').click()
    fast_row.screenshot(path=str(ROOT/'test-results/preload-fast-card.png'))
    fallback_row=page.locator(f'.task[data-task-id="{fallback[0]}"]')
    fallback_row.locator('.navigation-trace > summary').click()
    expect(fallback_row).to_contain_text('转入完整目录查证')
    expect(fallback_row).to_contain_text('模拟查询')
    fallback_row.screenshot(path=str(ROOT/'test-results/preload-fallback-card.png'))
    assert not errors,errors
    browser.close()
    print('Preload UI passed: fast-path receipt and timing, full-directory fallback, correct copied arguments. No new model calls.')
