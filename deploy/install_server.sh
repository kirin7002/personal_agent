#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installation step with sudo.' >&2
  exit 1
fi
project=/home/kirin/apps/personal_agent
install -m 644 "$project/deploy/personal-agent-web.service" /etc/systemd/system/personal-agent-web.service
install -m 644 "$project/deploy/personal-agent-hermes.service" /etc/systemd/system/personal-agent-hermes.service
systemctl daemon-reload
# Start only the web entry here. Model gateway starts after private configuration.
systemctl start personal-agent-web.service
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 \
  -H 'Host: agent.kirinxu.cn' http://172.17.0.1:18765/api/health
python3 - "$project" <<'PY'
from datetime import datetime
from pathlib import Path
import shutil,subprocess,sys
project=Path(sys.argv[1]);config=Path('/opt/caddy/Caddyfile')
original=config.read_text();fragment=(project/'deploy/Caddyfile.fragment').read_text()
block='agent.kirinxu.cn {\n    reverse_proxy host.docker.internal:18765\n}'
if 'agent.kirinxu.cn {' in original:
    if block not in original:
        raise SystemExit('Existing agent site differs; refusing to overwrite it')
    print('Agent site already installed')
else:
    folder=Path('/opt/caddy/personal-agent-backups');folder.mkdir(mode=0o700,exist_ok=True)
    backup=folder/('Caddyfile.'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(config,backup);backup.chmod(0o600)
    # Preserve the bind-mounted inode; replacing it would leave the container
    # reading the old file. The backup is restored on validation/reload failure.
    config.write_text(original.rstrip()+'\n\n'+block+'\n')
    validate=['docker','exec','caddy','caddy','validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile']
    reload=['docker','exec','caddy','caddy','reload','--config','/etc/caddy/Caddyfile','--adapter','caddyfile']
    try:
        subprocess.run(validate,check=True)
        subprocess.run(reload,check=True)
    except BaseException:
        config.write_text(original)
        subprocess.run(reload,check=False)
        raise
    print('New agent site loaded; previous Caddy configuration retained in private backup')
PY
systemctl is-active personal-agent-web.service
printf 'Services are started manually; boot enablement was not changed.\n'
