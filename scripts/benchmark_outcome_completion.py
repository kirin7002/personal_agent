"""Compare receipt-based result classification with the preceding deployed version."""
import argparse,asyncio,json,time,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parent.parent
CASES=[('R1','空调温度增加一度','generated',23),('R2','主驾空调温度增加一度','generated',23),('R1-repeat','空调温度增加一度','generated',23),('Q','查询当前空调温度','completed',None)]
async def main():
 p=argparse.ArgumentParser();p.add_argument('--phase',choices=['before','after'],required=True);a=p.parse_args();rows=[]
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=15) as c:
  (await c.post('/api/session')).raise_for_status()
  for name,goal,status,temp in CASES:
   r=await c.post('/api/tasks',json={'goal':goal,'request_key':uuid.uuid4().hex});r.raise_for_status();tid=r.json()['id']
   for _ in range(650):
    t=(await c.get('/api/tasks/'+tid)).json()
    if t['status']!='running':break
    await asyncio.sleep(.2)
   if t['status']=='running':await c.post('/api/tasks/'+tid+'/cancel');raise RuntimeError('Timed out')
   await asyncio.sleep(.5);t=(await c.get('/api/tasks/'+tid)).json()
   expected_zone='driver' if name=='R2' else 'all'
   ok=t['status']==status and (not t['calls'] if temp is None else len(t['calls'])==1 and t['calls'][0]['arguments']=={'zone':expected_zone,'temperature_c':temp})
   steps=[s for s in t['process']['steps'] if s['duration_kind']=='measured_tool'];corrections=[s for s in steps if 'INVALID_OUTCOME' in str(s.get('observation',{}))]
   row={'case':name,'goal':goal,'passed':ok,'status':t['status'],'outcome':t['outcome'],'seconds':t['elapsed_seconds'],'provider_requests':t['process']['timing']['provider_requests'],'correction_rounds':len(corrections),'operations':[{'title':s['title'],'observation':{k:v for k,v in (s.get('observation') or {}).items() if k!='data'}} for s in steps]}
   rows.append(row);(ROOT/f'test-results/outcome-completion-{a.phase}.json').write_text(json.dumps({'phase':a.phase,'trials':rows},ensure_ascii=False,indent=2))
   print(json.dumps({k:v for k,v in row.items() if k!='operations'},ensure_ascii=False),flush=True)
 if not all(r['passed'] for r in rows):raise SystemExit(1)
 if a.phase=='after' and any(r['correction_rounds'] for r in rows):raise SystemExit(2)
if __name__=='__main__':asyncio.run(main())
