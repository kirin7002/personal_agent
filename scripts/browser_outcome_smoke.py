"""Browser check for programmatic classification of a real relative-control task."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parent.parent
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=browser.new_page(viewport={'width':1440,'height':1050});errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded');expect(page.locator('#catalogCount')).to_have_text('68 项')
 page.locator('#message').fill('主驾空调温度增加一度');page.locator('#sendButton').click()
 row=page.locator('.task').last
 expect(row.locator('.task-status')).to_have_text('模拟成功',timeout=90000)
 if not row.locator('.process').evaluate('n=>n.open'):row.locator('.process > summary').click()
 expect(row).to_contain_text('程序判定结论类型')
 expect(row).to_contain_text('查询＋车控')
 expect(row).to_contain_text('程序校验回执并提交结论')
 assert '操作失败：提交任务结论' not in row.inner_text()
 row.get_by_role('button',name='查看结构 ↗').click()
 output=json.loads(page.locator('#jsonCode').inner_text())
 assert output['outcome']=='mixed' and len(output['results'])==2
 assert output['calls'][0]['arguments']=={'zone':'driver','temperature_c':23}
 page.get_by_role('button',name='关闭调用预览').click()
 step=row.locator('.process-step').filter(has_text='程序校验回执并提交结论').last
 step.scroll_into_view_if_needed();page.screenshot(path=str(ROOT/'test-results/programmatic-outcome.png'))
 assert not errors,errors
 print('Passed: query + control classified locally, no correction step, canonical mixed JSON and correct parameters.')
 browser.close()
