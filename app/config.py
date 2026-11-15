from dataclasses import dataclass
import os
import json
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def mobile_access_config():
    try:
        value=json.loads((ROOT/'.runtime/mobile-access.json').read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


_MOBILE_ACCESS=mobile_access_config()


@dataclass
class Settings:
    allowed_hosts: tuple[str,...] = tuple(dict.fromkeys(['localhost','127.0.0.1','::1', *[str(h).lower() for h in _MOBILE_ACCESS.get('allowed_hosts',[])]]))
    mobile_url: str = str(_MOBILE_ACCESS.get('mobile_url',''))
    mobile_trust_url: str = str(_MOBILE_ACCESS.get('trust_url',''))
    mobile_ca_path: Path = ROOT / '.runtime/mobile-tls/root-ca.crt'
    db_path: Path = Path(os.getenv("PREVIEW_DB", str(ROOT / ".data/preview.sqlite3")))
    catalog_dir: Path = Path(os.getenv("CAPABILITY_CONFIG_DIR", str(ROOT / "capabilities")))
    hermes_url: str = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
    hermes_key: str = os.getenv("HERMES_API_KEY", "")
    backend_model: str = os.getenv("BAILIAN_MODEL", "deepseek-v4.1-flash-1017")
    dashscope_key: str = os.getenv("QWEN_AUDIO_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
    workspace: str = os.getenv("BAILIAN_WORKSPACE_ID", "")
    region: str = os.getenv("BAILIAN_REGION", "cn-beijing")
    audio_model: str = os.getenv("QWEN_AUDIO_MODEL", "qwen-audio-3.1-realtime-flash")
    audio_voice: str = os.getenv("QWEN_AUDIO_VOICE", "longanqian_v3.1")
    audio_proxy: str | None = os.getenv("QWEN_AUDIO_PROXY") or None
    task_timeout: float = float(os.getenv("AGENT_TASK_TIMEOUT", "120"))
    candidate_preload_enabled: bool = os.getenv("CANDIDATE_PRELOAD_ENABLED", "true").lower() in {"1", "true", "yes"}
    candidate_preload_max_tools: int = int(os.getenv("CANDIDATE_PRELOAD_MAX_TOOLS", "3"))
    candidate_related_max_tools: int = int(os.getenv("CANDIDATE_RELATED_MAX_TOOLS", "4"))
    candidate_preload_max_chars: int = int(os.getenv("CANDIDATE_PRELOAD_MAX_CHARS", "12000"))

    direct_completion_enabled: bool = os.getenv("DIRECT_COMPLETION_ENABLED", "true").lower() in {"1", "true", "yes"}

    @property
    def audio_ready(self) -> bool:
        return bool(self.dashscope_key)

    def audio_url(self) -> str:
        import re
        from urllib.parse import urlencode
        if self.workspace and not re.fullmatch(r"[A-Za-z0-9_-]+", self.workspace):
            raise ValueError("百炼业务空间 ID 格式不正确")
        if self.region not in {"cn-beijing", "ap-southeast-1"}:
            raise ValueError("当前仅支持北京或新加坡地域")
        query = urlencode({"model": self.audio_model})
        # Audio Realtime still supports the shared endpoints; workspace hosts
        # are optional. Do not confuse this with Omni 3.8's endpoint rules.
        host = f"{self.workspace}.{self.region}.maas.aliyuncs.com" if self.workspace else {
            "cn-beijing": "dashscope.aliyuncs.com",
            "ap-southeast-1": "dashscope-intl.aliyuncs.com",
        }[self.region]
        return f"wss://{host}/api-ws/v1/realtime?{query}"
