"""Print a secret-free, project-specific MCP config snippet; does not edit Hermes config."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

config = {
    "mcp_servers": {
        "vehicle_preview": {
            "command": str(ROOT / ".venv/bin/python"),
            "args": ["-m", "app.mcp_server"],
            "cwd": str(ROOT),
            "env": {"PREVIEW_DB": str(ROOT / ".data/preview.sqlite3")},
            "timeout": 10,
        }
    }
}
print(json.dumps(config, ensure_ascii=False, indent=2))
