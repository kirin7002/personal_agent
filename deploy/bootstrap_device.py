"""Mint a short-lived first-device pairing invitation into a private file."""
import argparse,json,os,sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.store import Store
from app.device_sharing import DeviceSharing


def private(path,text):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(text)
    path.chmod(0o600)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'.runtime/first-device.txt');args=parser.parse_args()
    store=Store(Settings().db_path);sharing=DeviceSharing(store)
    record_path=ROOT/'.runtime/server-owner.json'
    record=json.loads(record_path.read_text()) if record_path.exists() else None
    owner_sid=record.get('session_id') if record and store.has_session(record.get('session_id')) else None
    if record:
        try:invite=sharing.create_code(record['device_id'])
        except PermissionError:record=None
    if not record:
        sid=owner_sid or store.create_session()
        issued=sharing.issue(sid,'服务器管理设备','server',trusted=True)
        record={'session_id':sid,'device_id':issued['device']['id']}
        private(record_path,json.dumps(record)+'\n')
        invite=sharing.create_code(record['device_id'])
    origin=json.loads((ROOT/'.runtime/server-access.json').read_text())['origin']
    text=('服务器首次连接\n\n电脑：'+origin+'/\n手机：'+origin+'/mobile\n\n一次性配对码：'+invite['code']+'\n有效至：'+datetime.fromtimestamp(invite['expires_at']).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')+'\n\n先在手机页面输入此码。配对后可为其他设备生成新配对码。过期后在服务器重新运行 deploy/bootstrap_device.py。\n')
    args.output.parent.mkdir(parents=True,exist_ok=True);private(args.output,text)
    print('Private connection instructions written to '+str(args.output))
