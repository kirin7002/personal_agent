"""Real Qwen silence regression plus visible voice and Hermes execution facts."""
import json
from pathlib import Path
import sqlite3
import wave

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'test-results'
with wave.open(str(OUT/'voice-command.wav'),'rb') as wav:
    pcm=wav.readframes(wav.getnframes())
    assert wav.getframerate()==16000
mic=OUT/'voice-idle-then-command.wav'
with wave.open(str(mic),'wb') as wav:
    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
    wav.writeframes(b'\0'*(32000*12)+pcm+b'\0'*(32000*90))

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',f'--use-file-for-fake-audio-capture={mic}'])
    context=browser.new_context(permissions=['microphone'],viewport={'width':1440,'height':1050})
    page=context.new_page();errors=[];voice_events=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def received(frame):
        e=json.loads(frame)
        voice_events.append({k:v for k,v in e.items() if k!='audio'})
    page.on('websocket',lambda ws:ws.on('framereceived',received))
    page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
    expect(page.locator('#catalogCount')).to_have_text('68 项')
    old=context.request.post('http://127.0.0.1:8765/api/examples/control.climate.set_temperature').json()
    assert old['status']=='generated'
    page.locator('#voiceButton').click()
    expect(page.locator('#voiceStatus')).to_have_text('已连接',timeout=25000)
    expect(page.locator('.voice-process')).to_contain_text('语音连接就绪')
    # Old success is pending, but no instruction has arrived on this voice connection.
    page.wait_for_timeout(5000)
    assert not any(e['type'] in {'assistant.delta','audio','user.transcript'} for e in voice_events), [e['type'] for e in voice_events]
    old_events=context.request.get('http://127.0.0.1:8765/api/events').json()['events']
    assert any(e['task_id']==old['id'] and e['kind']=='task.generated' and not e['spoken'] for e in old_events)
    print('Idle regression passed: old success remains unspoken; no audio or assistant reply on connection.',flush=True)
    expect(page.locator('.task')).to_have_count(2,timeout=55000)
    new=page.locator('.task').last
    expect(new.locator('.task-status')).to_have_text('模拟成功',timeout=90000)
    expect(new).to_contain_text('目标温度（℃）：23')
    expect(new).to_contain_text('分区：全部')
    expect(new).to_contain_text('未执行真实车控')
    tid=new.get_attribute('data-task-id')
    expect(page.locator('.voice-process')).to_contain_text('取得用户语音转写')
    expect(page.locator('.voice-process')).to_contain_text('派发车辆任务给 Hermes')
    expect(page.locator('.voice-process')).to_contain_text('任务结果已回传并播报',timeout=45000)
    delivered=context.request.get('http://127.0.0.1:8765/api/events').json()['events']
    assert any(e['task_id']==tid and e['kind']=='task.generated' and e['spoken'] for e in delivered)
    assert not any(e['task_id']==old['id'] and e['kind']=='task.generated' and e['spoken'] for e in delivered)
    assert any(e['type']=='assistant.delta' and '成功' in e.get('text','') for e in voice_events) or '已模拟下发成功' in ''.join(e.get('text','') for e in voice_events if e['type']=='assistant.delta')
    page.locator('.voice-process').scroll_into_view_if_needed()
    page.screenshot(path=str(OUT/'s2s-process.png'))
    page.locator('#voiceButton').click()
    expect(page.locator('.voice-process > summary')).to_contain_text('连接已结束')
    assert page.locator('.voice-process [data-timer-start]').count()==0
    control=new.locator('.process-step').filter(has_text='生成控制参数：设置空调温度').last
    control.locator('.step-data > summary').click()
    expect(control.locator('.step-data pre')).to_contain_text('temperature_c')
    page.wait_for_timeout(5500)
    expect(control.locator('.step-data')).to_have_attribute('open','')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(OUT/'process-mobile.png'))
    # Read the user's existing relative-temperature trace; never rerun it.
    with sqlite3.connect(ROOT/'.data/preview.sqlite3') as db:
        hist=db.execute("SELECT id,session_id FROM tasks WHERE goal='空调温度增加一度' AND status='generated' ORDER BY created DESC LIMIT 1").fetchone()
    if hist:
        history=browser.new_context(viewport={'width':1440,'height':1050})
        history.add_cookies([{'name':'preview_session','value':hist[1],'url':'http://127.0.0.1:8765','httpOnly':True,'sameSite':'Strict'}])
        view=history.new_page();view.on('pageerror',lambda e:errors.append(str(e)))
        view.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
        row=view.locator(f'.task[data-task-id="{hist[0]}"]')
        row.locator('.process > summary').click()
        expect(row).to_contain_text('主驾温度（℃）：22')
        expect(row).to_contain_text('副驾温度（℃）：22')
        expect(row).to_contain_text('目标温度（℃）：23')
        row.screenshot(path=str(OUT/'relative-temperature-steps.png'))
    assert not errors,errors
    (OUT/'voice-process-check.json').write_text(json.dumps({'idle_without_reply':True,'new_task_spoken':True,'history_facts_visible':bool(hist),'javascript_errors':errors},ensure_ascii=False,indent=2))
    print('Passed: current voice transcription, task dispatch, per-step values, result notification, playback completion, JSON disclosure, mobile and historical receipts.',flush=True)
    browser.close()
