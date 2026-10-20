"""URL validation and SSRF guard"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from app.services.errors import error_message

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".corp")

# is_global is True for these: NAT64 can wrap 10.x/127.x, 192.0.0.0/24 has global exceptions
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("64:ff9b::/96"),
]


class UnsafeURLError(ValueError):
    def __init__(self, message: str | None = None, code: str = "invalid_url"):
        super().__init__(message or error_message(code))
        self.code = code


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # some multicast ranges count as global
    if not ip.is_global or ip.is_multicast:
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version)


def _check_hostname(host: str) -> None:
    lowered = host.lower().rstrip(".")
    if not lowered:
        raise UnsafeURLError("В ссылке отсутствует адрес сайта.")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError(code="forbidden_target")
    try:
        ipaddress.ip_address(lowered)
        return
    except ValueError:
        pass
    # "router", "nas" etc. only exist on internal networks
    if "." not in lowered:
        raise UnsafeURLError(code="forbidden_target")


def resolve_host(host: str, port: int | None) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnsafeURLError(code="dns_error") from exc
    return sorted({str(info[4][0]) for info in infos})


def validate_public_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
    url = url.strip()
    if not url:
        raise UnsafeURLError("Вставьте ссылку на видео или аудио.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("Ссылка слишком длинная.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise UnsafeURLError("Ссылка содержит недопустимые символы.")
    if "://" not in url and ":" not in url.split("/", 1)[0]:
        url = "https://" + url

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(code="unsupported_scheme")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("Ссылки с логином и паролем не поддерживаются.")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("В ссылке указан некорректный порт.") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("Ссылки с нестандартным портом не поддерживаются.", "forbidden_target")

    host = parts.hostname or ""
    _check_hostname(host)
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise UnsafeURLError("Некорректное имя сайта в ссылке.") from exc

    if allow_private:
        return url

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise UnsafeURLError(code="forbidden_target")
        return url

    if resolve:
        addresses = resolve_host(host, port)
        if not addresses:
            raise UnsafeURLError(code="dns_error")
        if not all(is_public_ip(addr) for addr in addresses):
            raise UnsafeURLError(code="forbidden_target")
    return url


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def redact_text(text: object, limit: int = 300) -> str:
    # for yt-dlp error messages
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), str(text or ""))[:limit]


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        path = parts.path or "/"
        if len(path) > 48:
            path = path[:48] + "..."
        suffix = "?..." if parts.query else ""
        return f"{parts.scheme}://{host}{path}{suffix}"
    except Exception:
        return "<unparseable url>"


_GUARD_INSTALLED = False


def install_socket_guard(allowed_hosts: set[str] | None = None) -> None:
    """Patch socket so the current process can't connect to private addresses

    Called in the worker only. Checks the real IP on connect(), so redirects
    and DNS rebinding are covered too. allowed_hosts is for the proxy from .env
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    _GUARD_INSTALLED = True

    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    inet_families = (socket.AF_INET, socket.AF_INET6)

    allowed_ips: set[str] = set()
    for host in allowed_hosts or ():
        try:
            allowed_ips.update(str(i[4][0]) for i in original_getaddrinfo(host, None))
        except OSError:
            pass
    allowed_names = {h.lower() for h in allowed_hosts or ()}

    def _ok(ip: str) -> bool:
        return ip in allowed_ips or is_public_ip(ip)

    def guarded_getaddrinfo(host, *args, **kwargs):
        results = original_getaddrinfo(host, *args, **kwargs)
        if isinstance(host, str) and host.lower() in allowed_names:
            return results
        safe = [r for r in results if r[0] not in inet_families or _ok(str(r[4][0]))]
        if not safe:
            raise socket.gaierror(socket.EAI_NONAME, "blocked: non-public address")
        return safe

    def _assert_public(sock, address):
        if sock.family in inet_families and not _ok(str(address[0])):
            raise ConnectionRefusedError("blocked: non-public address")

    def guarded_connect(self, address):
        _assert_public(self, address)
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        _assert_public(self, address)
        return original_connect_ex(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
