"""Configure only this project's private Hermes home; no secrets on stdout."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_HOME = ROOT / ".runtime/hermes-home"


def private_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


def main():
    env_path = ROOT / ".env"
    env = dotenv_values(env_path)
    key = env.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("请先在项目 .env 配置百炼凭据")
    local_key = env.get("HERMES_API_KEY") or secrets.token_urlsafe(32)
    set_key(str(env_path), "HERMES_API_KEY", local_key)
    set_key(str(env_path), "HERMES_API_URL", "http://127.0.0.1:8642")
    env_path.chmod(0o600)
    defaults = (ROOT / ".runtime/hermes-agent/hermes_cli/config_defaults.py").read_text()
    version = int(re.search(r'"_config_version":\s*(\d+)', defaults)[1])
    model = env.get("BAILIAN_MODEL", "deepseek-v4.1-flash-1017")
    config = {
        "_config_version": version,
        "model": {"default": model, "provider": "custom:vehicle-bailian", "context_length": 65536},
        "providers": {"vehicle-bailian": {
            "api": env.get("BAILIAN_ANTHROPIC_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic"),
            "key_env": "DASHSCOPE_API_KEY", "transport": "anthropic_messages",
            "default_model": model, "context_length": 65536, "discover_models": False,
        }},
        "fallback_providers": [],
        "auth": {"adopt_external_logins": False},
        "nous": {"guest": False},
        "updates": {"check": False},
        "plugins": {"enabled": ["bailian-limits", "vehicle-completion"], "auto_update_check_hours": 0, "auto_apply": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "agent": {"max_turns": 24},
        "tools": {"tool_search": {"enabled": "off"}},
        "toolsets": ["mcp-vehicle_preview"],
        "platform_toolsets": {"api_server": ["mcp-vehicle_preview"], "cli": ["mcp-vehicle_preview"]},
        "mcp_servers": {"vehicle_preview": {
            "command": str(ROOT / ".venv/bin/python"), "args": ["-m", "app.mcp_server"],
            "cwd": str(ROOT), "env": {"PREVIEW_DB": str(ROOT / ".data/preview.sqlite3")},
            "timeout": 10, "trust": "full", "sampling": {"enabled": False},
        }},
        "gateway": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": 8642, "max_concurrent_runs": 3}},
    }
    private_write(RUNTIME_HOME / "config.yaml", json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    private_write(RUNTIME_HOME / ".env", f"DASHSCOPE_API_KEY={key}\nAPI_SERVER_KEY={local_key}\nAPI_SERVER_ENABLED=true\n")
    shutil.copytree(ROOT / "hermes_plugins/bailian_limits", RUNTIME_HOME / "plugins/bailian-limits", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "hermes_plugins/vehicle_completion", RUNTIME_HOME / "plugins/vehicle-completion", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    print("已生成项目隔离的 Hermes 配置，仅开启车辆预览 MCP；凭据未回显。")


if __name__ == "__main__":
    main()
