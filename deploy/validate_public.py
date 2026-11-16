"""Validate the deployed HTTPS app with a private, one-use test invitation.

No credentials/cookies are logged. Text requests and S2S produce mock controls.
"""
import argparse,asyncio,json,re,time
from pathlib import Path
import httpx,websockets

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--origin',default='https://agent.kirinxu.cn')
    parser.add_argument('--pairing-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('test-results/server-public-validation.json'))
    args=parser.parse_args();origin=args.origin.rstrip('/')
    invite=re.search(r'[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}',args.pairing_file.read_text()).group(0)
    report={'origin':origin,'real_hermes':False,'real_qwen':False,'physical_vehicle_execution':False}
    async with httpx.AsyncClient(base_url=origin,trust_env=False,timeout=20) as client:
        r=await client.post('/api/session',json={'name':'部署独立验收','kind':'desktop'});r.raise_for_status()
        assert r.json()['pairing_required']
        r=await client.post('/api/pairing/redeem',json={'code':invite});r.raise_for_status()
        device_id=r.json()['device']['id']
        try:
            config=(await client.get('/api/config')).json();health=(await client.get('/api/hermes-health')).json()
            assert config['hermes_configured'] and config['voice_configured'] and health['ready']
            report['backend_model']=config['backend_model'];report['voice_model']=config['audio_model']
            submitted=await client.post('/api/conversation/input',json={'text':'把所有分区空调设为23度','event_id':'server-text-validation'})
            submitted.raise_for_status();tid=submitted.json()['task']['id']
            end=time.monotonic()+125
            while time.monotonic()<end:
                task=(await client.get('/api/tasks/'+tid)).json()
                if task['status'] not in {'running','waiting'}:break
                await asyncio.sleep(.3)
            assert task['status']=='generated', task['status']
            assert task['source']=='hermes' and task['output']['executed'] is False
            assert [c['arguments'] for c in task['calls']]==[{'zone':'all','temperature_c':23}]
            report.update(real_hermes=True,text_final_parameters=task['calls'][0]['arguments'],provider_requests=task['process']['timing']['provider_requests'])
            audio=0;ready=asyncio.Event()
            cookie='; '.join(k+'='+v for k,v in client.cookies.items())
            async with websockets.connect(origin.replace('https:','wss:')+'/ws/voice',origin=origin,additional_headers={'Cookie':cookie},proxy=None,max_size=4*1024*1024) as ws:
                async def receive():
                    nonlocal audio
                    async for raw in ws:
                        event=json.loads(raw)
                        if event['type']=='voice.ready':ready.set()
                        elif event['type']=='audio':audio+=1
                        elif event['type']=='response.done':await ws.send(json.dumps({'type':'playback','busy':False}))
                        elif event['type']=='error':raise RuntimeError('S2S transport error')
                receiver=asyncio.create_task(receive())
                try:
                    await asyncio.wait_for(ready.wait(),30)
                    await ws.send(json.dumps({'type':'text','text':'把所有分区空调设为21度'},ensure_ascii=False))
                    end=time.monotonic()+130;voice_task=None
                    while time.monotonic()<end:
                        if receiver.done():receiver.result();raise RuntimeError('Voice ended early')
                        tasks=(await client.get('/api/tasks')).json()['tasks']
                        voice_task=next((t for t in tasks if t['id']!=tid),None)
                        events=(await client.get('/api/events')).json()['events']
                        if voice_task and voice_task['status']=='generated' and audio and any(e['task_id']==voice_task['id'] and e['kind']=='task.generated' and e['spoken'] for e in events):break
                        if voice_task and voice_task['status'] in {'failed','interrupted','unsupported'}:raise RuntimeError('Voice business task failed')
                        await asyncio.sleep(.25)
                    else:raise TimeoutError('Voice result timeout')
                    assert [c['arguments'] for c in voice_task['calls']]==[{'zone':'all','temperature_c':21}]
                    messages=(await client.get('/api/conversation/messages')).json()
                    assert messages['process_events']
                    assert sum(m['role']=='user' for m in messages['messages'])==2
                    report.update(real_qwen=True,voice_input='text',voice_audio_chunks=audio,voice_final_parameters=voice_task['calls'][0]['arguments'],shared_process_visible=True,notification_acknowledged=True)
                finally:
                    receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
            report['passed']=True
        finally:
            tasks=(await client.get('/api/tasks')).json().get('tasks',[])
            for task in tasks:
                if task['status'] in {'running','waiting','awaiting_input'}:
                    await client.post('/api/tasks/'+task['id']+'/cancel')
            await client.delete('/api/devices/'+device_id)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    try:asyncio.run(main())
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
