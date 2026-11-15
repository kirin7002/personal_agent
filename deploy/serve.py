"""Single server instance behind an explicitly trusted TLS reverse proxy."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import uvicorn
from app.main import create_app
from app.config import Settings

if __name__=='__main__':
    deployment=json.loads((ROOT/'.runtime/server-access.json').read_text())
    proxies=deployment.get('trusted_proxy_ips',[])
    if not proxies or '*' in proxies:
        raise SystemExit('An explicit trusted reverse proxy address is required')
    uvicorn.run(create_app(Settings()),host=deployment['bind_host'],port=deployment['port'],
                proxy_headers=True,forwarded_allow_ips=','.join(proxies),access_log=False,
                timeout_graceful_shutdown=5)
