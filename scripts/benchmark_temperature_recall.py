"""Small live before/after sample for a temperature paraphrase; no hardware calls.

Run while the previous catalog is still active; reload applies the versioned
on-disk metadata between phases. Model latency is not controlled by this script.
"""
import asyncio,json,time,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parent.parent

async def main():
    rows=[]
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,timeout=20) as client:
        (await client.post('/api/session')).raise_for_status()
        for phase in ('before','after'):
            if phase=='after':
                result=await client.post('/api/catalog/reload');result.raise_for_status()
            for repetition in range(3):
                start=time.monotonic()
                response=await client.post('/api/tasks',json={'goal':'把主驾空调调到21度','request_key':uuid.uuid4().hex,'profile_id':'demo-no-suspension'})
                response.raise_for_status();tid=response.json()['id']
                try:
                    async with asyncio.timeout(125):
                        while True:
                            task=(await client.get('/api/tasks/'+tid)).json()
                            if task['status']!='running':break
                            await asyncio.sleep(.15)
                    passed=task['status']=='generated' and [c['arguments'] for c in task['calls']]==[{'zone':'driver','temperature_c':21}]
                    preload=next((x for x in task['navigation'] if x['kind']=='catalog.preloaded'),{})
                    row={'phase':phase,'repeat':repetition+1,'correct':passed,'backend_seconds':task['elapsed_seconds'],
                         'observed_seconds':round(time.monotonic()-start,3),'model_requests':task['process']['timing']['provider_requests'],
                         'direct_completion':task['process']['timing']['direct_completion'],'preload_reason':preload.get('reason'),
                         'preloaded_tools':preload.get('tool_ids'),'catalog_revision':task['catalog_revision']}
                    rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
                    (ROOT/'test-results/temperature-recall-ab.json').write_text(json.dumps({'goal':'把主驾空调调到21度','scope':'same local service and mock profile; small sequential sample, not a latency guarantee','cases':rows},ensure_ascii=False,indent=2)+'\n')
                    if not passed:raise RuntimeError('Incorrect task outcome')
                finally:
                    if task['status']=='running':await client.post('/api/tasks/'+tid+'/cancel')
                await asyncio.sleep(.4)
if __name__=='__main__':asyncio.run(main())
