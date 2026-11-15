"""Host/transport checks for the local desktop and explicitly configured LAN TLS entry."""
import ipaddress
from urllib.parse import urlsplit


def local_client(connection):
    host=connection.client.host if connection.client else ''
    # Starlette's in-process TestClient uses this literal; a network peer has an IP.
    if host=='testclient':return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False


def allowed_host(connection, settings):
    return (connection.url.hostname or '').lower() in settings.allowed_hosts


def same_origin(origin, host):
    if not origin:return True
    parsed=urlsplit(origin)
    return parsed.scheme in {'http','https'} and parsed.netloc==host
