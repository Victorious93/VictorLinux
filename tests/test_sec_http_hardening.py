"""Pre-auth HTTP hardening: admin API vs browsers, garbage Content-Length, and a stalled TLS client."""
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "airootfs/usr/local/lib/victorlinux"))

import victorinput as vi  # noqa: E402


@pytest.fixture
def app(tmp_path):
    a = vi.InputApp(str(tmp_path), sink=vi.MemorySink(), bind="127.0.0.1", port=0, admin_port=0)
    a.start_background()
    for _ in range(100):
        if a.running and a.port:
            break
        time.sleep(0.05)
    assert a.running and a.port
    yield a
    a.stop()
    time.sleep(0.05)


def _admin(app, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", app.admin_port, timeout=5)
    h = dict(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_admin_works_for_the_control_center_style_request(app):
    st, data = _admin(app, "GET", "/status")
    assert st == 200 and json.loads(data)["running"] is True
    st, data = _admin(app, "POST", "/pair-cancel", b"{}", {"Content-Type": "application/json"})
    assert st == 200


def test_admin_refuses_dns_rebinding_host(app):
    st, _ = _admin(app, "GET", "/status", headers={"Host": "attacker.example:%d" % app.admin_port})
    assert st == 403
    st, _ = _admin(app, "POST", "/pair-start", b"{}", {"Host": "evil.test", "Content-Type": "application/json"})
    assert st == 403


def test_admin_refuses_cross_site_requests(app):
    st, _ = _admin(app, "POST", "/pair-start", b"{}", {"Origin": "https://evil.example", "Content-Type": "application/json"})
    assert st == 403
    st, _ = _admin(app, "GET", "/status", headers={"Origin": "null"})
    assert st == 403


def test_admin_post_must_be_json(app):
    # text/plain and form posts are "simple" cross-origin requests that need no CORS preflight
    st, _ = _admin(app, "POST", "/pair-start", b'{"host": "x"}', {"Content-Type": "text/plain"})
    assert st == 415
    before = json.loads(_admin(app, "GET", "/status")[1])["pairing"]
    assert not before.get("active"), before          # the refused request must not have opened a pairing window


def _raw(port, payload, tls=True, timeout=4):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    if tls:
        s = ssl._create_unverified_context().wrap_socket(s)
    s.sendall(payload)
    try:
        return s.recv(4096)
    finally:
        s.close()


@pytest.mark.parametrize("length", ["-1", "abc", "1e3", "٣"])
def test_garbage_content_length_is_rejected_not_hung(app, length):
    req = (f"POST /victor/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode()
    t0 = time.time()
    out = _raw(app.port, req)
    assert out.startswith(b"HTTP/1.1 400"), out[:40]
    assert time.time() - t0 < 3


def test_stalled_tls_client_does_not_block_other_clients(app):
    stalled = [socket.create_connection(("127.0.0.1", app.port)) for _ in range(3)]   # TCP only, never a ClientHello
    try:
        time.sleep(0.2)
        c = http.client.HTTPSConnection("127.0.0.1", app.port, timeout=4, context=ssl._create_unverified_context())
        c.request("GET", "/health")
        assert c.getresponse().status == 200
        c.close()
    finally:
        for s in stalled:
            s.close()


def test_slow_unauthenticated_http_client_is_dropped(app, monkeypatch):
    # shrink the pre-auth timeout through the handler class used by this server
    handler_cls = app._httpd.RequestHandlerClass
    monkeypatch.setattr(handler_cls, "timeout", 0.5)
    s = ssl._create_unverified_context().wrap_socket(socket.create_connection(("127.0.0.1", app.port), timeout=4))
    s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")      # never finishes the headers
    t0 = time.time()
    try:
        got = s.recv(10)
    except (ssl.SSLError, ConnectionError):
        got = b""
    s.close()
    assert got == b"" and time.time() - t0 < 3      # the server hung up on its own, long before the client gave up


def test_victormic_mac_compare_accepts_non_ascii_without_raising():
    import hmac
    assert hmac.compare_digest("abc".encode(), "ábc".encode()) is False   # the str form raises TypeError
    src = (ROOT / "airootfs/usr/local/lib/victorlinux/victormic.py").read_text()
    assert 'hmac.compare_digest(want.encode(), str(h.get("mac", "")).encode())' in src


def test_victorinput_copies_identical_across_editions():
    deb = ROOT / "debian-edition/config/includes.chroot_after_packages/usr/local/lib/victorlinux"
    arch = ROOT / "airootfs/usr/local/lib/victorlinux"
    for n in ("victorinput.py", "victormic.py"):
        assert (arch / n).read_bytes() == (deb / n).read_bytes(), n


def test_gateway_content_length_helper():
    import gateway
    assert gateway.content_length({"Content-Length": "12"}, 100) == 12
    assert gateway.content_length({}, 100) == 0
    for bad in ("-5", "x", "", " "):
        if bad.strip():
            with pytest.raises(ValueError):
                gateway.content_length({"Content-Length": bad}, 100)
    with pytest.raises(ValueError):
        gateway.content_length({"Content-Length": "101"}, 100)


def _generic_stall_check(tls_per_connection, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    ctx, _fp, _cert = vi.ssl_context(str(tmp_path))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    tls_per_connection(srv, ctx, handshake_timeout=1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    stalled = socket.create_connection(("127.0.0.1", port))
    try:
        time.sleep(0.2)
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=4, context=ssl._create_unverified_context())
        c.request("GET", "/")
        assert c.getresponse().read() == b"ok"
        # the stalled peer is cut off after the handshake timeout
        stalled.settimeout(4)
        assert stalled.recv(1) == b""
    finally:
        stalled.close()
        srv.shutdown()
        srv.server_close()


def test_gateway_tls_helper_survives_a_stalled_client(tmp_path):
    import gateway
    _generic_stall_check(gateway.tls_per_connection, tmp_path)


def test_fleet_tls_helper_survives_a_stalled_client(tmp_path):
    sys.path.insert(0, str(ROOT / "fleet"))
    from victorfleet import server as fleet_server
    _generic_stall_check(fleet_server.tls_per_connection, tmp_path)


def test_control_center_non_ascii_token_gets_401_not_a_dropped_connection(tmp_path, monkeypatch):
    import control_center as cc
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    server = cc.Server(("127.0.0.1", 0), cc.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", server.server_port), timeout=4)
        s.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Victor-Token: caf\xe9\r\nConnection: close\r\n\r\n")
        got = s.recv(64)
        s.close()
        assert b" 401 " in got.split(b"\r\n")[0], got        # used to raise TypeError in compare_digest and drop the connection
    finally:
        server.shutdown()
        server.server_close()
