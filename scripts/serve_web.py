"""One application instance, local HTTP plus optional LAN TLS listeners."""
import asyncio
from contextlib import nullcontext
from pathlib import Path
import signal
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import uvicorn
from app.config import Settings,mobile_access_config
from app.main import create_app


async def main():
    access=mobile_access_config()
    app=create_app(Settings())
    configs=[uvicorn.Config(app,host=access.get('http_host','127.0.0.1'),port=8765,lifespan='on',access_log=False,timeout_graceful_shutdown=3)]
    if access.get('certfile') and access.get('keyfile'):
        configs.append(uvicorn.Config(app,host=access.get('https_host','0.0.0.0'),port=int(access.get('https_port',8766)),
                                     lifespan='off',access_log=False,timeout_graceful_shutdown=3,
                                     ssl_certfile=access['certfile'],ssl_keyfile=access['keyfile']))
    servers=[uvicorn.Server(config) for config in configs]
    for server in servers:server.capture_signals=nullcontext
    loop=asyncio.get_running_loop()
    def stop():
        for server in servers:server.should_exit=True
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop)
    jobs=[asyncio.create_task(server.serve()) for server in servers]
    done,pending=await asyncio.wait(jobs,return_when=asyncio.FIRST_COMPLETED)
    stop()
    await asyncio.gather(*jobs)
    for job in done:job.result()


if __name__=='__main__':asyncio.run(main())
