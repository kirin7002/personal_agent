"""Actual Qwen transport + shared conversation. Text in/audio out, not a microphone test."""
import asyncio,json,ssl,time,argparse,base64,wave
from pathlib import Path
import httpx,websockets

ROOT=Path(__file__).resolve().parent.parent
access=json.loads((ROOT/'.runtime/mobile-access.json').read_text())
BASE=access['mobile_url'].removesuffix('/mobile')
context=ssl.create_default_context(cafile=str(ROOT/'.runtime/mobile-tls/root-ca.crt'))

def cookies(client):return '; '.join(k+'='+v for k,v in client.cookies.items())

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--audio',action='store_true');args=parser.parse_args()
    target=23 if args.audio else 22
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False) as desktop, httpx.AsyncClient(base_url=BASE,verify=context,trust_env=False) as phone:
        (await desktop.post('/api/session',json={'name':'语音验收电脑','kind':'desktop'})).raise_for_status()
        (await phone.post('/api/session',json={'name':'语音验收手机','kind':'mobile'})).raise_for_status()
        code=(await desktop.post('/api/pairing/code')).json()['code']
        paired=await phone.post('/api/pairing/redeem',json={'code':code});paired.raise_for_status()
        device_id=paired.json()['device']['id']
        ready=asyncio.Event();audio_chunks=0
        async with websockets.connect(BASE.replace('https:','wss:')+'/ws/voice',ssl=context,origin=BASE,additional_headers={'Cookie':cookies(phone)},proxy=None,max_size=4*1024*1024) as ws:
            async def receiver():
                nonlocal audio_chunks
                async for raw in ws:
                    event=json.loads(raw)
                    if event['type']=='voice.ready':ready.set()
                    elif event['type']=='audio':audio_chunks+=1
                    elif event['type']=='response.done':await ws.send(json.dumps({'type':'playback','busy':False}))
                    elif event['type']=='error':raise RuntimeError('Voice error; no upstream details exposed')
            job=asyncio.create_task(receiver())
            try:
                await asyncio.wait_for(ready.wait(),25)
                if args.audio:
                    with wave.open(str(ROOT/'test-results/voice-command.wav'),'rb') as wav:
                        assert wav.getframerate()==16000 and wav.getnchannels()==1 and wav.getsampwidth()==2
                        pcm=b'\0'*6400+wav.readframes(wav.getnframes())+b'\0'*48000
                    for offset in range(0,len(pcm),640):
                        await ws.send(json.dumps({'type':'audio','audio':base64.b64encode(pcm[offset:offset+640]).decode()}))
                        await asyncio.sleep(.02)
                else:
                    await ws.send(json.dumps({'type':'text','text':'把所有分区空调设为22度'},ensure_ascii=False))
                deadline=time.monotonic()+100
                while time.monotonic()<deadline:
                    if job.done():job.result();raise RuntimeError('Voice closed early')
                    tasks=(await desktop.get('/api/tasks')).json()['tasks']
                    messages=(await desktop.get('/api/conversation/messages')).json()
                    events=(await desktop.get('/api/events')).json()['events']
                    if tasks and tasks[0]['status']=='generated' and any(e['kind']=='task.generated' and e['spoken'] for e in events):break
                    await asyncio.sleep(.2)
                else:raise RuntimeError('Shared voice completion timeout')
                users=[m for m in messages['messages'] if m['role']=='user']
                assert len(users)==1 and users[0]['device_id']==device_id
                assert ('23' in users[0]['text'] or '二十三' in users[0]['text']) if args.audio else users[0]['text']=='把所有分区空调设为22度'
                assert len(tasks)==1 and tasks[0]['calls'][0]['arguments']=={'zone':'all','temperature_c':target}
                assert sum(m['kind']=='task.generated' for m in messages['messages'])==1
                assert messages['process_events'] and audio_chunks
                owner=(await desktop.get('/api/voice-owner')).json()['owner'];assert owner['device_id']==device_id
                # The observer cannot silently create a second voice owner.
                async with websockets.connect('ws://127.0.0.1:8765/ws/voice',origin='http://127.0.0.1:8765',additional_headers={'Cookie':cookies(desktop)},proxy=None) as second:
                    assert json.loads(await second.recv())['type']=='voice.busy'
                result={'passed':True,'actual_qwen':True,'input_modality':'prerecorded_speech' if args.audio else 'text','physical_microphone_tested':False,
                        'tls_certificate_verified':True,'shared_user_messages':len(users),'task_count':len(tasks),
                        'desktop_observed_voice_process':True,'final_parameters':tasks[0]['calls'][0]['arguments'],
                        'audio_chunks':audio_chunks,'notification_acknowledged':True,'second_voice_owner_rejected':True}
                (ROOT/('test-results/shared-voice-asr-live.json' if args.audio else 'test-results/shared-voice-live.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(result,ensure_ascii=False))
            finally:
                job.cancel();await asyncio.gather(job,return_exceptions=True)

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
