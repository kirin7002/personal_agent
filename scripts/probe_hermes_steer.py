"""Opt-in isolated real-model steer verification. Never edits the running app configuration."""
import argparse,asyncio,json,os,secrets,shutil,signal,socket,subprocess,sys,time,uuid
from pathlib import Path
import httpx
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.store import Store
from app.capability_catalog import CapabilityCatalog
from app.hermes import TaskManager
from experiments.steer_probe.adapter import setup,register_case,case,revise,release
from experiments.steer_probe.manager import ProbeManager, ObservedStockManager

REPO=ROOT/'.runtime/hermes-agent'

def write_private(path,text):
 path.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,'w') as f:f.write(text)
 os.chmod(path,0o600)

async def run_case(store,cat,settings,name,barrier,protect,stock=False,completion='agent'):
 manager=(ObservedStockManager if stock else ProbeManager)(store,cat,settings)
 sid=store.create_session();task=manager.submit(sid,'把所有分区空调设为23度',uuid.uuid4().hex,completion_mode=completion)
 tid=task['id'];register_case(store,task,name,protect,barrier)
 run_id=None;steer_result=None
 try:
  deadline=time.monotonic()+95
  while time.monotonic()<deadline:
   current=store.task(tid);run_id=current['run_id'];events=store.task_events(tid)
   at_barrier=case(store,tid)['reached'] if barrier!='during_model' else any(e['kind']=='probe.provider_request' for e in events)
   if run_id and at_barrier:break
   if current['status']!='running':raise RuntimeError('Task ended before probe barrier: '+current['status'])
   await asyncio.sleep(.025)
  else:raise RuntimeError('Probe barrier not reached')
  revision=revise(store,tid,sid,'把所有分区空调设为21度')
  edit={'task_id':tid,'task_revision':revision,'catalog_revision':task['catalog_revision'],
        'user_request':'把所有分区空调设为21度','change':'用户在当前任务执行中将23度改为21度；分区仍为全部。以最新目标为准，旧23度控制回执不能完成新目标。',
        'completion_policy':{'mode':'agent','instruction':'修改后的任务以新参数调用，并引用新回执提交 finish_task。'}}
  r=await manager.hermes.client.post(f'/v1/runs/{run_id}/steer',json={'input':json.dumps(edit,ensure_ascii=False)})
  steer_result={'http_status':r.status_code,'accepted':r.json().get('accepted',False)}
  release(store,tid)
  job=manager.jobs.get(tid)
  if job:await asyncio.wait_for(asyncio.shield(job),125)
  if stock:
   for _ in range(40):
    state=await manager.hermes.status(run_id)
    if state.get('status') not in {'running','stopping','queued','started'}:break
    await asyncio.sleep(.1)
  current=store.task(tid);events=store.task_events(tid);c=case(store,tid)
  with store.connect() as db:
   records=[{'revision':x['task_revision'],'created':x['created'],'response':json.loads(x['response'])} for x in db.execute('SELECT * FROM capability_calls WHERE task_id=? ORDER BY created',(tid,))]
  prepared=[x for x in records if x['response']['status']=='prepared']
  stale_after=[x for x in prepared if x['created']>=c['changed_at'] and x['response']['arguments'].get('temperature_c')==23]
  final=json.loads(current['final_result']) if current['final_result'] else None
  final_temps=[x['arguments'].get('temperature_c') for x in (final or {}).get('calls',[])]
  notifications=[e for e in events if e['kind']=='task.generated']
  errors=[{'name':e['data']['name'],'error':e['data']['error']} for e in events if e['kind']=='probe.tool_response' and e['data'].get('error')]
  request_events=[e for e in events if e['kind']=='probe.provider_request']
  response_events=[e for e in events if e['kind']=='probe.provider_response']
  in_flight=bool(request_events and response_events and request_events[0]['created']<=c['changed_at']<=response_events[0]['created'])
  remote=await manager.hermes.status(run_id)
  same_run=current['run_id']==run_id and sum(e['kind']=='task.dispatched' for e in events)==1
  protected_ok=(steer_result['accepted'] and same_run and current['status']=='generated' and final_temps==[21] and not stale_after and len(notifications)==1)
  row={'case':name,'barrier':barrier,'protection':protect,'stock_manager':stock,'completion_mode':completion,
       'steer':steer_result,'same_task_and_run':same_run,'business_status':current['status'],'remote_status':remote.get('status'),
       'goal_after':current['goal'],'revision_after':current['revision'],'final_temperatures':final_temps,
       'old_23_accepted_before_change':sum(x['created']<c['changed_at'] and x['response']['arguments'].get('temperature_c')==23 for x in prepared),
       'old_23_accepted_after_change':len(stale_after),'completed_notifications':len(notifications),
       'old_receipts_in_final':bool(final and any(x['arguments'].get('temperature_c')==23 for x in final['calls'])),
       'change_during_first_provider_request':in_flight,'provider_requests':len(request_events),'tool_errors':errors,
       'stock_stop_requests':sum(e['kind']=='probe.stock_stop_requested' for e in events),
       'stock_stop_accepted':any(e['kind']=='probe.stock_stop_returned' and e['data'].get('accepted') for e in events),
       'protected_result_passed':protected_ok if protect and not stock else None,
       'trace':[{'kind':e['kind'],'at_ms':round((e['created']-task['created'])*1000,1),'data':e['data']} for e in events if e['kind'].startswith('probe.') or e['kind']=='agent.step']}
  if protect and not stock:
   if barrier=='during_model':row['protected_result_passed'] &= in_flight
   if barrier=='before_finish':row['protected_result_passed'] &= row['old_23_accepted_before_change']==1
   # Completed tasks must not be silently reopened by another edit.
   try:revise(store,tid,sid,'把所有分区空调设为20度',20)
   except ValueError:row['terminal_edit_rejected']=True
   else:row['terminal_edit_rejected']=False;row['protected_result_passed']=False
  return row
 finally:
  release(store,tid)
  if run_id:await manager.stop_remote(run_id)
  await manager.close()

