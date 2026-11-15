"""Probe-only telemetry and completed-task close, bound to the runtime session ID."""
import os
import json
from pathlib import Path
from app.store import Store
from types import SimpleNamespace
from .adapter import case


def local_response(message,model):
    return SimpleNamespace(id='local_steer_probe_completion',type='message',role='assistant',model=model,
        content=[SimpleNamespace(type='text',text=message)],stop_reason='end_turn',stop_sequence=None,
        usage=SimpleNamespace(input_tokens=0,output_tokens=0,cache_read_input_tokens=0,cache_creation_input_tokens=0))


def execute(request,next_call,session_id='',api_mode='',**kwargs):
    audit={'stage':'execute','api_mode':api_mode,'session_id':session_id,'roles':[m.get('role') for m in request.get('messages',[])], 'content_shapes':[[type(m.get('content')).__name__,len(str(m.get('content','')))] for m in request.get('messages',[])]}
    with open(os.environ['PROBE_AUDIT_PATH'],'a') as f:f.write(json.dumps(audit)+'\n')
    if api_mode!='anthropic_messages':return next_call(request)
    store=Store(Path(os.environ['PREVIEW_DB']));tid=None
    # API conversation names and the actual runtime session ID can differ.
    # Resolve this probe's trusted initial input envelope, not a generated tool arg.
    for message in request.get('messages',[]):
        if message.get('role')!='user':continue
        content=message.get('content',[])
        pieces=[content] if isinstance(content,str) else [b.get('text','') for b in content if isinstance(b,dict) and b.get('type')=='text']
        for text in pieces:
            try:
                data=json.loads(text);candidate=case(store,data['task_id'])
                if candidate and data.get('user_request')==candidate['old_goal']:
                    tid=candidate['task_id'];break
            except (ValueError,KeyError,TypeError):pass
        if tid:break
    if tid is None:return next_call(request)
    task=store.task(tid)
    if task['status'] in {'generated','completed'} and task.get('final_result'):
        store.event(tid,'probe.local_end',{'binding':'runtime_session'})
        return local_response(task['summary'],request.get('model',''))
    store.event(tid,'probe.provider_request',{'task_revision_at_start':task['revision']})
    response=next_call(request)
    blocks=[{'name':b.name,'arguments':b.input} for b in getattr(response,'content',[]) if getattr(b,'type','')=='tool_use']
    store.event(tid,'probe.provider_response',{'tool_calls':blocks})
    return response


def register(ctx):
    with open(os.environ['PROBE_AUDIT_PATH'],'a') as f:f.write('{"stage":"register"}\n')
    ctx.register_middleware('llm_execution',execute)
