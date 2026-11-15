"""Real Qwen Audio handshake and audio generation check; never prints the key."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import wave

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import Settings


async def main():
    cfg=Settings()
    pcm=bytearray()
    text=[]
    events={}
    async with websockets.connect(cfg.audio_url(),additional_headers={'Authorization':f'Bearer {cfg.dashscope_key}'},open_timeout=20,max_size=4*1024*1024,proxy=cfg.audio_proxy) as ws:
        await ws.send(json.dumps({'type':'session.update','session':{'modalities':['text','audio'],'voice':cfg.audio_voice,'turn_detection':{'type':'server_vad'},'instructions':'请用中文简短回答。'}}))
        async with asyncio.timeout(45):
            async for raw in ws:
                event=json.loads(raw)
                kind=event.get('type','unknown')
                events[kind]=events.get(kind,0)+1
                if event.get('code') == 'Model.AccessDenied':
                    print(json.dumps({'ok':False,'error_type':'Model.AccessDenied','message':'Current credential cannot access this model in the configured region/workspace.'}))
                    return 1
                if kind=='session.updated':
                    await ws.send(json.dumps({'type':'conversation.item.create','item':{'type':'message','role':'user','content':[{'type':'input_text','text':'请说：语音连接测试成功。'}]}}))
                    await ws.send(json.dumps({'type':'response.create'}))
                elif kind=='response.audio.delta':
                    pcm.extend(base64.b64decode(event.get('delta','')))
                elif kind=='response.audio_transcript.delta':
                    text.append(event.get('delta',''))
                elif kind=='error':
                    err=event.get('error',{})
                    print(json.dumps({'ok':False,'error_type':err.get('type'),'message':str(err.get('message','')).replace(cfg.dashscope_key,'[redacted]')[:300]},ensure_ascii=False))
                    return 1
                elif kind=='response.done':
                    status=event.get('response',{}).get('status')
                    out=ROOT/'test-results/qwen-voice-reply.wav'
                    out.parent.mkdir(exist_ok=True)
                    with wave.open(str(out),'wb') as wav:
                        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(pcm)
                    ok=bool(pcm) and status=='completed'
                    print(json.dumps({'ok':ok,'model':cfg.audio_model,'status':status,'audio_seconds':round(len(pcm)/48000,2),'transcript':''.join(text),'events':events},ensure_ascii=False))
                    return 0 if ok else 1

if __name__=='__main__':
    try:
        sys.exit(asyncio.run(main()))
    except Exception as error:
        # Request exceptions can contain sensitive headers; expose only the type/status.
        response=getattr(error,'response',None)
        print(json.dumps({'ok':False,'error':type(error).__name__,'http_status':getattr(response,'status_code',None)}))
        sys.exit(2)