async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--cases',default='');parser.add_argument('--repeats',type=int,default=1,choices=[1,2]);parser.add_argument('--output',type=Path,default=ROOT/'test-results/steer-probe.json');args=parser.parse_args()
 folder=ROOT/'.runtime'/('steer-probe-'+uuid.uuid4().hex[:8]);folder.mkdir(mode=0o700)
 home=folder/'home';home.mkdir(mode=0o700)
 db_path=folder/'tasks.sqlite3';store=Store(db_path);setup(store);cat=CapabilityCatalog(store)
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 key=secrets.token_urlsafe(32)
 config=json.loads((ROOT/'.runtime/hermes-home/config.yaml').read_text())
 config['gateway']['api_server'].update(port=port,max_concurrent_runs=1)
 config['plugins']['enabled']=['bailian-limits','steer-probe']
 config['mcp_servers']['vehicle_preview'].update(args=['-m','experiments.steer_probe.mcp_server'],env={'PREVIEW_DB':str(db_path)},timeout=30)
 write_private(home/'config.yaml',json.dumps(config,ensure_ascii=False,indent=2))
 original=dotenv_values(ROOT/'.runtime/hermes-home/.env')
 write_private(home/'.env',f"DASHSCOPE_API_KEY={original['DASHSCOPE_API_KEY']}\nAPI_SERVER_KEY={key}\nAPI_SERVER_ENABLED=true\n")
 shutil.copytree(ROOT/'hermes_plugins/bailian_limits',home/'plugins/bailian-limits',ignore=shutil.ignore_patterns('__pycache__'))
 write_private(home/'plugins/steer-probe/__init__.py','from experiments.steer_probe.middleware import register\n')
 write_private(home/'plugins/steer-probe/plugin.yaml','name: steer-probe\nversion: 0.1.0\ndescription: Isolated steer validation telemetry\n')
 env={**os.environ,'HERMES_HOME':str(home),'HERMES_RUNTIME_DIR':str(ROOT/'.runtime/hermes-tools'),'PREVIEW_DB':str(db_path),'PROBE_AUDIT_PATH':str(folder/'middleware-debug.jsonl'),'PYTHONPATH':os.pathsep.join([str(ROOT),os.environ.get('PYTHONPATH','')]),'API_SERVER_KEY':key,'API_SERVER_ENABLED':'true','API_SERVER_PORT':str(port),'API_SERVER_HOST':'127.0.0.1','DASHSCOPE_API_KEY':original['DASHSCOPE_API_KEY']}
 env['NO_PROXY']=env.get('NO_PROXY','')+',localhost,127.0.0.1,::1,dashscope.aliyuncs.com';env['no_proxy']=env['NO_PROXY']
 os.environ['HERMES_HOME']=str(ROOT/'.runtime/hermes-home')
 os.environ['HERMES_RUNTIME_DIR']=str(ROOT/'.runtime/hermes-tools')
 sys.path.insert(0,str(REPO));from pm.environments import project_python, runtime_facts_path
 facts=runtime_facts_path(REPO);local_facts=home/facts.relative_to(ROOT/'.runtime/hermes-home')
 write_private(local_facts,facts.read_text())
 (local_facts.parent/'environments').symlink_to(facts.parent/'environments',target_is_directory=True)
 log_path=folder/'gateway.log';fd=os.open(log_path,os.O_WRONLY|os.O_CREAT,0o600)
 with os.fdopen(fd,'wb',buffering=0) as log:proc=subprocess.Popen([str(project_python(REPO)),'-m','gateway.run'],cwd=REPO,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 settings=Settings(db_path=db_path,hermes_url=f'http://127.0.0.1:{port}',hermes_key=key)
 rows=[];report_path=args.output
 try:
  async with httpx.AsyncClient(base_url=settings.hermes_url,headers={'Authorization':f'Bearer {key}'},trust_env=False,timeout=5) as client:
   for _ in range(70):
    try:
     r=await client.get('/v1/capabilities');r.raise_for_status()
     if r.json().get('features',{}).get('run_steer'):break
    except httpx.HTTPError:pass
    if proc.poll() is not None:raise RuntimeError('Isolated gateway exited; private log retained')
    await asyncio.sleep(.3)
   else:raise RuntimeError('Isolated gateway unavailable')
  plan=[('native_only','before_control',False,False,'agent'),('stock_monitor','before_control',True,True,'agent'),('during_model','during_model',True,False,'auto'),('before_control','before_control',True,False,'auto'),('before_finish','before_finish',True,False,'agent')]
  if args.cases:plan=[p for p in plan if p[0] in args.cases.split(',')]
  if not plan:raise ValueError('No selected cases')
  for repeat in range(args.repeats):
   for name,barrier,protect,stock,mode in plan:
    row=await run_case(store,cat,settings,name,barrier,protect,stock,mode);row['repeat']=repeat+1;rows.append(row)
    report_path.write_text(json.dumps({'isolated':True,'model':settings.backend_model,'catalog_revision':cat.snapshot().revision,'tests':rows},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in row.items() if k!='trace'},ensure_ascii=False),flush=True)
    await asyncio.sleep(.5)
  if any(r['protected_result_passed'] is False for r in rows):raise SystemExit(1)
 finally:
  if proc.poll() is None:
   os.killpg(proc.pid,signal.SIGTERM)
   try:await asyncio.to_thread(proc.wait,8)
   except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);await asyncio.to_thread(proc.wait)
  print('Isolated probe gateway stopped; application services untouched.',flush=True)

if __name__=='__main__':asyncio.run(main())
