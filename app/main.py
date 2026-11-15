from contextlib import asynccontextmanager
import asyncio
import json
import time
import fcntl
import uuid
import httpx
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import Catalog
from .capability_catalog import CapabilityCatalog
from .capability_tools import CapabilityTools
from .config import ROOT, Settings
from .hermes import TaskManager
from .realtime import VoiceBridge
from .store import Store
from .task_presentation import build_process
from .access import local_client, allowed_host
from .device_sharing import DeviceSharing, PairingRateLimited
from .shared_messages import MessageLog
from .conversation_recording import record_input, record_result, input_key


class DeviceInit(BaseModel):
    name: str = Field(default='浏览器',min_length=1,max_length=80)
    kind: Literal['desktop','mobile'] = 'desktop'


class PairingInput(BaseModel):
    code: str = Field(min_length=1,max_length=128)


class TaskInput(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    request_key: str = Field(min_length=1, max_length=100)
    profile_id: str | None = None
    parent_task_id: str | None = None
    discovery_mode: Literal["auto", "off"] | None = None
    completion_mode: Literal["auto", "agent"] | None = None


class AnswerInput(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)
    task_revision: int | None = Field(default=None, ge=0)


class ReviseInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    task_revision: int = Field(ge=0)
    change_id: str = Field(min_length=1, max_length=100)


class FollowupInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    task_revision: int = Field(ge=0)
    event_id: str = Field(min_length=1, max_length=100)


class ConversationInput(BaseModel):
    text: str = Field(min_length=1,max_length=2000)
    event_id: str = Field(min_length=1,max_length=100)
    task_id: str | None = None
    task_revision: int | None = Field(default=None,ge=0)


class PlanInput(BaseModel):
    goal: str = Field(min_length=1,max_length=2000)
    schedule: dict
    request_key: str = Field(min_length=1,max_length=100)
    profile_id: str | None = None


class PlanActionInput(BaseModel):
    action: Literal['pause','resume','cancel','cancel_once','cancel_plan','update','edit']
    revision: int = Field(ge=0)
    goal: str | None = Field(default=None,min_length=1,max_length=2000)
    schedule: dict | None = None
    profile_id: str | None = None


class ProfileInput(BaseModel):
    profile_id: str


class ImportInput(BaseModel):
    payload: dict
    mode: str = "merge"


class ApplyImportInput(BaseModel):
    preview_id: str
    base_revision: str


def same_origin(origin, host):
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


def create_app(settings: Settings | None = None, manager_factory=TaskManager):
    settings = settings or Settings()
    legacy_catalog = Catalog()
    store = Store(settings.db_path)
    catalog = CapabilityCatalog(store, settings.catalog_dir)
    manager = manager_factory(store, catalog, settings)
    voices = {}
    devices = DeviceSharing(store)
    shared = MessageLog(store)
    manager.shared = shared
    manager.devices = devices

    @asynccontextmanager
    async def lifespan(app):
        lock_path = store.path.with_suffix(".web.lock")
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("同一个数据库只允许启动一个预览服务进程")
            store.interrupt_running()
            await manager.expire_questions()
            manager.ensure_scheduler()
            yield
            await manager.close()

    app = FastAPI(title="Personal Agent · 调用预览", lifespan=lifespan)
    app.state.store, app.state.manager = store, manager
    app.state.devices, app.state.shared, app.state.voices = devices, shared, voices

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not allowed_host(request,settings):
            return JSONResponse({"detail": "当前 Demo 仅允许本机访问"}, status_code=403)
        if not same_origin(request.headers.get("origin"), request.headers.get("host")):
            return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        if not local_client(request) and request.url.scheme!='https' and request.url.path not in {'/api/health','/mobile/trust.crt','/mobile/connect','/mobile/access'}:
            return JSONResponse({'detail':'手机请使用受信任的 HTTPS 入口','code':'https_required','mobile_url':settings.mobile_url,'trust_url':settings.mobile_trust_url},status_code=403)
        if not local_client(request) and request.url.path.startswith('/api/') and request.url.path not in {'/api/health','/api/session','/api/session/reset','/api/pairing/redeem'}:
            try:identity(request)
            except HTTPException as exc:return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, error):
        return JSONResponse({"detail": "找不到当前会话的任务"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        recorded=getattr(request.state,'recorded_message',None)
        if recorded and shared.by_key(recorded['sid'],recorded['key']+':error') is None:
            shared.append(recorded['sid'],recorded['key']+':error','assistant',str(error),'text',device_id=recorded['device_id'],kind='request_error')
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def denied(request,error):
        return JSONResponse({'detail':'设备尚未配对、已断开或凭据失效，请重新连接。'},status_code=401)

    @app.exception_handler(PairingRateLimited)
    async def pairing_limited(request,error):
        return JSONResponse({'detail':'配对尝试过多，请稍后重试。'},status_code=429,headers={'Retry-After':str(error.retry_after)})

    def identity(request, allow_unpaired=False):
        token=request.cookies.get('preview_device')
        if token:
            try:device=devices.authenticate(token)
            except PermissionError:raise HTTPException(401,'设备连接已失效，请重新连接')
            if not allow_unpaired and not device['trusted']:
                raise HTTPException(403,'请先输入电脑显示的配对码')
            return device
        sid=request.cookies.get('preview_session')
        if local_client(request) and sid and store.has_session(sid):
            return {'id':None,'sid':sid,'session_id':sid,'name':'本机浏览器','kind':'desktop','trusted':True,'paired':False,'binding_version':0}
        raise HTTPException(401,'请刷新页面初始化设备连接')

    def session(request):
        return identity(request)['sid']

    def still_bound(request, expected):
        try:
            current=identity(request)
            return current['sid']==expected['sid'] and current['id']==expected['id'] and current['binding_version']==expected['binding_version']
        except HTTPException:return False

    def record_request(request,device,key,text):
        record_input(shared,device['sid'],key,text,device['id'])
        request.state.recorded_message={'sid':device['sid'],'key':key,'device_id':device['id']}

    def public_device(device):
        return {k:v for k,v in device.items() if k not in {'sid','session_id','token','token_hash'}}

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": "dry_run", "vehicle_execution": False}

    @app.post("/api/session")
    async def start_session(request: Request, body: DeviceInit | None = None):
        token=request.cookies.get('preview_device')
        if token:
            device=devices.authenticate(token)
            return {'ready':True,'device':public_device(device),'pairing_required':not device['trusted']}
        sid=request.cookies.get('preview_session') if local_client(request) else None
        if not sid or not store.has_session(sid):sid=store.create_session()
        initial=body or DeviceInit()
        issued=devices.issue(sid,initial.name,initial.kind,trusted=local_client(request))
        response=JSONResponse({'ready':True,'device':issued['device'],'pairing_required':not issued['device']['trusted']})
        secure=request.url.scheme=='https'
        response.set_cookie('preview_device',issued['token'],httponly=True,secure=secure,samesite='strict',max_age=7*86400)
        response.set_cookie('preview_session',sid,httponly=True,secure=secure,samesite='strict',max_age=7*86400)
        return response

    @app.post('/api/session/reset')
    async def reset_session(request: Request):
        token=request.cookies.get('preview_device')
        if token:
            try:
                device=devices.authenticate(token)
                devices.revoke(device['sid'],device['id'])
                current_voice=voices.get(device['sid'])
                if current_voice and current_voice['device_id']==device['id']:
                    await current_voice['socket'].close(code=4001)
            except PermissionError:pass
        response=JSONResponse({'ready':False})
        response.delete_cookie('preview_device');response.delete_cookie('preview_session')
        return response

    @app.post('/api/pairing/code')
    async def pairing_code(request: Request):
        device=identity(request)
        if not device['id']:raise HTTPException(409,'请刷新页面后再生成配对码')
        return {**devices.create_code(device['id']),'mobile_url':settings.mobile_url or str(request.base_url).rstrip('/')+'/mobile','trust_url':settings.mobile_trust_url}

    @app.post('/api/pairing/redeem')
    async def pairing_redeem(body: PairingInput, request: Request):
        device=identity(request,allow_unpaired=True)
        if not device['id']:raise HTTPException(409,'请刷新页面初始化设备连接')
        rebound=devices.redeem(device['id'],body.code)
        old_voice=voices.get(device['sid'])
        if old_voice and old_voice['device_id']==device['id']:
            await old_voice['socket'].close(code=4001)
        response=JSONResponse({'paired':True,'device':public_device(rebound)})
        response.set_cookie('preview_session',rebound['sid'],httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=7*86400)
        return response

    @app.get('/api/devices')
    async def list_devices(request: Request):
        device=identity(request)
        return {'devices':devices.devices(device['sid']),'current_device_id':device['id']}

    @app.delete('/api/devices/{device_id}')
    async def revoke_device(device_id: str,request: Request):
        caller=identity(request)
        revoked=devices.revoke(caller['sid'],device_id)
        voice=voices.get(caller['sid'])
        if voice and voice['device_id']==device_id:
            await voice['socket'].close(code=4001)
        return {'device':revoked}

    @app.get('/api/voice-owner')
    async def voice_owner(request: Request):
        owner=voices.get(session(request))
        return {'owner':{k:owner[k] for k in ('device_id','name','kind')} if owner else None}

    @app.post('/api/voice/takeover')
    async def voice_takeover(request: Request):
        device=identity(request)
        old=voices.pop(device['sid'],None)
        if old:
            try:
                await old['socket'].send_json({'type':'voice.released','message':'语音已切换到另一台设备'})
                await old['socket'].close(code=4001)
            except Exception:pass
        return {'released':True,'message':'可以在本设备开启语音'}

    @app.get("/api/config")
    async def config():
        return {"mode": "mock", "schema_source": "配置驱动的示例能力目录；不代表真实车型", "hermes_configured": bool(settings.hermes_key), "backend_model": settings.backend_model, "voice_configured": settings.audio_ready, "audio_model": settings.audio_model, "mobile_url":settings.mobile_url, "trust_url":settings.mobile_trust_url, "tools_count": len(catalog.snapshot().tools)}

    @app.get("/api/hermes-health")
    async def hermes_health():
        if not settings.hermes_key:
            return {"ready": False, "status": "not_configured"}
        try:
            capabilities = await manager.hermes.capabilities()
            ready = bool(capabilities.get("features", {}).get("run_submission"))
            return {"ready": ready, "status": "api_connected" if ready else "incompatible_api"}
        except (httpx.HTTPError, ValueError):
            return {"ready": False, "status": "unreachable"}

    @app.get("/api/catalog")
    async def get_catalog():
        snap = catalog.snapshot()
        from .capability_catalog import json_read
        return {"catalog_revision": snap.revision, "format_version": snap.bundle["format_version"], "package": snap.bundle["package"], "counts": {kind: sum(t["kind"] == kind for t in snap.tools.values()) for kind in ["control", "manual", "status"]}, "profiles": [{k: p[k] for k in ["id", "title", "description"]} for p in snap.profiles.values()], "scenarios": list(snap.scenarios.values()), "root": snap.browse(), "output_schema": json_read(catalog.root / "schemas/result.schema.json")}

    @app.get("/api/catalog/tree")
    async def browse_catalog(path: str = "", query: str = "", offset: int = 0, limit: int = 30):
        return catalog.snapshot().browse(path.split(".") if path else [], query, offset, limit)

    @app.get("/api/catalog/tools/{tool_id}")
    async def describe_tool(tool_id: str):
        return catalog.snapshot().describe([tool_id])

    @app.get("/api/catalog/export")
    async def export_catalog(request: Request):
        session(request)
        snap = catalog.snapshot()
        return JSONResponse(snap.bundle, headers={"Content-Disposition": 'attachment; filename="vehicle-capabilities.bundle.json"', "X-Catalog-Revision": snap.revision})

    @app.post("/api/catalog/import/preview")
    async def preview_import(body: ImportInput, request: Request):
        session(request)
        return catalog.preview_import(body.payload, body.mode)

    @app.post("/api/catalog/import/apply")
    async def apply_import(body: ApplyImportInput, request: Request):
        session(request)
        snap = catalog.apply_import(body.preview_id, body.base_revision)
        return {"catalog_revision": snap.revision, "applied": True}

    @app.post("/api/catalog/reload")
    async def reload_catalog(request: Request):
        session(request)
        before = catalog.snapshot()
        snap = catalog.reload()
        return {"catalog_revision": snap.revision, "diff": catalog.diff(before.bundle, snap.bundle)}

    @app.post("/api/session/profile")
    async def select_profile(body: ProfileInput, request: Request):
        sid = session(request)
        if body.profile_id not in catalog.snapshot().profiles:
            raise HTTPException(422, "模拟配置不存在")
        store.select_profile(sid, body.profile_id)
        return {"profile_id": body.profile_id, "applies_to": "new_tasks_only"}

    @app.get("/api/session/profile")
    async def get_profile(request: Request):
        snap = catalog.snapshot()
        value = store.session_profile(session(request))
        if value not in snap.profiles:
            value = "demo-ready" if "demo-ready" in snap.profiles else next(iter(snap.profiles))
        return {"profile_id": value}

    @app.get("/api/tasks")
    async def tasks(request: Request):
        sid=session(request)
        await manager.expire_questions()
        focus=manager.router.current(sid)
        dialog=manager.dialog(sid)
        return {"tasks": [public_task(t) for t in store.tasks(sid)],'focus_task_id':focus['id'] if focus else None,
                'dialog':{'kind':dialog['kind'],'question':dialog['question'],'expires':dialog['expires']} if dialog else None}

    def public_task(task):
        import json
        result = {k: v for k, v in task.items() if k not in {"session_id", "request_key", "run_id"}}
        if task.get("catalog_revision"):
            receipts = store.capability_results(task["id"])
            result["results"] = receipts
            result["output"] = json.loads(task["final_result"]) if task.get("final_result") else ({"schema_version": "capability-result/v1", "catalog_revision": task["catalog_revision"], "profile_id": task["profile_id"], "mode": "mock", "executed": False, "outcome": None, "calls": task["calls"], "results": receipts} if receipts else None)
        else:
            result["output"] = legacy_catalog.output(task["calls"]) if task["calls"] else None
            result["results"] = []
        change = store.latest_change(task['id'])
        result['superseded_result_ids'] = []
        if change:
            with store.connect() as db:
                result['superseded_result_ids'] = [r['id'] for r in db.execute('SELECT id,response FROM capability_calls WHERE task_id=? AND task_revision<?',(task['id'],change['revision'])) if json.loads(r['response']).get('kind')=='control']
        result['latest_change'] = ({k:v for k,v in change.items() if k not in {'run_id'}} if change else None)
        result.pop("final_result", None)
        result["navigation"] = store.task_navigation(task["id"])
        snapshot = catalog.snapshot(task["catalog_revision"]) if task.get("catalog_revision") else None
        result["process"] = build_process(task, store.task_events(task["id"]), snapshot, result["results"])
        result['waits'] = manager.loop_events.waits(task['id'],task['session_id']) if hasattr(manager,'loop_events') else []
        result["elapsed_seconds"] = round(task["updated"]-task["created"], 2) if task["status"] not in {"running","waiting","awaiting_input"} else None
        result["simulated_dispatch"] = task.get("outcome") in {"prepared", "mixed"} or (not task.get("catalog_revision") and task["status"] == "generated")
        return result

    def public_plan(plan):
        return {k:v for k,v in plan.items() if k not in {'session_id','request_key'}}

    def public_interaction(result):
        value=dict(result)
        if value.get('task'):value['task']=public_task(value['task'])
        if value.get('plan'):value['plan']=public_plan(value['plan'])
        if 'plans' in value:value['plans']=[public_plan(p) for p in value['plans']]
        return value

    @app.post('/api/conversation/input')
    async def conversation_input(body: ConversationInput, request: Request):
        device=identity(request);sid=device['sid'];key='input:'+body.event_id
        record_request(request,device,key,body.text)
        result=await manager.handle_input(sid,body.text,body.event_id,body.task_id,body.task_revision)
        record_result(shared,sid,key,result,device['id'])
        return public_interaction(result)

    @app.get('/api/conversation/messages')
    async def conversation_messages(request: Request,before: int | None = None,limit: int = 100):
        sid=session(request)
        shared.sync_events(sid)
        return shared.snapshot(sid,before,limit)

    @app.get('/api/conversation/stream')
    async def conversation_stream(request: Request,after: int = 0):
        bound=identity(request);sid=bound['sid']
        try:cursor=max(0,after,int(request.headers.get('last-event-id','0')))
        except ValueError:raise HTTPException(422,'无效消息游标')
        async def stream():
            nonlocal cursor
            heartbeat=time.monotonic()
            while not await request.is_disconnected():
                if not still_bound(request,bound):
                    yield 'event: revoked\ndata: {"message":"设备连接已失效"}\n\n';return
                shared.sync_events(sid)
                batch=shared.events(sid,cursor)
                for event in batch['events']:
                    if not still_bound(request,bound):
                        yield 'event: revoked\ndata: {"message":"设备连接已失效"}\n\n';return
                    yield f"id: {event['seq']}\ndata: {json.dumps({**event,'cursor':event['seq']},ensure_ascii=False)}\n\n"
                cursor=batch['cursor']
                if time.monotonic()-heartbeat>=15:
                    yield ': keepalive\n\n';heartbeat=time.monotonic()
                await asyncio.sleep(.25)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

    @app.get('/api/plans')
    async def plans(request: Request):
        sid=session(request)
        return {'plans':[{**public_plan(p),'runs':manager.plans.runs(sid,p['id'])} for p in manager.plans.list(sid)],'timezone':manager.plans.timezone}

    @app.post('/api/plans',status_code=202)
    async def create_plan(body: PlanInput,request: Request):
        device=identity(request);sid=device['sid'];key='plan-create:'+body.request_key
        frequency={'daily':'每天','weekly':'每周','interval':'按间隔'}.get(body.schedule.get('frequency'),'按计划')
        label=frequency+' '+str(body.schedule.get('time') or '')
        record_request(request,device,key,'创建周期计划：'+label+' '+body.goal)
        result=manager.create_plan(sid,body.goal,body.schedule,body.request_key,body.profile_id)
        record_result(shared,sid,key,result,device['id'])
        return public_interaction(result)

    @app.get('/api/plans/{plan_id}')
    async def get_plan(plan_id: str,request: Request):
        sid=session(request)
        return {**public_plan(manager.plans.get(sid,plan_id)),'runs':manager.plans.runs(sid,plan_id)}

    @app.post('/api/plans/{plan_id}/action')
    async def plan_action(plan_id: str,body: PlanActionInput,request: Request):
        changes={k:v for k,v in {'goal':body.goal,'schedule':body.schedule,'profile_id':body.profile_id}.items() if v is not None}
        device=identity(request);sid=device['sid'];key=input_key('plan-action',plan_id,body.action,body.revision,changes)
        labels={'pause':'暂停计划','resume':'恢复计划','cancel':'取消计划','cancel_once':'取消这一次','cancel_plan':'取消整个计划','update':'修改计划','edit':'修改计划'}
        plan=manager.plans.get(sid,plan_id)
        record_request(request,device,key,labels[body.action]+'：'+plan['goal'])
        result=await manager.plan_action(sid,plan_id,body.action,body.revision,**changes)
        record_result(shared,sid,key,result,device['id'])
        return public_interaction(result)

    @app.get('/api/plan-events')
    async def plan_events(request: Request,after: int=0):
        return {'events':[{k:v for k,v in e.items() if k!='session_id'} for e in manager.plans.events(session(request),max(0,after))]}

    @app.post("/api/tasks", status_code=202)
    async def create_task(body: TaskInput, request: Request):
        if not body.goal.strip():
            raise HTTPException(422, "请输入需求")
        device=identity(request);sid=device['sid'];key='create:'+body.request_key
        record_request(request,device,key,body.goal.strip())
        task = manager.submit(sid, body.goal.strip(), body.request_key, body.profile_id, body.discovery_mode, body.completion_mode, body.parent_task_id)
        record_result(shared,sid,key,{'task':task,'message':'收到，我来处理。'},device['id'])
        return public_task(task)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        return public_task(store.task(task_id, session(request)))

    @app.post("/api/tasks/{task_id}/answer", status_code=202)
    async def answer(task_id: str, body: AnswerInput, request: Request):
        if not body.answer.strip():
            raise HTTPException(422, "请输入补充信息")
        device=identity(request);sid=device['sid'];key=input_key('answer',task_id,body.task_revision,body.answer)
        record_request(request,device,key,body.answer.strip())
        task=await manager.resume(sid,task_id,body.answer.strip(),body.task_revision)
        record_result(shared,sid,key,{'task':task,'message':'收到，接着处理。'},device['id'])
        return public_task(task)

    @app.post("/api/tasks/{task_id}/revise", status_code=202)
    async def revise(task_id: str, body: ReviseInput, request: Request):
        device=identity(request);sid=device['sid'];key='revise:'+body.change_id
        record_request(request,device,key,body.text)
        task=await manager.revise(sid,task_id,body.text,body.task_revision,body.change_id)
        record_result(shared,sid,key,{'task':task,'message':'已收到修改，接着处理。'},device['id'])
        return public_task(task)

    @app.post("/api/tasks/{task_id}/followup", status_code=202)
    async def followup(task_id: str, body: FollowupInput, request: Request):
        device=identity(request);sid=device['sid'];key='followup:'+body.event_id
        record_request(request,device,key,body.text)
        result=manager.append_followup(sid,task_id,body.text,body.task_revision,body.event_id)
        record_result(shared,sid,key,{**result,'message':'已排队，当前任务完成后处理。'},device['id'])
        return result

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        device=identity(request);sid=device['sid'];task=store.task(task_id,sid)
        key=input_key('cancel',task_id,task['revision'])
        record_request(request,device,key,'取消这次任务')
        task=await manager.cancel(sid,task_id)
        record_result(shared,sid,key,{'task':task},device['id'])
        return public_task(task)

    @app.get("/api/events")
    async def events(request: Request, after: int = 0):
        return {"events": store.events(session(request), max(after, 0))}

    @app.get("/api/events/cursor")
    async def event_cursor(request: Request):
        return {"cursor": store.event_cursor(session(request))}

    @app.get("/api/events/stream")
    async def stream_events(request: Request, after: int = 0):
        bound=identity(request)
        sid=bound['sid']
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "无效事件游标")
        async def generate():
            nonlocal cursor
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                if not still_bound(request,bound):
                    yield 'event: revoked\ndata: {"message":"设备连接已失效"}\n\n'
                    return
                events = store.events(sid, cursor)
                for event in events:
                    if not still_bound(request,bound):
                        yield 'event: revoked\ndata: {"message":"设备连接已失效"}\n\n';return
                    cursor = event["id"]
                    # Send identifiers only; the UI fetches the sanitized task projection.
                    payload = {k: event[k] for k in ["id", "task_id", "kind", "created"]}
                    yield f"id: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if time.monotonic()-heartbeat >= 15:
                    yield ": keepalive\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(.2)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/examples/{tool_name}")
    async def example(tool_name: str, request: Request):
        """Explicit fixture preview, not instruction parsing and not model reasoning."""
        import uuid
        snap = catalog.snapshot()
        if tool_name not in snap.tools:
            raise HTTPException(404, "没有这个示例")
        sid = session(request)
        profile_id = store.session_profile(sid) or ("demo-ready" if "demo-ready" in snap.profiles else next(iter(snap.profiles)))
        if profile_id not in snap.profiles:
            raise HTTPException(409, "所选模拟配置已移除，请重新选择")
        item = snap.tools[tool_name]
        example = item["examples"][0]
        task, _ = store.create_task(sid, example["instruction"], uuid.uuid4().hex, source="fixture", catalog_revision=snap.revision, profile_id=profile_id)
        service = CapabilityTools(store, catalog)
        context = {"task_id": task["id"], "task_revision": 0}
        response = service.call("call_tools", {**context, "catalog_revision": snap.revision, "calls": [{"call_id": "example-1", "tool_id": tool_name, "tool_version": item["version"], "arguments": example["arguments"]}]})
        receipt = response["results"][0]
        outcome = "prepared" if receipt["status"] == "prepared" else "answered" if receipt["status"] == "returned_mock" else "unsupported"
        summary = receipt["error"]["message"] if receipt["error"] else f"{item['title']}的模拟数据已返回，可在结构化结果中查看内容和来源。"
        service.call("finish_task", {**context, "outcome": outcome, "summary": summary, "result_ids": [receipt["result_id"]]})
        return public_task(store.task(task["id"]))

    @app.websocket("/ws/voice")
    async def voice(ws: WebSocket):
        if not allowed_host(ws,settings) or not same_origin(ws.headers.get('origin'),ws.headers.get('host')) or (not local_client(ws) and ws.url.scheme!='wss'):
            await ws.close(code=1008);return
        try:bound=identity(ws)
        except HTTPException:
            await ws.close(code=1008);return
        sid=bound['sid']
        await ws.accept()
        if sid in voices:
            await ws.send_json({'type':'voice.busy','message':'另一台设备正在语音对话，可显式接管。','owner':{k:voices[sid][k] for k in ('device_id','name','kind')}})
            await ws.close(code=4009);return
        connection_id=uuid.uuid4().hex
        voices[sid]={'device_id':bound['id'],'name':bound['name'],'kind':bound['kind'],'connection_id':connection_id,'socket':ws}
        bridge=VoiceBridge(ws,sid,settings,store,manager)
        bridge.device_id=bound['id']
        bridge.device_name=bound['name']
        bridge.binding_valid=lambda: still_bound(ws,bound) and voices.get(sid,{}).get('connection_id')==connection_id
        async def watch_binding():
            while True:
                await asyncio.sleep(1)
                if not still_bound(ws,bound) or voices.get(sid,{}).get('connection_id')!=connection_id:
                    await ws.close(code=4001);return
        monitor=asyncio.create_task(watch_binding())
        try:
            await bridge.run()
        finally:
            monitor.cancel();await asyncio.gather(monitor,return_exceptions=True)
            if voices.get(sid,{}).get('connection_id')==connection_id:voices.pop(sid,None)

    @app.get('/mobile')
    @app.get('/mobile/')
    async def mobile():
        return FileResponse(ROOT/'web/mobile.html')

    @app.get('/mobile/trust.crt')
    async def trust_certificate():
        if not settings.mobile_ca_path.is_file():raise HTTPException(404,'尚未配置本地 HTTPS')
        return FileResponse(settings.mobile_ca_path,media_type='application/x-x509-ca-cert',filename='personal-agent-local-ca.crt')

    @app.get('/mobile/access')
    async def mobile_access():
        return {'mobile_url':settings.mobile_url,'trust_url':settings.mobile_trust_url}

    @app.get('/mobile/connect')
    async def mobile_connect():
        return FileResponse(ROOT/'web/mobile-connect.html')

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "web/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


# Factory avoids touching the database when importing modules for tests or MCP.
