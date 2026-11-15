"""Small same-instruction sample for simple controls; remote latency is not isolated."""
import argparse,asyncio,json,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parent.parent
async def main():
 p=argparse.ArgumentParser();p.add_argument('--phase',required=True,choices=['before','after']);a=p.parse_args();rows=[]
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=15) as c:
  (await c.post('/api/session')).raise_for_status()
  for i in range(3):
   r=await c.post('/api/tasks',json={'goal':'全部空调温度设置为21度','request_key':uuid.uuid4().hex});r.raise_for_status();tid=r.json()['id']
   for _ in range(300):
    t=(await c.get('/api/tasks/'+tid)).json()
    if t['status']!='running':break
    await asyncio.sleep(.2)
   if t['status']=='running':await c.post('/api/tasks/'+tid+'/cancel');raise RuntimeError('Timed out')
   pre=next(n for n in t['navigation'] if n['kind']=='catalog.preloaded')
   ok=t['status']=='generated' and len(t['calls'])==1 and t['calls'][0]['arguments']=={'zone':'all','temperature_c':21}
   row={'repeat':i+1,'passed':ok,'seconds':t['elapsed_seconds'],'provider_requests':t['process']['timing']['provider_requests'],'payload_chars':pre['payload_chars'],'related_tools':pre.get('related_tool_ids',[])}
   rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
   (ROOT/f'test-results/simple-preload-{a.phase}.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
   await asyncio.sleep(.5)
 if not all(r['passed'] for r in rows):raise SystemExit(1)
if __name__=='__main__':asyncio.run(main())
