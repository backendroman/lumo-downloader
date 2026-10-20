import socket

import pytest

from app.core import netguard
from app.core.netguard import UnsafeURLError, is_public_ip, redact_url, validate_public_url


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc",
    "http://example.com/video.mp4",
    "https://example.com:443/a",
    "https://example.com:8443/a",
    "example.com/watch?v=1",
])
def test_valid_public_urls(url):
    result = validate_public_url(url)
    assert result.startswith(("http://", "https://"))


@pytest.mark.parametrize("url,code", [
    ("ftp://example.com/file", "unsupported_scheme"),
    ("file:///etc/passwd", "unsupported_scheme"),
    ("javascript:alert(1)", "unsupported_scheme"),
    ("data:text/html,hi", "unsupported_scheme"),
    ("gopher://example.com/", "unsupported_scheme"),
])
def test_disallowed_schemes(url, code):
    with pytest.raises(UnsafeURLError) as exc:
        validate_public_url(url)
    assert exc.value.code == code


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "https://",
    "https://exa mple.com/",
    "https://example.com/\x00",
    "https://user:pass@example.com/",
    "https://example.com:99999/",
    "https://" + "a" * 3000 + ".com",
])
def test_malformed_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://LOCALHOST./admin",
    "http://127.0.0.1/",
    "http://127.1.2.3:80/",
    "http://0.0.0.0/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",  # CGNAT
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
    "http://metadata.google.internal/",
    "http://router.local/",
    "http://intranet/",
    "http://internal.example.com/",  # resolves to 10.0.0.5 (see conftest)
    "http://mixed.example.com/",  # one of the answers is loopback
    "https://example.com:22/",  # non-standard port
])
def test_internal_targets_are_blocked(url):
    with pytest.raises(UnsafeURLError) as exc:
        validate_public_url(url)
    assert exc.value.code == "forbidden_target"


def test_unresolvable_host():
    with pytest.raises(UnsafeURLError) as exc:
        validate_public_url("https://nxdomain.example.com/v")
    assert exc.value.code == "dns_error"


@pytest.mark.parametrize("ip,expected", [
    ("8.8.8.8", True),
    ("93.184.216.34", True),
    ("2606:4700::1111", True),
    ("127.0.0.1", False),
    ("10.0.0.1", False),
    ("169.254.1.1", False),
    ("224.0.0.1", False),
    ("::1", False),
    ("::ffff:10.0.0.1", False),
    ("not-an-ip", False),
])
def test_is_public_ip(ip, expected):
    assert is_public_ip(ip) is expected


def test_redact_url_hides_query_and_credentials():
    redacted = redact_url("https://user:secret@example.com/watch?v=1&token=SECRET#frag")
    assert "SECRET" not in redacted and "secret" not in redacted
    assert redacted == "https://example.com/watch?..."


def test_socket_guard_blocks_private_connections(monkeypatch):
    monkeypatch.setattr(netguard, "_GUARD_INSTALLED", False)
    orig_gai, orig_connect, orig_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    try:
        netguard.install_socket_guard()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("127.0.0.1", 9))
            with pytest.raises(ConnectionRefusedError):
                sock.connect(("169.254.169.254", 80))
        finally:
            sock.close()
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("internal.example.com", 443)
    finally:
        socket.getaddrinfo = orig_gai
        socket.socket.connect = orig_connect
        socket.socket.connect_ex = orig_connect_ex
