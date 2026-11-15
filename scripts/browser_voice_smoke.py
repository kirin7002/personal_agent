"""Real browser audio-capture pipeline with synthetic microphone input; no physical mic recording."""
from pathlib import Path
import wave
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent.parent
input_path=ROOT/'test-results/browser-microphone.wav'
with wave.open(str(ROOT/'test-results/voice-command.wav'),'rb') as wav:
    pcm=wav.readframes(wav.getnframes())
    assert wav.getframerate()==16000 and len(pcm)>32000
with wave.open(str(input_path),'wb') as wav:
    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
    wav.writeframes(b'\0'*(32000*5)+pcm+b'\0'*(32000*90))

with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    browser=p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None,headless=True,args=[
        '--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',
        f'--use-file-for-fake-audio-capture={input_path}',
    ])
    context=browser.new_context(permissions=['microphone'],viewport={'width':1440,'height':1050})
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8765',wait_until='domcontentloaded')
    expect(page.locator('#catalogCount')).to_have_text('68 项')
    page.locator('#voiceButton').click()
    expect(page.locator('#voiceStatus')).to_have_text('已连接',timeout=25000)
    expect(page.locator('.task').first.locator('.task-status')).to_have_text('模拟成功',timeout=130000)
    page.wait_for_function("document.querySelector('#chat').innerText.includes('模拟下发成功')",timeout=45000)
    expect(page.locator(".voice-process")).to_contain_text("任务结果已回传并播报",timeout=45000)
    assert 'control.climate.set_temperature' in page.locator('#jsonCode').inner_text()
    task=context.request.get('http://127.0.0.1:8765/api/tasks/'+page.locator('.task').first.get_attribute('data-task-id')).json()
    assert task['process']['timing']['direct_completion']
    assert task['process']['timing']['provider_requests']==1
    page.screenshot(path=str(ROOT/'test-results/voice-desktop.png'),full_page=True)
    page.locator('#voiceButton').click()
    expect(page.locator('#voiceButton')).to_have_attribute('aria-pressed','false')
    assert not errors,errors
    print('Browser voice passed: synthetic microphone → Qwen Audio → one-request Hermes preview → direct completion → spoken completion → clean disconnect.')
    browser.close()
