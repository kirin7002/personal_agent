"""Exercise missing-prefetch fallback through real Hermes and the chat UI."""
import json
from pathlib import Path
import time
import uuid

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 context=browser.new_context(viewport={'width':1440,'height':1050})
 page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded');expect(page.locator('#catalogCount')).to_have_text('68 项')
 r=context.request.post('http://127.0.0.1:8765/api/tasks',data={'goal':'查询当前空调温度','discovery_mode':'off','request_key':uuid.uuid4().hex})
 assert r.ok,r.status
 tid=r.json()['id'];row=page.locator(f'.task[data-task-id="{tid}"]')
 expect(row.locator('.task-status')).to_have_text('查询完成',timeout=90000)
 if not row.locator('.process').evaluate('n=>n.open'):row.locator('.process > summary').click()
 expect(row).to_contain_text('全局检索车辆能力')
 expect(row).to_contain_text('完整参数已提供')
 expect(row).to_contain_text('主驾温度（℃）：22')
 t=context.request.get('http://127.0.0.1:8765/api/tasks/'+tid).json()
 assert any(n['kind']=='catalog.searched' for n in t['navigation'])
 assert not any(n['kind'] in ['catalog.browsed','catalog.described'] for n in t['navigation'])
 assert t['calls']==[] and any(r['tool_id']=='status.climate.get_state' for r in t['results'])
 search=row.locator('.process-step').filter(has_text='全局检索车辆能力').last
 search.scroll_into_view_if_needed();page.screenshot(path=str(ROOT/'test-results/tool-search-desktop.png'))
 page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 page.screenshot(path=str(ROOT/'test-results/tool-search-mobile.png'))
 (ROOT/'test-results/tool-search-fallback.json').write_text(json.dumps({'goal':t['goal'],'status':t['status'],'timing':t['process']['timing'],'navigation':t['navigation'],'results':t['results']},ensure_ascii=False,indent=2))
 assert not errors,errors
 print(json.dumps({'global_search':True,'directory_calls':0,'status':t['status'],'seconds':t['elapsed_seconds'],'javascript_errors':errors},ensure_ascii=False))
 browser.close()
