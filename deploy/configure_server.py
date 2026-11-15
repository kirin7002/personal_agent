"""Configure only this checkout. Secrets stay in its existing private .env."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent.parent

def private(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(text)
    path.chmod(0o600)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--origin',required=True)
    parser.add_argument('--bind-host',default='172.17.0.1')
    parser.add_argument('--port',type=int,default=18765)
    parser.add_argument('--trusted-proxy',action='append',required=True)
    args=parser.parse_args();origin=args.origin.rstrip('/');url=urlsplit(origin)
    if url.scheme!='https' or not url.hostname or url.path or url.username or url.password:
        raise SystemExit('Use a clean HTTPS origin')
    private(ROOT/'.runtime/server-access.json',json.dumps({'origin':origin,'bind_host':args.bind_host,'port':args.port,'trusted_proxy_ips':args.trusted_proxy},indent=2)+'\n')
    private(ROOT/'.runtime/mobile-access.json',json.dumps({'allowed_hosts':[url.hostname],'mobile_url':origin+'/mobile','trust_url':''},indent=2)+'\n')
    subprocess.run([sys.executable,str(ROOT/'scripts/configure_local_hermes.py')],check=True)
    print('Server origin and explicit proxy configuration prepared; no credentials printed.')
