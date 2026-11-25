"""Feature-module loader: optional screens and routes plug in without weakening the Control Center guards."""
import json
import os

import pytest

import cc_ext
import control_center as cc
from test_control_api import request, serving

DEB = os.path.join(os.path.dirname(__file__), "..", "debian-edition", "config", "includes.chroot_after_packages", "usr", "local")


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "TOKEN_FILE", str(tmp_path / "cache" / "cc.token"))
    monkeypatch.setenv("VICTOR_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(cc, "APP", cc.App(), raising=False)
    web = tmp_path / "web"
    web.mkdir()
    (web / "demo.html").write_text('<section id="t-demo" data-tab="demo" data-label="Demo"><p>demo</p></section>')
    (web / "demo.js").write_text('victorExt.on("demo",()=>{});')
    monkeypatch.setattr(cc_ext, "WEB", str(web))
    monkeypatch.setitem(cc_ext.ROUTES, ("GET", "/api/demo"), lambda app, q: (200, {"q": q.get("x", [""])[0]}))
    monkeypatch.setitem(cc_ext.ROUTES, ("POST", "/api/demo"), lambda app, b: (201, {"echo": b}))
    with serving(cc.Handler) as port:
        yield port, {"X-Victor-Token": cc.APP.token}


def test_sections_and_scripts_are_injected(control):
    port, _ = control
    status, page = request(port, "GET", "/")
    assert status == 200
    assert b'data-tab="demo"' in page and b'<script src="/ext/demo.js"></script>' in page
    assert b"VICTOR-EXT" not in page


def test_ext_script_served_and_names_restricted(control):
    port, _ = control
    assert request(port, "GET", "/ext/demo.js")[0] == 200
    for bad in ("/ext/../index.html", "/ext/Demo.js", "/ext/demo.html", "/ext/missing.js", "/ext/.js"):
        assert request(port, "GET", bad)[0] == 404, bad


def test_ext_routes_need_token_and_local_host(control):
    port, headers = control
    assert request(port, "GET", "/api/demo?x=1")[0] == 401
    assert request(port, "GET", "/api/demo?x=1", headers={**headers, "Host": "evil.example"})[0] == 403
    status, body = request(port, "GET", "/api/demo?x=1", headers=headers)
    assert status == 200 and json.loads(body) == {"q": "1"}
    status, body = request(port, "POST", "/api/demo", {"a": 1}, headers)
    assert status == 201 and json.loads(body) == {"echo": {"a": 1}}
    assert request(port, "POST", "/api/nothing", {}, headers)[0] == 404


def test_real_feature_modules_import_cleanly():
    cc_ext.load()
    try:
        assert cc_ext.ERRORS == {}
    finally:
        cc_ext.MODULES.clear(); cc_ext.ROUTES.clear(); cc_ext.ERRORS.clear()


def test_debian_loaders_present():
    lib = os.path.join(DEB, "lib", "victorlinux", "victorlib")
    assert os.path.isfile(os.path.join(lib, "ctl_ext", "__init__.py"))
    assert os.path.isfile(os.path.join(lib, "tk_ext", "__init__.py"))
    ctl = open(os.path.join(DEB, "bin", "victor-ctl")).read()
    assert "ctl_ext.register(sp)" in ctl and "ext[a.grp].handle(a)" in ctl
    assert "self.tab_ext()" in open(os.path.join(DEB, "bin", "victor-control")).read()
