"""Send a prerecorded PCM command through the real voice bridge and wait for spoken completion."""
import asyncio
import base64
import json
from pathlib import Path
import time
import wave

import httpx
import websockets

ROOT=Path(__file__).resolve().parent.parent


async def main():
    with wave.open(str(ROOT/'test-results/voice-command.wav'),'rb') as wav:
        assert wav.getframerate()==16000 and wav.getnchannels()==1 and wav.getsampwidth()==2
        pcm=wav.readframes(wav.getnframes())
    assert len(pcm)>32000, '测试语音为空或过短'
    transcripts=[]
    heard=[]
    audio=bytearray()
    event_types={}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',timeout=20,trust_env=False) as client:
        (await client.post('/api/session')).raise_for_status()
        cookie=client.cookies.get('preview_session')
        async with websockets.connect('ws://127.0.0.1:8765/ws/voice',origin='http://127.0.0.1:8765',additional_headers={'Cookie':f'preview_session={cookie}'},max_size=4*1024*1024,proxy=None) as ws:
            ready=asyncio.Event()
            async def receive():
                async for raw in ws:
                    event=json.loads(raw)
                    kind=event.get('type')
                    event_types[kind]=event_types.get(kind,0)+1
                    if kind=='voice.ready': ready.set()
                    elif kind=='audio': audio.extend(base64.b64decode(event['audio']))
                    elif kind=='assistant.delta': transcripts.append(event.get('text',''))
                    elif kind=='user.transcript': heard.append(event.get('text',''))
                    elif kind=='error': raise RuntimeError(event.get('message','Voice error'))
            async def send_audio():
                await asyncio.wait_for(ready.wait(),25)
                data=b'\0'*9600+pcm+b'\0'*48000
                for offset in range(0,len(data),640):
                    await ws.send(json.dumps({'type':'audio','audio':base64.b64encode(data[offset:offset+640]).decode()}))
                    await asyncio.sleep(.02)
            receiver=asyncio.create_task(receive())
            sender=asyncio.create_task(send_audio())
            try:
                deadline=time.monotonic()+130
                result=None
                while time.monotonic()<deadline:
                    if receiver.done(): receiver.result(); raise RuntimeError('Voice connection ended early')
                    if sender.done(): sender.result()
                    tasks=(await client.get('/api/tasks')).json()['tasks']
                    if tasks:
                        task=tasks[0]
                        if task['status'] in {'failed','unsupported','interrupted'}: raise RuntimeError(task.get('summary'))
                        events=(await client.get('/api/events')).json()['events']
                        announced=any(e['kind']=='task.generated' and e['spoken'] for e in events)
                        if task['status']=='generated' and announced and audio:
                            assert [{'tool':c['tool'],'arguments':c['arguments']} for c in task['calls']]==[{'tool':'control.climate.set_temperature','arguments':{'zone':'all','temperature_c':23}}]
                            result={'ok':True,'source':'prerecorded_speech','heard':heard,'reply':''.join(transcripts),'calls':task['calls'],'audio_seconds':round(len(audio)/48000,2),'notification_acknowledged':True,'events':event_types}
                            break
                    await asyncio.sleep(.25)
                if result is None:
                    result={'ok':False,'error':'timeout','heard':heard,'reply':''.join(transcripts),'audio_seconds':round(len(audio)/48000,2),'events':event_types}
                path=ROOT/'test-results/live-voice.json'
                path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                with wave.open(str(ROOT/'test-results/live-voice-reply.wav'),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(audio)
                print(json.dumps(result,ensure_ascii=False),flush=True)
                if not result['ok']: raise SystemExit(1)
            finally:
                receiver.cancel();sender.cancel()
                await asyncio.gather(receiver,sender,return_exceptions=True)


if __name__=='__main__':
    asyncio.run(main())
