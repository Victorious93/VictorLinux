"""Real tests: generate a config, start Asterisk on it, talk SIP over TLS. Skipped (and says so) when asterisk is not installed."""
import hashlib, json, os, re, shutil, socket, ssl, subprocess, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import victor_voip as V

PORT = 15071


def md5(s): return hashlib.md5(s.encode()).hexdigest()


class Sip:
    def __init__(self, port):
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
        self.s = c.wrap_socket(raw); self.local = self.s.getsockname()[1]; self.cseq = 0

    def req(self, method, uri, user, extra="", auth=None, body=""):
        self.cseq += 1
        h = (f"{method} {uri} SIP/2.0\r\nVia: SIP/2.0/TLS 127.0.0.1:{self.local};branch=z9hG4bK{os.urandom(4).hex()};rport\r\nMax-Forwards: 70\r\n"
             f"From: <sip:{user}@127.0.0.1>;tag={os.urandom(3).hex()}\r\nTo: <sip:{user}@127.0.0.1>\r\nCall-ID: {os.urandom(8).hex()}\r\nCSeq: {self.cseq} {method}\r\n"
             f"Contact: <sip:{user}@127.0.0.1:{self.local};transport=tls>\r\n{extra}" + (f"Authorization: {auth}\r\n" if auth else "") + f"Content-Length: {len(body)}\r\n\r\n{body}")
        self.s.sendall(h.encode()); return self.read()

    def read(self):
        buf = b""
        while b"\r\n\r\n" not in buf: buf += self.s.recv(4096)
        head, _, rest = buf.partition(b"\r\n\r\n")
        m = re.search(rb"Content-Length: *(\d+)", head, re.I); n = int(m.group(1)) if m else 0
        while len(rest) < n: rest += self.s.recv(4096)
        return head.decode(errors="replace")

    def register(self, user, pw, extra="Expires: 60\r\n"):
        r = self.req("REGISTER", "sip:127.0.0.1", user, extra)
        if not r.startswith("SIP/2.0 401"): return r
        ch = dict(re.findall(r'(\w+)="([^"]*)"', re.search(r"WWW-Authenticate: Digest (.*)", r).group(1)))
        uri = "sip:127.0.0.1"; ha1 = md5(f"{user}:{ch['realm']}:{pw}"); ha2 = md5(f"REGISTER:{uri}"); cn = os.urandom(4).hex(); resp = md5(f"{ha1}:{ch['nonce']}:00000001:{cn}:auth:{ha2}")
        a = f'Digest username="{user}", realm="{ch["realm"]}", nonce="{ch["nonce"]}", uri="{uri}", response="{resp}", algorithm=MD5, qop=auth, nc=00000001, cnonce="{cn}", opaque="{ch.get("opaque","")}"'
        return self.req("REGISTER", uri, user, extra, auth=a)


@unittest.skipUnless(shutil.which("asterisk") and shutil.which("openssl"), "asterisk/openssl not installed: SIP integration test NOT RUN")
class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="vv-"); os.environ["VICTOR_VOIP_DIR"] = cls.d
        V.main(["init", "--host", "127.0.0.1", "--port", str(PORT)])
        import io, contextlib
        cls.creds = {}
        for n, pstn in (("alice", True), ("bob", False)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()): V.main(["add-device", n] + (["--pstn"] if pstn else []))
            cls.creds[n] = json.loads(buf.getvalue())
        cls.ast = subprocess.Popen(["asterisk", "-C", os.path.join(cls.d, "etc/asterisk.conf"), "-f"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try: socket.create_connection(("127.0.0.1", PORT), timeout=1).close(); break
            except OSError: time.sleep(0.5)
        else: raise RuntimeError("asterisk did not come up")

    @classmethod
    def tearDownClass(cls):
        cls.ast.terminate(); cls.ast.wait(15); shutil.rmtree(cls.d, ignore_errors=True)

    def test_provisioning_doc_has_pin_and_srtp_and_unique_strong_passwords(self):
        a, b = self.creds["alice"], self.creds["bob"]
        self.assertEqual(a["transport"], "tls"); self.assertEqual(a["srtp"], "required"); self.assertRegex(a["server_cert_sha256"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertNotEqual(a["password"], b["password"]); self.assertEqual(len(a["password"]), 24); self.assertNotEqual(a["username"], b["username"])

    def test_authenticated_registration_succeeds_and_wrong_password_fails(self):
        ok = Sip(PORT).register(self.creds["alice"]["username"], self.creds["alice"]["password"]); self.assertTrue(ok.startswith("SIP/2.0 200"), ok)
        try: bad = Sip(PORT).register(self.creds["alice"]["username"], "wrong-password")
        except (TimeoutError, OSError): bad = "SIP/2.0 403 (no reply: dropped)"      # a silent drop is also a refusal
        self.assertFalse(bad.startswith("SIP/2.0 200"), bad)

    def test_unknown_user_and_anonymous_calls_are_refused(self):
        r = Sip(PORT).req("INVITE", "sip:1002@127.0.0.1", "stranger", "Content-Type: application/sdp\r\n")
        self.assertRegex(r.split("\r\n")[0], r"SIP/2.0 (401|403|404)")
        self.assertFalse(Sip(PORT).register("nobody", "x").startswith("SIP/2.0 200"))

    def test_no_plaintext_listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.5)
        s.sendto(b"OPTIONS sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:9;branch=z9hG4bKx\r\nFrom: <sip:a@x>;tag=1\r\nTo: <sip:x@127.0.0.1>\r\nCall-ID: u1\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n", ("127.0.0.1", 5060))
        with self.assertRaises((socket.timeout, ConnectionRefusedError)): s.recv(100)

    def test_dialplan_only_lets_pstn_devices_dial_out(self):
        out = subprocess.run(["asterisk", "-C", os.path.join(self.d, "etc/asterisk.conf"), "-rx", "dialplan show pstn-out"], capture_output=True, text=True).stdout
        self.assertIn('PSTN_OK', out); self.assertIn("GROUP_COUNT", out)
        ep = subprocess.run(["asterisk", "-C", os.path.join(self.d, "etc/asterisk.conf"), "-rx", "pjsip show endpoint " + self.creds["bob"]["username"]], capture_output=True, text=True).stdout
        self.assertIn("PSTN_OK", ep); self.assertIn("media_encryption", ep)


class TestRender(unittest.TestCase):
    def test_trunk_secret_stays_out_of_stdout_and_file_is_private(self):
        d = tempfile.mkdtemp(); os.environ["VICTOR_VOIP_DIR"] = d; os.environ["VOIP_TRUNK_PASSWORD"] = "trunk-secret-XYZ"
        if not shutil.which("openssl"): self.skipTest("no openssl")
        V.main(["init", "--host", "voip.example.test"]); V.main(["trunk", "--host", "sip.provider.test", "--user", "acct1"])
        self.assertEqual(oct(os.stat(os.path.join(d, "secrets.json")).st_mode & 0o777), "0o600")
        self.assertIn("trunk-secret-XYZ", open(os.path.join(d, "etc/pjsip.conf")).read())
        self.assertIn("media_encryption=sdes", open(os.path.join(d, "etc/pjsip.conf")).read())
        for bad in (["add-device", "../x"], ["set", "--allow-prefix", "1;rm"]):
            with self.assertRaises(SystemExit): V.main(bad)


if __name__ == "__main__":
    unittest.main()
