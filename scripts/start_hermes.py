"""Run this project's isolated, PM-managed Hermes gateway in the foreground."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / ".runtime/hermes-agent"
os.environ["HERMES_HOME"] = str(ROOT / ".runtime/hermes-home")
os.environ["HERMES_RUNTIME_DIR"] = str(ROOT / ".runtime/hermes-tools")
# A long-lived gateway must not depend on a transient development proxy for
# its own loopback API or the explicitly configured Bailian service.
direct_hosts = ["localhost", "127.0.0.1", "::1", "dashscope.aliyuncs.com"]
existing_no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
os.environ["NO_PROXY"] = ",".join(filter(None, [existing_no_proxy, *direct_hosts]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]
sys.path.insert(0, str(ROOT))
from app.config import Settings
settings = Settings()
os.environ["PREVIEW_DB"] = str(settings.db_path.resolve())
if settings.hermes_key:
    os.environ['API_SERVER_KEY'] = settings.hermes_key
    os.environ['API_SERVER_ENABLED'] = 'true'
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH", "")]))
sys.path.insert(0, str(REPO))

if not REPO.is_dir():
    raise SystemExit("请先下载 Hermes 官方源码到 .runtime/hermes-agent")

from pm.environments import project_python

python = project_python(REPO)
if not python.is_file():
    raise SystemExit("Hermes 的 PM 环境尚未准备完成")
os.chdir(REPO)
os.execv(str(python), [str(python), "-m", "gateway.run"])
