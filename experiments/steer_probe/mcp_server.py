"""Original MCP schemas with deterministic test barriers before mutation/finish."""
import asyncio,json,time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool,TextContent
from app.config import Settings
from app.store import Store
from app.capability_catalog import CapabilityCatalog
from app.capability_tools import entrypoint_schemas
from .adapter import ProbeTools,case,setup

async def main():
    store=Store(Settings().db_path);setup(store)
    service=ProbeTools(store,CapabilityCatalog(store,Settings().catalog_dir,initialize=False))
    server=Server('vehicle-call-preview')
    @server.list_tools()
    async def listed():return [Tool(**t) for t in entrypoint_schemas()]
    @server.call_tool()
    async def called(name,arguments):
        args=arguments or {};c=case(store,args.get('task_id',''))
        hit=c and not c['reached'] and ((c['barrier']=='before_control' and name=='call_tools') or (c['barrier']=='before_finish' and name=='finish_task'))
        if hit:
            with store.connect() as db:db.execute('UPDATE steer_probe_cases SET reached=1 WHERE task_id=?',(c['task_id'],))
            store.event(c['task_id'],'probe.barrier',{'name':name,'stage':c['barrier'],'arguments':args})
            deadline=time.monotonic()+20
            while not case(store,c['task_id'])['released'] and time.monotonic()<deadline:await asyncio.sleep(.025)
            if not case(store,c['task_id'])['released']:
                return [TextContent(type='text',text=json.dumps(service.error('PROBE_TIMEOUT','验证停点未释放')))]
        result=service.call(name,args)
        if c:
            store.event(c['task_id'],'probe.tool_response',{'name':name,'task_revision':args.get('task_revision'),'calls':args.get('calls',[]),'error':result.get('error'),'status':result.get('status'), 'result_ids':[r.get('result_id') for r in result.get('results',[])]})
        return [TextContent(type='text',text=json.dumps(result,ensure_ascii=False))]
    async with stdio_server() as (read,write):await server.run(read,write,server.create_initialization_options())

if __name__=='__main__':asyncio.run(main())
