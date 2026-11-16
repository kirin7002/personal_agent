"""Live same-instruction evaluation across three control families. No hardware I/O."""
import argparse,asyncio,json,time,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parent.parent

def normalize(calls):
 return sorted([{'tool':c.get('tool') or c.get('tool_id'),'arguments':c['arguments']} for c in calls],key=lambda c:json.dumps(c,sort_keys=True))

async def main():
 p=argparse.ArgumentParser();p.add_argument('--phase',required=True,choices=['before','after']);p.add_argument('--repeats',type=int,default=2);p.add_argument('--kinds',default='explicit,clarify,neighbor,relative,complex,reject');p.add_argument('--cases',default='');p.add_argument('--dataset',default='benchmarks/simple-controls-cases.json');p.add_argument('--output');args=p.parse_args()
 data=json.loads((ROOT/args.dataset).read_text());rows=[]
 cases=[c for c in data['cases'] if c['kind'] in args.kinds.split(',') and (not args.cases or c['id'] in args.cases.split(','))]
 output=ROOT/(args.output or f'test-results/simple-controls-{args.phase}.json');output.parent.mkdir(parents=True,exist_ok=True)
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=20) as client:
  (await client.post('/api/session')).raise_for_status()
  for repeat in range(args.repeats):
   # Alternate domain order on the second pass; retain identical prompts.
   order=cases if repeat%2==0 else list(reversed(cases))
   for case in order:
    t=None;tid=None;started=time.monotonic()
    try:
     response=await client.post('/api/tasks',json={'goal':case['goal'],'request_key':uuid.uuid4().hex,'profile_id':data['profile']});response.raise_for_status();tid=response.json()['id']
     async with asyncio.timeout(110):
      while True:
       t=(await client.get('/api/tasks/'+tid)).json()
       if t['status']!='running':break
       await asyncio.sleep(.15)
     actual=normalize(t['calls']);valid=[normalize(case['expected']),*[normalize(a) for a in case.get('alternatives',[])]]
     prepared=[r for r in t.get('results',[]) if r.get('status')=='prepared']
     correct=(t['status']=='awaiting_input' and not prepared) if case['kind']=='clarify' else (t['status']=='generated' and actual in valid)
     if case['kind']=='reject':correct=t['status'] in {'awaiting_input','failed','unsupported','cancelled'} and not prepared
     observed_queries=[r['tool_id'] for r in t.get('results',[]) if r.get('kind')=='status' and r.get('status')=='returned_mock']
     if case.get('requires_query'):correct=correct and case['requires_query'] in observed_queries and not t['process']['timing']['direct_completion']
     pre=next((n for n in t['navigation'] if n['kind']=='catalog.preloaded'),{})
     row={'case':case['id'],'goal':case['goal'],'group':case['group'],'kind':case['kind'],'repeat':repeat+1,'phase':args.phase,'correct':correct,'status':t['status'],'arguments':actual,'question':t.get('question'),
          'backend_seconds':round(t['updated']-t['created'],3),'observed_seconds':round(time.monotonic()-started,3),'model_requests':t['process']['timing']['provider_requests'],'fast':t['process']['timing']['direct_completion'],
          'preloaded_tools':pre.get('tool_ids',[]),'preload_reason':pre.get('reason'),'payload_chars':pre.get('payload_chars',0),'navigation_steps':len([n for n in t['navigation'] if n['kind'] in {'catalog.browsed','catalog.described','catalog.searched'}]),'catalog_revision':t['catalog_revision'],'observed_queries':observed_queries}
    except Exception as error:
     row={'case':case['id'],'goal':case['goal'],'group':case['group'],'kind':case['kind'],'repeat':repeat+1,'phase':args.phase,'correct':False,'error_type':type(error).__name__}
    finally:
     if tid and (not t or t['status'] in {'running','awaiting_input','waiting'}):await client.post('/api/tasks/'+tid+'/cancel')
    rows.append(row);output.write_text(json.dumps({'profile':data['profile'],'note':'same prompts and service; small sample, model/network timing is uncontrolled','cases':rows},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:row.get(k) for k in ['phase','case','repeat','correct','status','model_requests','backend_seconds']},ensure_ascii=False),flush=True)
    await asyncio.sleep(.3)
if __name__=='__main__':asyncio.run(main())
